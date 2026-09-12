"""Realocar gravações: vincular a reunião a um evento da agenda e mover áudio entre reuniões.

Em 12/09/2026 a Nora Weekly foi gravada "na mão", sem título, convidados nem
link, porque a agenda não a mostrou; e uma gravação pode cair dentro de outra
reunião quando o botão da barra anexa a reunião errada. Corrigir isso era pedir
ao agente. Aqui a correção é do Bruno, na tela dedicada.

Mover reaproveita a exclusão (quarentena, invalidação e retirada durável do
conteúdo da origem no Zinom) e anexa ao destino um job NOVO com a transcrição
preservada: o id do job é a identidade da fonte no Zinom, e o antigo é
tombstonado pela origem. O job entra como `transcribed`: o daemon refaz
transcrição consolidada, resumo e entrega sem novo ASR.

Travas em ordem global (a reunião de nome menor primeiro), porque dois
movimentos cruzados A→B e B→A travariam um ao outro. A intenção do movimento
vai para o journal da exclusão ANTES de anexar no destino: a partir daí a
origem recusa "desfazer", e uma interrupção é concluída pelo daemon.
"""
import datetime
import json
import re
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from castanha.durability import atomic_write, file_sha256, meeting_lock, write_json

ARCHIVE_DIR = ".recording-exclusions"


class RelocationError(ValueError):
    pass


class RelocationConflict(RelocationError):
    pass


# O que vale guardar do evento: o que o painel, o Silver e o Zinom mostram.
_EVENT_FIELDS = ("uid", "title", "start", "end", "organizer", "conference_url", "conference_provider",
                 "html_link", "calendar_name", "account", "location")
_ATTENDEE_FIELDS = ("name", "email", "response", "organizer", "optional")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n\n?", re.DOTALL)
_CONVIDADOS = re.compile(r"(Convidados \(presença não confirmada\): )([^\n]*?)(\.?)(?=\n|$)")


def _slug_ok(slug: Any) -> str:
    if (not isinstance(slug, str) or not slug or slug.startswith(".")
            or any(c in slug for c in ("/", "\\", "\x00"))):
        raise RelocationError("Identidade da reunião inválida")
    return slug


def _agora() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def event_record(event: Any) -> Dict[str, Any]:
    """O evento da agenda no formato do `calendar_event` do metadata: só texto e listas simples."""
    if not isinstance(event, dict):
        raise RelocationError("Evento da agenda inválido")
    title = str(event.get("title") or "").strip()
    uid = str(event.get("uid") or "").strip()
    if not title or not uid:
        raise RelocationError("Evento da agenda sem título ou identificador")
    record: Dict[str, Any] = {}
    for key in _EVENT_FIELDS:
        value = event.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise RelocationError(f"Campo {key} do evento inválido")
        record[key] = value
    record["title"] = title
    record["uid"] = uid
    attendees: List[Dict[str, Any]] = []
    for item in event.get("attendees") or []:
        if not isinstance(item, dict):
            continue
        pessoa = {k: item.get(k) for k in _ATTENDEE_FIELDS if item.get(k) is not None}
        if pessoa.get("name") or pessoa.get("email"):
            attendees.append(pessoa)
    record["attendees"] = attendees
    record["source"] = "zinom"
    record["linked_at"] = _agora()
    return record


def _guards(slug: str, storage, *, papel: str) -> None:
    """Nada muda numa reunião em captura, congelada ou com exclusão por concluir.

    A exclusão pendente compara `recording_revision` com o seu journal: mexer
    na reunião agora deixaria a limpeza remota ou o reprocessamento órfãos.
    """
    from castanha.recording_exclusion import (ExclusionError, _capture_guard, capture_blocked,
                                              needs_resume, pending_exclusion)
    bronze = storage.bronze_dir / slug
    if not bronze.is_dir():
        raise RelocationError(f"Reunião de {papel} não encontrada")
    try:
        _capture_guard(slug)
    except ExclusionError as exc:
        raise RelocationError(str(exc)) from exc
    if (bronze / ".legacy-recovery").exists() or (bronze / ".legacy-recovery").is_symlink():
        raise RelocationError(f"Reunião de {papel} congelada por recuperação legada")
    from castanha.annotations import regeneration_pending
    if regeneration_pending(bronze):
        raise RelocationError(f"A reunião de {papel} tem um resumo em regeneração; aguarde ele concluir")
    if pending_exclusion(bronze):
        try:
            bloqueada = capture_blocked(slug, storage) or needs_resume(slug, storage)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RelocationError(f"Controle de exclusão da reunião de {papel} inválido; nada alterado") from exc
        if bloqueada:
            raise RelocationError(f"A reunião {'da ' + papel if papel == 'gravação' else 'de ' + papel} ainda tem uma alteração de áudios em andamento "
                                  "(retirada no Zinom ou reprocessamento). Aguarde ela concluir e tente de novo")


# ------------------------------------------------------------------ vincular
def relink_silver(text: str, metadata: Dict[str, Any], frontmatter: str) -> str:
    """Troca frontmatter, primeiro título e a linha de convidados; o resumo fica como está."""
    title = metadata.get("title") or "Reunião"
    body = _FRONTMATTER.sub("", text, count=1) if text.startswith("---\n") else text
    body = re.sub(r"^#\s+.*$", lambda _m: f"# {title}", body, count=1, flags=re.MULTILINE)
    attendees = (metadata.get("calendar_event") or {}).get("attendees") or []
    nomes = ", ".join(a.get("name") or a.get("email", "") for a in attendees if isinstance(a, dict)) or "Não identificados"
    body = _CONVIDADOS.sub(lambda m: m.group(1) + nomes + m.group(3), body, count=1)
    return frontmatter + body


def link_meeting_to_event(slug: str, event: Any, storage=None) -> Dict[str, Any]:
    """A reunião passa a ser o evento: título, convidados e link, no metadata, no Silver e no Gold.

    Não mexe em `recording_revision` (as gravações não mudaram) nem chama a LLM:
    o resumo já escrito fica, e "Reprocessar reunião" o refaz se ele quiser. O
    Zinom recebe a nova versão pela fila (o título muda a revisão do envelope).
    """
    from castanha.storage import MeetingStorage
    from castanha.summarizer import silver_frontmatter
    storage = storage or MeetingStorage()
    _slug_ok(slug)
    record = event_record(event)
    _guards(slug, storage, papel="gravação")
    bronze = storage.bronze_dir / slug
    with meeting_lock(bronze):
        meta = storage._read_bronze_metadata(slug)
        if not isinstance(meta, dict) or not meta or meta.get("slug", slug) != slug:
            raise RelocationError("Metadados da reunião inválidos; nada alterado")
        previous_title = meta.get("title") or ""
        previous_event = meta.get("calendar_event") if isinstance(meta.get("calendar_event"), dict) else {}
        history = [h for h in (meta.get("event_link_history") or []) if isinstance(h, dict)] \
            if isinstance(meta.get("event_link_history"), list) else []
        history.append({"at": record["linked_at"], "previous_title": previous_title,
                        "previous_event": previous_event, "event_uid": record["uid"]})
        meta.update(title=record["title"], calendar_event=record, event_link_history=history[-10:])
        delivery = meta.get("zinom") if isinstance(meta.get("zinom"), dict) else None
        if delivery and delivery.get("remember_id") and delivery.get("status") == "ok":
            # Nota legada (remember): a fila só a reedita se o recibo disser pendente.
            # O transporte Bronze não precisa disso: o título muda a revisão do envelope.
            meta["zinom"] = {**delivery, "status": "pending", "reason": "Evento da agenda vinculado; nota a atualizar"}
        silver = storage.silver_dir / f"{slug}.md"
        novo_silver = (relink_silver(silver.read_text(encoding="utf-8"), meta, silver_frontmatter(meta))
                       if silver.exists() else None)
        storage.write_bronze_metadata(slug, meta)
        if novo_silver is not None:
            atomic_write(silver, novo_silver)
        gold = storage.gold_dir / f"{slug}.json"
        if gold.exists():
            try:
                dados = json.loads(gold.read_text(encoding="utf-8"))
            except ValueError:
                dados = None
            if isinstance(dados, dict) and "title" in dados:
                dados["title"] = record["title"]
                write_json(gold, dados)
    quantos = len(record["attendees"])
    return {"status": "ok", "slug": slug, "title": record["title"], "previous_title": previous_title,
            "event_uid": record["uid"], "attendees": quantos,
            "message": f"Reunião vinculada a “{record['title']}” ({quantos} convidado(s)). "
                       "O Zinom recebe a versão atualizada na próxima sincronização."}


# --------------------------------------------------------------------- mover
def _job_for(bronze: Path, filename: str) -> Dict[str, Any]:
    jobs_dir = bronze / ".jobs"
    jobs: List[Dict[str, Any]] = []
    if jobs_dir.is_dir():
        for path in sorted(jobs_dir.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RelocationError("Job da gravação ilegível; nada foi movido") from exc
            if isinstance(job, dict):
                jobs.append(job)
    if not jobs:
        raise RelocationError("Gravação legada sem job nativo; mover só vale para gravações do Castanha")
    matching = [j for j in jobs if Path(str(j.get("audio_path") or "")).name == filename]
    if len(matching) != 1:
        raise RelocationError("Gravação não encontrada ou ambígua nesta reunião")
    job = matching[0]
    if Path(str(job.get("audio_path"))).parent != bronze:
        raise RelocationError("Job fora da reunião; nada foi movido")
    if not isinstance(job.get("id"), str) or not job["id"]:
        raise RelocationError("Job sem identidade; nada foi movido")
    if job.get("stage") not in ("done", "transcribed"):
        raise RelocationError("Aguarde a transcrição desta gravação terminar antes de movê-la")
    return job


def _create_destination(storage, title: str, job: Dict[str, Any], record_event: Optional[Dict[str, Any]]) -> str:
    recorded_at = str(job.get("recorded_at") or _agora())
    try:
        quando = datetime.datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        quando = datetime.datetime.now()
    dest_slug = storage.create_meeting_slug(title, dt=quando.astimezone() if quando.tzinfo else quando)
    (storage.bronze_dir / dest_slug).mkdir(parents=True, exist_ok=False)
    state = job.get("state") if isinstance(job.get("state"), dict) else {}
    storage.write_bronze_metadata(dest_slug, {
        "slug": dest_slug, "title": title, "recorded_at": recorded_at, "mode": state.get("mode", "dual"),
        "calendar_event": record_event or {"title": title, "start": recorded_at, "attendees": []},
        "recordings": [], "recording_revision": 0, "processing_status": "pending",
        "created_by": "move_recording"})
    return dest_slug


def _attach(storage, dest_slug: str, dest_title: str, bronze_b: Path, quarantine: Path, sha: str,
            job: Dict[str, Any], new_job_id: str, bronze_a: Path, origin_slug: str, now: str) -> Path:
    jobs_dir = bronze_b / ".jobs"
    jobs_dir.mkdir(exist_ok=True)
    ext = Path(str(job.get("audio_path"))).suffix or ".ogg"
    dest_audio = bronze_b / f"capture_{new_job_id}{ext}"
    job_file = jobs_dir / f"{new_job_id}.json"
    # A consolidação prefixa `base_transcript.txt` (o texto legado, anterior aos
    # jobs). Sem ele, o próximo processamento do destino apagaria a transcrição
    # de uma reunião legada, e uma captura seguinte numa reunião nova copiaria a
    # transcrição movida para a base e a somaria de novo. Mesma regra do stop.
    base = jobs_dir / "base_transcript.txt"
    if not base.exists():
        legado = bronze_b / "transcript_raw.txt"
        nativos = any(p.suffix == ".json" for p in jobs_dir.iterdir())
        atomic_write(base, "" if nativos or not legado.exists() else legado.read_text(encoding="utf-8"))
    # Idempotente: a retomada de um movimento interrompido repete esta etapa.
    if dest_audio.exists():
        if file_sha256(dest_audio) != sha:
            raise RelocationError("Já existe outro áudio com essa identidade no destino; nada anexado")
    else:
        atomic_write(dest_audio, quarantine)
        if file_sha256(dest_audio) != sha:
            dest_audio.unlink(missing_ok=True)
            raise RelocationError("Cópia do áudio divergente; nada anexado")
    # Artefatos por hash do áudio (canais já transcritos, escolha de provedor):
    # com eles o destino não precisa voltar ao ASR nem à VPS.
    for relative in (Path(".channels") / sha, Path(".providers") / f"{sha}.json"):
        source, target = bronze_a / relative, bronze_b / relative
        if source.exists() and not target.exists():
            target.parent.mkdir(exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=False)
            else:
                shutil.copy2(source, target)
    state = dict(job.get("state")) if isinstance(job.get("state"), dict) else {}
    current = dict(state.get("current_meeting")) if isinstance(state.get("current_meeting"), dict) else {}
    current["title"] = dest_title
    state["current_meeting"] = current
    new_job = {**job, "id": new_job_id, "audio_path": str(dest_audio), "state": state, "stage": "transcribed",
               "moved_from": {"slug": origin_slug, "job_id": job.get("id"),
                              "filename": Path(str(job.get("audio_path"))).name, "at": now}}
    if job_file.exists():
        atual = json.loads(job_file.read_text(encoding="utf-8"))
        if atual.get("id") != new_job_id or atual.get("sha256") != sha:
            raise RelocationError("Já existe outro job com essa identidade no destino; nada anexado")
    else:
        write_json(job_file, new_job)
    meta = storage._read_bronze_metadata(dest_slug)
    if any(isinstance(r, dict) and r.get("job_id") == new_job_id for r in meta.get("recordings") or []):
        return dest_audio
    record = {"id": dest_audio.name, "filename": dest_audio.name, "path": str(dest_audio), "job_id": new_job_id,
              "sha256": sha, "recorded_at": job.get("recorded_at"), "size_bytes": dest_audio.stat().st_size,
              "duration_seconds": job.get("duration_seconds", 0), "audio_status": job.get("audio_status", "desconhecido"),
              "transcribed": True, "transcription_error": None, "transcription_provider": job.get("provider"),
              "capture_mode": job.get("capture_mode", state.get("mode", "dual"))}
    recordings = [r for r in (meta.get("recordings") or []) if isinstance(r, dict)] + [record]
    meta.update(recordings=recordings, recordings_count=len(recordings),
                recording_revision=int(meta.get("recording_revision") or 0) + 1,
                processing_status="pending", bronze_audio_file=str(dest_audio),
                duration_seconds=sum(float(r.get("duration_seconds") or 0) for r in recordings))
    if meta.get("exclusion_id"):
        meta.update(content_status="rebuilding", can_restore=False,
                    restore_reason="Nova gravação adicionada; cópia anterior preservada")
    storage.write_bronze_metadata(dest_slug, meta)
    return dest_audio


def move_recording(slug: str, filename: str, *, to: Optional[str] = None, new_title: Optional[str] = None,
                   event: Any = None, expected_revision: Optional[int] = None, storage=None) -> Dict[str, Any]:
    """Move uma gravação (e sua transcrição) para outra reunião, existente ou nova.

    Passos: exclusão da origem (durável; reversível até aqui), cópia da quarentena
    para o destino sob o lock da origem, marcação `moved_to` no journal. Uma
    falha entre a exclusão e a cópia deixa a origem restaurável e o destino
    intocado. Depois de copiado, a origem não oferece mais "desfazer": a cópia
    da quarentena continua guardada.
    """
    from castanha.annotations import _directory
    from castanha.recording_exclusion import (ExclusionConflict, ExclusionError, _archive, _audio_hash, _load, _save,
                                              exclude_recording)
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    _slug_ok(slug)
    if (not isinstance(filename, str) or Path(filename).name != filename or filename in ("", ".", "..")
            or "\\" in filename):
        raise RelocationError("Nome da gravação inválido")
    if (to is None) == (new_title is None):
        raise RelocationError("Informe a reunião de destino ou o título da nova reunião")
    bronze_a = storage.bronze_dir / slug
    _guards(slug, storage, papel="origem")
    job = _job_for(bronze_a, filename)
    record_event = event_record(event) if event is not None else None
    if to is not None:
        _slug_ok(to)
        if to == slug:
            raise RelocationError("Origem e destino são a mesma reunião")
        if not (storage.bronze_dir / to).is_dir():
            raise RelocationError("Reunião de destino não encontrada")
        dest_title = (storage._read_bronze_metadata(to) or {}).get("title") or to
    else:
        dest_title = (new_title or "").strip()
        if not dest_title:
            raise RelocationError("Título da nova reunião vazio")
        if len(dest_title) > 200:
            raise RelocationError("Título da nova reunião longo demais")

    now = _agora()
    with ExitStack() as travas:
        # Ordem global: nome menor primeiro. A exclusão trava a origem por dentro;
        # se o destino vem antes na ordem, a trava dele é tomada já aqui e mantida
        # até o fim, com a validação feita sob ela.
        destino_travado = to is not None and to < slug
        if destino_travado:
            travas.enter_context(meeting_lock(storage.bronze_dir / to))
            _guards(to, storage, papel="destino")
        elif to is not None:
            _guards(to, storage, papel="destino")  # pré-checagem; repetida sob a trava adiante

        # 1) Origem: exclusão durável e reversível (quarentena, invalidação, retirada remota pendente).
        try:
            excluded = exclude_recording(slug, filename, storage, expected_revision=expected_revision)
        except ExclusionConflict as exc:
            raise RelocationConflict(str(exc)) from exc
        except ExclusionError as exc:
            raise RelocationError(str(exc)) from exc
        if not isinstance(excluded, dict) or excluded.get("status") != "ok":
            # `capture_start` devolve erro em vez de levantar quando a trava está ocupada.
            raise RelocationError((excluded or {}).get("message") or "Exclusão da origem recusada; nada foi movido")

        # 2) Trava da origem, do journal até o fim: o daemon não retoma a exclusão no meio.
        fd = travas.enter_context(_directory(storage, slug, lock=True))
        op = _load(fd)
        if not op or op.get("filename") != filename or op.get("phase") in ("restored", "restoring"):
            raise RelocationError("Exclusão da origem não encontrada; a gravação segue na quarentena")
        if op.get("moved_to"):
            raise RelocationError("Esta gravação já foi movida")
        with _archive(fd, op["id"]) as archive:
            if _audio_hash(archive, "audio") != op["audio_sha256"]:
                raise RelocationError("Áudio da quarentena diverge do original; nada foi movido")

        # 3) Destino sob trava (ordem: origem < destino aqui) e revalidado; ou reunião nova.
        created = to is None
        if to is not None:
            if not destino_travado:
                travas.enter_context(meeting_lock(storage.bronze_dir / to))
                _guards(to, storage, papel="destino")
            dest_slug = to
        else:
            dest_slug = _create_destination(storage, dest_title, job, record_event)
            travas.enter_context(meeting_lock(storage.bronze_dir / dest_slug))

        # 4) Journal ANTES de anexar: intenção e identidade do movimento. Daqui em
        #    diante a origem recusa "desfazer", e uma interrupção é retomada pelo daemon.
        new_job_id = uuid.uuid4().hex
        op["moved_to"] = {"slug": dest_slug, "job_id": new_job_id, "title": dest_title, "at": now,
                          "phase": "attaching"}
        _save(fd, op)
        # 5) Anexar (idempotente) e 6) concluir a origem.
        _attach(storage, dest_slug, dest_title, storage.bronze_dir / dest_slug,
                bronze_a / ARCHIVE_DIR / op["id"] / "audio", op["audio_sha256"], job, new_job_id, bronze_a, slug, now)
        metadata = _finish_origin(fd, slug, op, dest_title)
    return {"status": "ok", "slug": slug, "filename": filename,
            "destination": {"slug": dest_slug, "title": dest_title, "job_id": new_job_id, "created": created},
            "exclusion_id": op["id"], "remaining_count": len(op.get("remaining") or []),
            "content_status": metadata.get("content_status"), "cleanup_status": metadata.get("cleanup_status"),
            "can_restore": False, "recording_revision": metadata.get("recording_revision"),
            "message": f"Gravação movida para “{dest_title}”. Transcrição, resumo e entrega ao Zinom "
                       "das duas reuniões serão refeitos automaticamente."}


def _finish_origin(fd, slug: str, op: Dict[str, Any], dest_title: str) -> Dict[str, Any]:
    """Journal concluído e metadata da origem: sem desfazer, e reprocessamento pedido se sobrou áudio."""
    from castanha.annotations import _metadata, _read, _write_json
    from castanha.recording_exclusion import _save, _sha
    op["moved_to"]["phase"] = "attached"
    if op.get("remaining"):
        # Mesmo pedido que o botão "Reprocessar" faria: o daemon refaz a origem sem o áudio movido.
        op["reprocess_requested"] = True
    _save(fd, op)
    metadata = _metadata(fd, slug)
    metadata.update(can_restore=False, restore_reason=f"Gravação movida para {dest_title}")
    if op.get("remaining"):
        metadata.update(content_status="rebuilding", processing_status="pending")
    _write_json(fd, "metadata.json", metadata)
    op["after_metadata_sha256"] = _sha(_read(fd, "metadata.json"))
    _save(fd, op)
    return metadata


def move_pending(op: Any) -> bool:
    """True quando o journal registra um movimento ainda não anexado no destino."""
    return bool(op) and isinstance(op.get("moved_to"), dict) and op["moved_to"].get("phase") == "attaching"


def resume_move(slug: str, storage=None) -> bool:
    """Conclui um movimento interrompido depois do journal: anexa no destino e fecha a origem.

    Chamado pela retomada da exclusão antes de ela travar a origem. Travas na
    mesma ordem global do movimento. Sem nada a concluir, não faz nada.
    """
    from castanha.annotations import _directory
    from castanha.recording_exclusion import _archive, _audio_hash, _load
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    bronze_a = storage.bronze_dir / slug
    try:
        with _directory(storage, slug) as fd:
            op = _load(fd)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not move_pending(op):
        return False
    dest_slug = str(op["moved_to"].get("slug") or "")
    _slug_ok(dest_slug)
    bronze_b = storage.bronze_dir / dest_slug
    if not bronze_b.is_dir():
        raise RelocationError("Reunião de destino do movimento não existe mais; o áudio segue na quarentena da origem")
    with ExitStack() as travas:
        primeiro, segundo = sorted((slug, dest_slug))
        for nome in (primeiro, segundo):
            if nome == slug:
                fd = travas.enter_context(_directory(storage, slug, lock=True))
            else:
                travas.enter_context(meeting_lock(bronze_b))
        op = _load(fd)
        if not move_pending(op):
            return False
        with _archive(fd, op["id"]) as archive:
            if _audio_hash(archive, "audio") != op["audio_sha256"]:
                raise RelocationError("Áudio da quarentena diverge do original; movimento não concluído")
        job = _job_from_quarantine(fd, op)
        _attach(storage, dest_slug, str(op["moved_to"].get("title") or dest_slug), bronze_b,
                bronze_a / ARCHIVE_DIR / op["id"] / "audio", op["audio_sha256"], job,
                str(op["moved_to"]["job_id"]), bronze_a, slug, _agora())
        _finish_origin(fd, slug, op, str(op["moved_to"].get("title") or dest_slug))
    return True


def _job_from_quarantine(fd, op: Dict[str, Any]) -> Dict[str, Any]:
    """O job removido, como a exclusão o guardou (snapshot em `.jobs/<id>.json`)."""
    from castanha.annotations import _read
    from castanha.recording_exclusion import _archive, _backup
    removed = op.get("removed_job_file")
    with _archive(fd, op["id"]) as archive:
        for item in op.get("files") or []:
            if item.get("root") == "bronze" and item.get("parts") == [".jobs", removed]:
                texto = _backup(archive, item)
                if texto:
                    job = json.loads(texto)
                    if isinstance(job, dict):
                        return job
    raise RelocationError("Job da gravação movida não encontrado na quarentena; movimento não concluído")
