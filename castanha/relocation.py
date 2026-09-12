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

from castanha.capture_gate import capture_start
from castanha.config import load_config
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
# Transcrição e anotações manuais são conteúdo histórico ou autoral: nada a
# partir da primeira dessas seções é reescrito.
_TRANSCRICAO = re.compile(r"^##\s+(?:.{0,3}\s*Transcri|Anotações manuais)", re.MULTILINE)


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
        from castanha.annotations import _directory
        from castanha.recording_exclusion import _load
        try:
            bloqueada = capture_blocked(slug, storage) or needs_resume(slug, storage)
            with _directory(storage, slug) as fd:
                op = _load(fd)
            restauravel = bool(op and op.get("phase") not in ("restored",)
                               and (storage._read_bronze_metadata(slug) or {}).get("can_restore") is True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RelocationError(f"Controle de exclusão da reunião de {papel} inválido; nada alterado") from exc
        if bloqueada:
            raise RelocationError(f"A reunião {'da ' + papel if papel == 'gravação' else 'de ' + papel} ainda tem uma alteração de áudios em andamento "
                                  "(retirada no Zinom ou reprocessamento). Aguarde ela concluir e tente de novo")
        if restauravel:
            # Mexer no metadata agora deixaria o "Desfazer" oferecido e sempre recusado.
            raise RelocationError(f"A reunião de {papel} tem uma exclusão ainda restaurável; "
                                  "desfaça-a ou reprocesse antes de continuar")


def _destination_guards(dest: str, origin: str, storage) -> None:
    """Além das guardas comuns: destino real (não link), diferente da origem, vivo no Zinom e nativo.

    Link simbólico para a origem passaria pela comparação de nomes e travaria
    o próprio `.processing.lock` por outro descritor (deadlock). Reunião com
    entrega terminal no Zinom (`tombstoned`/`superseded`) sai da fila de
    processamento e o job movido nunca seria consolidado. Reunião legada, com
    gravação sem job nativo, não passa pela ponte Bronze e a entrega ficaria
    bloqueada: para ela, o caminho é uma reunião nova.
    """
    bronze_b = storage.bronze_dir / dest
    if bronze_b.is_symlink() or not bronze_b.is_dir():
        raise RelocationError("Reunião de destino não encontrada")
    if bronze_b.resolve() == (storage.bronze_dir / origin).resolve():
        raise RelocationError("Origem e destino são a mesma reunião")
    _guards(dest, storage, papel="destino")
    meta = storage._read_bronze_metadata(dest)
    if not isinstance(meta, dict) or not meta or meta.get("slug", dest) != dest:
        raise RelocationError("Metadados da reunião de destino inválidos; nada foi movido")
    delivery = meta.get("zinom") if isinstance(meta.get("zinom"), dict) else {}
    if delivery.get("status") in ("tombstoned", "superseded"):
        raise RelocationError("A reunião de destino foi retirada do Zinom e não recebe gravações; escolha outra ou crie uma nova")
    if any(isinstance(r, dict) and not r.get("job_id") for r in meta.get("recordings") or []):
        raise RelocationError("A reunião de destino é legada (gravação sem job nativo) e não passa pela ponte Bronze; "
                              "mova para uma reunião nova")


# ------------------------------------------------------------------ vincular
def _nomes(attendees: Any) -> str:
    return ", ".join(a.get("name") or a.get("email", "") for a in (attendees or []) if isinstance(a, dict)) or "Não identificados"


def relink_silver(text: str, metadata: Dict[str, Any], frontmatter: str, previous_attendees: Any = None) -> str:
    """Troca frontmatter, primeiro título e a linha controlada de convidados; o resumo fica como está.

    Só a linha inteira igual à lista anterior é campo controlado: a mesma frase
    dentro de um parágrafo do resumo (ou com texto depois) é texto do resumo.
    Sem a linha anterior, a lista nova entra logo abaixo do título.
    """
    title = metadata.get("title") or "Reunião"
    body = _FRONTMATTER.sub("", text, count=1) if text.startswith("---\n") else text
    corte = _TRANSCRICAO.search(body)
    body, transcricao = (body[:corte.start()], body[corte.start():]) if corte else (body, "")
    body = re.sub(r"^#\s+.*$", lambda _m: f"# {title}", body, count=1, flags=re.MULTILINE)
    nomes = _nomes((metadata.get("calendar_event") or {}).get("attendees"))
    atual = re.compile(r"^Convidados \(presença não confirmada\): " + re.escape(nomes) + r"\.$", re.MULTILINE)
    anterior = re.compile(r"^Convidados \(presença não confirmada\): " + re.escape(_nomes(previous_attendees)) + r"\.?$",
                          re.MULTILINE)
    if atual.search(body):
        pass  # reaplicação (por exemplo, depois de uma falha): a linha certa já está lá
    elif anterior.search(body):
        body = anterior.sub(lambda _m: f"Convidados (presença não confirmada): {nomes}.", body, count=1)
    else:
        # Sem a linha controlada, só o frontmatter mudaria, e o frontmatter não vai ao
        # Zinom: a lista de convidados entra logo abaixo do título, no corpo da nota.
        body = re.sub(r"^(#\s+.*)$", lambda m: f"{m.group(1)}\n\nConvidados (presença não confirmada): {nomes}.",
                      body, count=1, flags=re.MULTILINE)
    # O link da chamada também é conteúdo da nota (e do envelope), não só metadata.
    # O link da chamada também é conteúdo da nota (e do envelope). Só a linha colada
    # à linha controlada de convidados é nossa; um "Link da chamada:" em outra seção
    # (uma sala alternativa anotada nos próximos passos) é texto do resumo e fica.
    link = str((metadata.get("calendar_event") or {}).get("conference_url") or "").strip()
    controlada = re.compile(r"^(Convidados \(presença não confirmada\): " + re.escape(nomes) + r"\.)(\nLink da chamada: [^\n]*)?$",
                            re.MULTILINE)
    body = controlada.sub(lambda m: m.group(1) + (f"\nLink da chamada: {link}" if link else ""), body, count=1)
    return frontmatter + body + transcricao


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
        _guards(slug, storage, papel="gravação")  # de novo, sob a trava: nada começou nesse meio tempo
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
        # Derivados primeiro, metadata por último: se algo falhar no meio, o vínculo
        # não consta no metadata e a operação é reaplicável do zero (cada escrita é atômica).
        silver = storage.silver_dir / f"{slug}.md"
        if silver.exists():
            atomic_write(silver, relink_silver(silver.read_text(encoding="utf-8"), meta, silver_frontmatter(meta),
                                               previous_attendees=(previous_event or {}).get("attendees")))
        gold = storage.gold_dir / f"{slug}.json"
        if gold.exists():
            try:
                dados = json.loads(gold.read_text(encoding="utf-8"))
            except ValueError:
                dados = None
            if isinstance(dados, dict) and "title" in dados:
                dados["title"] = record["title"]
                write_json(gold, dados)
        storage.write_bronze_metadata(slug, meta)
    quantos = len(record["attendees"])
    return {"status": "ok", "slug": slug, "title": record["title"], "previous_title": previous_title,
            "event_uid": record["uid"], "attendees": quantos,
            "message": f"Reunião vinculada a “{record['title']}” ({quantos} convidado(s)). "
                       "O Zinom recebe a versão atualizada na próxima sincronização."}


# --------------------------------------------------------------------- mover
def _refuse_unscoped_remote(metadata: Any) -> None:
    """Reunião com nota legada no Zinom (`remember_id`) não move.

    A exclusão dessa reunião marca a retirada remota como "sem escopo
    verificável" e a deixa pendente de revisão humana, mas ainda restaurável.
    Mover tiraria o "desfazer" e deixaria a origem sem derivados e sem retomada.
    """
    delivery = metadata.get("zinom") if isinstance(metadata, dict) and isinstance(metadata.get("zinom"), dict) else {}
    if delivery.get("remember_id"):
        raise RelocationError("A reunião de origem tem uma nota legada no Zinom sem escopo verificável; "
                              "use Excluir e Reprocessar em vez de mover")


def _refuse_retired_source(bronze: Path, metadata: Any, job: Dict[str, Any]) -> None:
    """Conteúdo que o Zinom já retirou (tombstoned/superseded) não ganha identidade nova.

    Mover geraria outro `source_id` e republicaria o que foi esquecido de propósito.
    """
    import hashlib
    from castanha.bronze_ingest import TERMINAL_STATES
    delivery = metadata.get("zinom") if isinstance(metadata, dict) and isinstance(metadata.get("zinom"), dict) else {}
    if delivery.get("status") in TERMINAL_STATES:
        raise RelocationError("O conteúdo desta reunião foi retirado do Zinom e não pode ser movido para outra identidade")
    source = "castanha:" + hashlib.sha256(str(job.get("id") or "").encode("utf-8")).hexdigest()
    receipts = bronze / ".brain-ingest"
    if receipts.is_dir():
        from castanha.bronze_ingest import TERMINAL_EVIDENCE, BronzeIngestError, _verify_terminal_receipts
        try:
            # O mesmo verificador da entrega: checkpoints e a evidência terminal
            # independente têm de concordar; um estado inconsistente (tombstone
            # anotado antes de o checkpoint ser atualizado) recusa em vez de liberar.
            checkpoints = _verify_terminal_receipts(receipts, metadata if isinstance(metadata, dict) else {})
            evidence_path = receipts / TERMINAL_EVIDENCE
            evidence = json.loads(evidence_path.read_bytes()) if evidence_path.exists() else {}
        except (BronzeIngestError, ValueError, OSError, KeyError, TypeError) as exc:
            raise RelocationError("Recibos do Zinom na origem inconsistentes ou ilegíveis; nada foi movido") from exc
        for saved in checkpoints.values():
            envelope = ((saved.get("request") or {}).get("envelope") or {}) if isinstance(saved, dict) else {}
            if envelope.get("source_id") == source and saved.get("status") in TERMINAL_STATES:
                raise RelocationError("Esta gravação foi retirada do Zinom e não pode ser movida para outra identidade")
        for entry in (evidence.values() if isinstance(evidence, dict) else []):
            if isinstance(entry, dict) and entry.get("source_id") == source:
                raise RelocationError("Esta gravação foi retirada do Zinom e não pode ser movida para outra identidade")


def _refuse_orphan_legacy_base(bronze: Path, metadata: Any, job: Dict[str, Any]) -> None:
    """Mover o último job de uma reunião com texto legado na base a deixaria vazia.

    O engine não reconstrói uma reunião só a partir de `base_transcript.txt`
    (sem áudio e sem job), então a exclusão apagaria a transcrição consolidada e
    os derivados sem ninguém para refazê-los. Melhor recusar do que esconder.
    """
    base = bronze / ".jobs" / "base_transcript.txt"
    try:
        legado = base.read_text(encoding="utf-8").strip() if base.exists() else ""
    except OSError:
        legado = ""
    if not legado:
        return
    removed = set((metadata or {}).get("removed_job_ids") or []) if isinstance(metadata, dict) else set()
    outros_jobs = [p for p in (bronze / ".jobs").glob("*.json") if p.stem != job.get("id") and p.stem not in removed]
    # Só outro job nativo reconstrói a base: um áudio legado já transcrito é pulado
    # pela retomada legada e a base ficaria fora da transcrição consolidada.
    if not outros_jobs:
        raise RelocationError("Este é o último job de uma reunião com transcrição legada; movê-lo deixaria a "
                              "reunião sem como ser reconstruída. Use uma reunião nova para a gravação ou mantenha-a aqui")


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


def _planned_slug(storage, title: str, job: Dict[str, Any]) -> str:
    """O nome da reunião nova, decidido antes de qualquer alteração e guardado na intenção."""
    recorded_at = str(job.get("recorded_at") or _agora())
    try:
        quando = datetime.datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        quando = datetime.datetime.now()
    return storage.create_meeting_slug(title, dt=quando.astimezone() if quando.tzinfo else quando)


def _create_destination(storage, title: str, job: Dict[str, Any], record_event: Optional[Dict[str, Any]],
                        slug: Optional[str] = None) -> str:
    recorded_at = str(job.get("recorded_at") or _agora())
    dest_slug = slug or _planned_slug(storage, title, job)
    bronze_b = storage.bronze_dir / dest_slug
    if bronze_b.is_symlink():
        raise RelocationError("Já existe outra reunião com o nome planejado para o destino; nada foi movido")
    if bronze_b.exists():
        # Uma tentativa anterior já criou esta reunião (e caiu antes do journal): reutilizar,
        # desde que seja a nossa e ainda vazia; nunca criar uma segunda com sufixo.
        existente = storage._read_bronze_metadata(dest_slug) or {}
        if existente:
            if existente.get("created_by") != "move_recording" or existente.get("recordings"):
                raise RelocationError("Já existe outra reunião com o nome planejado para o destino; nada foi movido")
            return dest_slug
        # Pasta criada e metadata não gravado (queda ou disco cheio no meio): concluir
        # a criação, desde que a pasta esteja vazia além da trava.
        if any(p.name != ".processing.lock" for p in bronze_b.iterdir()):
            raise RelocationError("Já existe outra reunião com o nome planejado para o destino; nada foi movido")
    else:
        bronze_b.mkdir(parents=True, exist_ok=False)
    state = job.get("state") if isinstance(job.get("state"), dict) else {}
    storage.write_bronze_metadata(dest_slug, {
        "slug": dest_slug, "title": title, "recorded_at": recorded_at, "mode": state.get("mode", "dual"),
        "calendar_event": record_event or {"title": title, "start": recorded_at, "attendees": []},
        "recordings": [], "recording_revision": 0, "processing_status": "pending",
        "created_by": "move_recording"})
    return dest_slug


def _attach(storage, dest_slug: str, dest_title: str, bronze_b: Path, quarantine: Path, sha: str,
            job: Dict[str, Any], new_job_id: str, bronze_a: Path, origin_slug: str, now: str) -> Path:
    """Anexa a gravação no destino. Idempotente: a retomada repete esta etapa.

    Ordem: job (com `moved_from.committed=False`), áudio, artefatos por hash,
    metadata, e por fim `committed=True` no job. O áudio nunca existe no destino
    sem um job que o identifique, então uma exclusão no meio do caminho não o
    encontra sem identidade; e o marcador de conclusão é o job, não a presença
    do registro em `recordings`, que o daemon pode reconstruir sozinho.
    """
    jobs_dir = bronze_b / ".jobs"
    jobs_dir.mkdir(exist_ok=True)
    ext = Path(str(job.get("audio_path"))).suffix or ".ogg"
    dest_audio = bronze_b / f"capture_{new_job_id}{ext}"
    job_file = jobs_dir / f"{new_job_id}.json"
    # A consolidação prefixa `base_transcript.txt` (o texto legado, anterior aos
    # jobs). Sem ele, uma captura seguinte numa reunião nova copiaria a
    # transcrição movida para a base e a somaria de novo. Mesma regra do stop.
    base = jobs_dir / "base_transcript.txt"
    if not base.exists():
        legado = bronze_b / "transcript_raw.txt"
        nativos = any(p.suffix == ".json" for p in jobs_dir.iterdir())
        atomic_write(base, "" if nativos or not legado.exists() else legado.read_text(encoding="utf-8"))
    state = dict(job.get("state")) if isinstance(job.get("state"), dict) else {}
    current = dict(state.get("current_meeting")) if isinstance(state.get("current_meeting"), dict) else {}
    current["title"] = dest_title
    state["current_meeting"] = current
    if job_file.exists():
        new_job = json.loads(job_file.read_text(encoding="utf-8"))
        if not isinstance(new_job, dict) or new_job.get("id") != new_job_id or new_job.get("sha256") != sha:
            raise RelocationError("Já existe outro job com essa identidade no destino; nada anexado")
        if (new_job.get("moved_from") or {}).get("committed") is True:
            return dest_audio  # tentativa anterior concluiu tudo
    else:
        new_job = {**job, "id": new_job_id, "audio_path": str(dest_audio), "state": state, "stage": "transcribed",
                   "moved_from": {"slug": origin_slug, "job_id": job.get("id"),
                                  "filename": Path(str(job.get("audio_path"))).name, "at": now,
                                  "phase": "job", "committed": False}}
        write_json(job_file, new_job)
    if dest_audio.exists():
        if file_sha256(dest_audio) != sha:
            raise RelocationError("Já existe outro áudio com essa identidade no destino; nada anexado")
    else:
        # Áudio ausente com anexo não concluído só pode ser cópia interrompida:
        # `delete-recording` recusa apagar um áudio cujo anexo ainda não foi
        # concluído, e a exclusão registra o id em `removed_job_ids`.
        atomic_write(dest_audio, quarantine)
        if file_sha256(dest_audio) != sha:
            dest_audio.unlink(missing_ok=True)
            raise RelocationError("Cópia do áudio divergente; nada anexado")
        new_job = {**new_job, "moved_from": {**(new_job.get("moved_from") or {}), "phase": "audio"}}
        write_json(job_file, new_job)
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
    meta = storage._read_bronze_metadata(dest_slug)
    ja_listado = any(isinstance(r, dict) and r.get("job_id") == new_job_id for r in meta.get("recordings") or [])
    record = {"id": dest_audio.name, "filename": dest_audio.name, "path": str(dest_audio), "job_id": new_job_id,
              "sha256": sha, "recorded_at": job.get("recorded_at"), "size_bytes": dest_audio.stat().st_size,
              "duration_seconds": job.get("duration_seconds", 0), "audio_status": job.get("audio_status", "desconhecido"),
              "transcribed": True, "transcription_error": None, "transcription_provider": job.get("provider"),
              "capture_mode": job.get("capture_mode", state.get("mode", "dual"))}
    recordings = [r for r in (meta.get("recordings") or []) if isinstance(r, dict)] + ([] if ja_listado else [record])
    # Numa retomada, o daemon pode já ter consolidado este job (`done`): reabrir o
    # processamento deixaria o destino preso, porque a fila só reprocessa com job
    # fora de `done` e a entrega recusa metadata pendente.
    processing = "pending" if new_job.get("stage") != "done" else (meta.get("processing_status") or "complete")
    meta.update(recordings=recordings, recordings_count=len(recordings),
                recording_revision=int(meta.get("recording_revision") or 0) + 1,
                processing_status=processing, bronze_audio_file=str(dest_audio),
                duration_seconds=sum(float(r.get("duration_seconds") or 0) for r in recordings))
    if meta.get("exclusion_id") and processing == "pending":
        # Como uma captura nova numa reunião com exclusão anterior. Com o job já
        # consolidado, o conteúdo atual continua atual.
        meta.update(content_status="rebuilding", can_restore=False,
                    restore_reason="Nova gravação adicionada; cópia anterior preservada")
    storage.write_bronze_metadata(dest_slug, meta)
    moved_from = dict(new_job.get("moved_from") or {})
    moved_from.update(phase="metadata", committed=True)
    write_json(job_file, {**new_job, "moved_from": moved_from})
    return dest_audio


@capture_start
def move_recording(slug: str, filename: str, *, to: Optional[str] = None, new_title: Optional[str] = None,
                   event: Any = None, expected_revision: Optional[int] = None, storage=None) -> Dict[str, Any]:
    """Move uma gravação (e sua transcrição) para outra reunião, existente ou nova.

    Sob a trava da origem, sem soltar: exclusão (quarentena, invalidação, retirada
    remota pendente), journal `moved_to`, anexo no destino, fecho da origem. Uma
    falha antes do journal deixa a origem restaurável e o destino intocado; depois
    dele a origem recusa "desfazer" e o daemon conclui o que faltou. A cópia da
    quarentena continua guardada em qualquer caso. Como a captura, corre sob a
    trava global de mutação (`capture_start`).
    """
    import os
    from castanha.annotations import _directory, _write_json
    from castanha.recording_exclusion import (MOVE_INTENT, ExclusionConflict, ExclusionError, _archive, _audio_hash,
                                              _exclude_locked, _load, _save)
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    if not move_enabled():
        raise RelocationError("Mover gravação está desligado nesta instalação (relocation.move_enabled)")
    _slug_ok(slug)
    if (not isinstance(filename, str) or Path(filename).name != filename or filename in ("", ".", "..")
            or "\\" in filename):
        raise RelocationError("Nome da gravação inválido")
    if (to is None) == (new_title is None):
        raise RelocationError("Informe a reunião de destino ou o título da nova reunião")
    bronze_a = storage.bronze_dir / slug
    _guards(slug, storage, papel="origem")
    _refuse_unscoped_remote(storage._read_bronze_metadata(slug))
    job = _job_for(bronze_a, filename)
    _refuse_retired_source(bronze_a, storage._read_bronze_metadata(slug), job)
    _refuse_orphan_legacy_base(bronze_a, storage._read_bronze_metadata(slug), job)
    record_event = event_record(event) if event is not None else None
    if to is not None:
        _slug_ok(to)
        if to == slug:
            raise RelocationError("Origem e destino são a mesma reunião")
        _destination_guards(to, slug, storage)  # pré-checagem; repetida sob a trava adiante
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
            _destination_guards(to, slug, storage)

        # 1) Trava da origem, da exclusão ao fim: entre uma etapa e outra ninguém
        #    retira conteúdo remoto, restaura nem captura nesta reunião.
        fd = travas.enter_context(_directory(storage, slug, lock=True))
        _guards(slug, storage, papel="origem")  # de novo, sob a trava
        metadata_a = storage._read_bronze_metadata(slug)
        _refuse_unscoped_remote(metadata_a)
        job = _job_for(bronze_a, filename)  # relido sob a trava: um retry concorrente pode tê-lo mudado
        _refuse_retired_source(bronze_a, metadata_a, job)
        _refuse_orphan_legacy_base(bronze_a, metadata_a, job)
        # Intenção ANTES da exclusão: se o processo cair entre as duas, a retomada
        # sabe que aquela exclusão era um movimento e o conclui, em vez de tratá-la
        # como exclusão comum (retirada remota e sem destino).
        planned = to if to is not None else _planned_slug(storage, dest_title, job)
        _write_json(fd, MOVE_INTENT, {"version": 1, "filename": filename, "job_id": job.get("id"),
                                      "dest_slug": planned, "create_new": to is None,
                                      "dest_title": dest_title, "event": record_event, "at": now})
        try:
            excluded = _exclude_locked(fd, storage, slug, filename, expected_revision)
        except ExclusionConflict as exc:
            _remove_intent(fd)
            raise RelocationConflict(str(exc)) from exc
        except ExclusionError as exc:
            _remove_intent(fd)
            raise RelocationError(str(exc)) from exc
        if not isinstance(excluded, dict) or excluded.get("status") != "ok":
            _remove_intent(fd)
            raise RelocationError((excluded or {}).get("message") or "Exclusão da origem recusada; nada foi movido")
        # 2) O journal recém-criado.
        op = _load(fd)
        if not op or op.get("filename") != filename or op.get("phase") in ("restored", "restoring"):
            raise RelocationError("Exclusão da origem não encontrada; a gravação segue na quarentena")
        if op.get("moved_to"):
            _remove_intent(fd)
            raise RelocationError("Esta gravação já foi movida")
        if op.get("unscoped_remote"):
            # Ainda sem `moved_to`: a exclusão fica restaurável pelo "Desfazer".
            _remove_intent(fd)
            raise RelocationError("A retirada remota desta reunião não tem escopo verificável; a gravação foi "
                                  "excluída e continua restaurável, mas não foi movida")
        with _archive(fd, op["id"]) as archive:
            if _audio_hash(archive, "audio") != op["audio_sha256"]:
                raise RelocationError("Áudio da quarentena diverge do original; nada foi movido")

        # 3) Destino sob trava (ordem: origem < destino aqui) e revalidado; ou reunião nova.
        created = to is None
        try:
            if to is not None:
                if not destino_travado:
                    travas.enter_context(meeting_lock(storage.bronze_dir / to))
                    _destination_guards(to, slug, storage)
                dest_slug = to
            else:
                dest_slug = _create_destination(storage, dest_title, job, record_event, slug=planned)
                travas.enter_context(meeting_lock(storage.bronze_dir / dest_slug))
        except RelocationError as exc:
            # A origem já foi excluída com a intenção gravada: o daemon conclui o
            # movimento quando o destino liberar. Nada fica restaurável pela metade.
            return {"status": "pending", "slug": slug, "filename": filename, "exclusion_id": op["id"],
                    "remaining_count": len(op.get("remaining") or []), "can_restore": False,
                    "destination": {"slug": to, "title": dest_title, "created": False},
                    "message": f"Gravação retirada da origem, mas o destino não está disponível agora ({exc}). "
                               "O movimento será concluído automaticamente assim que ele liberar."}

        # 4) Journal ANTES de anexar: intenção e identidade do movimento. Daqui em
        #    diante a origem recusa "desfazer", e uma interrupção é retomada pelo daemon.
        new_job_id = uuid.uuid4().hex
        op["moved_to"] = {"slug": dest_slug, "job_id": new_job_id, "title": dest_title, "at": now,
                          "phase": "attaching"}
        _save(fd, op)
        _remove_intent(fd)  # o journal assumiu a intenção
        # 5) Anexar (idempotente, a partir do snapshot que a exclusão guardou) e 6) concluir a origem.
        _attach(storage, dest_slug, dest_title, storage.bronze_dir / dest_slug,
                bronze_a / ARCHIVE_DIR / op["id"] / "audio", op["audio_sha256"], _job_from_quarantine(fd, op),
                new_job_id, bronze_a, slug, now)
        metadata = _finish_origin(fd, slug, op, dest_title, storage)
    automatico = bool((load_config().get("sync") or {}).get("auto_retry_enabled", False))
    return {"status": "ok", "slug": slug, "filename": filename, "auto_resume": automatico,
            "destination": {"slug": dest_slug, "title": dest_title, "job_id": new_job_id, "created": created},
            "exclusion_id": op["id"], "remaining_count": len(op.get("remaining") or []),
            "content_status": metadata.get("content_status"), "cleanup_status": metadata.get("cleanup_status"),
            "can_restore": False, "recording_revision": metadata.get("recording_revision"),
            "message": f"Gravação movida para “{dest_title}”. Transcrição, resumo e entrega ao Zinom das duas reuniões "
                       + ("serão refeitos automaticamente pelo daemon." if automatico else
                          "ficam pendentes: rode `castanha sync --all` ou Reprocessar em cada reunião.")}


def _remove_intent(fd) -> None:
    import os
    from castanha.recording_exclusion import MOVE_INTENT
    try:
        os.unlink(MOVE_INTENT, dir_fd=fd)
    except FileNotFoundError:
        pass


def _adopt_intent(fd, storage, slug: str) -> bool:
    """Exclusão feita, journal sem `moved_to`, intenção no disco: assume o movimento.

    Cria o destino novo se a intenção o pedia. Devolve True se assumiu algo.
    """
    from castanha.recording_exclusion import _apply, _load, _move_intent, _save
    intent = _move_intent(fd)
    op = _load(fd)
    if not intent:
        return False
    if op and op.get("phase") == "applying":
        _apply(fd, storage, op)  # exclusão interrompida: concluí-la antes de assumir o movimento
        op = _load(fd)
    if not op or op.get("phase") == "restored" or op.get("filename") != intent.get("filename"):
        _remove_intent(fd)  # a exclusão não chegou a acontecer: nada a mover
        return False
    if op.get("moved_to"):
        _remove_intent(fd)  # o journal já tinha assumido; sobrou só o arquivo
        return False
    dest_slug = str(intent.get("dest_slug") or "")
    _slug_ok(dest_slug)
    if intent.get("create_new"):
        # Idempotente: cria, ou conclui uma criação interrompida, ou reutiliza a já pronta.
        job = _job_from_quarantine(fd, op)
        _create_destination(storage, str(intent.get("dest_title") or "Reunião"), job,
                            intent.get("event") if isinstance(intent.get("event"), dict) else None, slug=dest_slug)
    op["moved_to"] = {"slug": dest_slug, "job_id": uuid.uuid4().hex, "title": str(intent.get("dest_title") or dest_slug),
                      "at": _agora(), "phase": "attaching", "adopted_from_intent": True}
    _save(fd, op)
    _remove_intent(fd)
    return True


def _finish_origin(fd, slug: str, op: Dict[str, Any], dest_title: str, storage=None) -> Dict[str, Any]:
    """Journal concluído e metadata da origem: sem desfazer, e reprocessamento pedido se sobrou conteúdo."""
    from castanha.annotations import _metadata, _read, _write_json
    from castanha.recording_exclusion import _save, _sha
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    # Metadata primeiro, journal depois: uma queda no meio deixa o movimento
    # ainda "attaching" (a retomada repete esta etapa), nunca um journal
    # concluído com um metadata que ainda oferece "desfazer".
    from castanha.recording_exclusion import surviving_jobs
    metadata = _metadata(fd, slug)
    # Sobrou conteúdo na origem? Áudio restante ou transcrição preservada de
    # um áudio já apagado (`delete-recording`): nos dois casos ela é refeita.
    sobras = bool(op.get("remaining")) or bool(surviving_jobs(storage, slug, metadata))
    metadata.update(can_restore=False, restore_reason=f"Gravação movida para {dest_title}")
    if sobras:
        # `can_reprocess` vem da exclusão contando só arquivos; transcrição
        # preservada também se reprocessa, e o botão da janela depende disso.
        metadata.update(content_status="rebuilding", processing_status="pending", can_reprocess=True)
    _write_json(fd, "metadata.json", metadata)
    op["moved_to"]["phase"] = "attached"
    if sobras:
        # Mesmo pedido que o botão "Reprocessar" faria: o daemon refaz a origem sem o áudio movido.
        op["reprocess_requested"] = True
    op["after_metadata_sha256"] = _sha(_read(fd, "metadata.json"))
    _save(fd, op)
    return metadata


def move_enabled() -> bool:
    return bool((load_config().get("relocation") or {}).get("move_enabled", True))


def attach_in_progress(storage, job: Any) -> bool:
    """O movimento que trouxe este job ainda está anexando? (Só então o áudio não pode ser apagado.)

    Um job restaurado no destino pode carregar `committed=False` de um snapshot
    antigo mesmo com o movimento já encerrado na origem: aí não há o que esperar.
    """
    from castanha.annotations import _directory
    from castanha.recording_exclusion import _load
    moved_from = job.get("moved_from") if isinstance(job, dict) and isinstance(job.get("moved_from"), dict) else {}
    origem = str(moved_from.get("slug") or "")
    if not origem:
        return False
    try:
        _slug_ok(origem)
        with _directory(storage, origem) as fd:
            op = _load(fd)
    except (OSError, ValueError, KeyError, TypeError, RelocationError):
        return False
    return move_pending(op) and str(op["moved_to"].get("job_id")) == str(job.get("id"))


def move_pending(op: Any) -> bool:
    """True quando o journal registra um movimento ainda não anexado no destino."""
    return bool(op) and isinstance(op.get("moved_to"), dict) and op["moved_to"].get("phase") == "attaching"


def resume_move(slug: str, storage=None) -> bool:
    """Conclui um movimento interrompido depois do journal: anexa no destino e fecha a origem.

    Chamado pela retomada da exclusão antes de ela travar a origem. Travas na
    mesma ordem global do movimento. O journal é relido sob a trava e conferido
    com a leitura inicial: se outra execução o trocou no meio, a seleção do
    destino e das travas recomeça. Sem nada a concluir, não faz nada.
    """
    from castanha.annotations import _directory
    from castanha.recording_exclusion import _apply, _archive, _audio_hash, _load, _move_intent
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    bronze_a = storage.bronze_dir / slug
    # Intenção gravada antes da exclusão e ainda não assumida pelo journal (queda
    # entre as duas etapas): assumir sob a trava da origem, e só então seguir.
    try:
        with _directory(storage, slug) as fd:
            tem_intencao = bool(_move_intent(fd))
    except (OSError, ValueError, KeyError, TypeError):
        tem_intencao = False
    if tem_intencao:
        with _directory(storage, slug, lock=True) as fd:
            _adopt_intent(fd, storage, slug)
    for _tentativa in range(3):
        try:
            with _directory(storage, slug) as fd:
                visto = _load(fd)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if not move_pending(visto):
            return False
        dest_slug = str(visto["moved_to"].get("slug") or "")
        _slug_ok(dest_slug)
        bronze_b = storage.bronze_dir / dest_slug
        if bronze_b.is_symlink() or not bronze_b.is_dir():
            raise RelocationError("Reunião de destino do movimento não existe mais; o áudio segue na quarentena da origem")
        with ExitStack() as travas:
            for nome in sorted((slug, dest_slug)):
                if nome == slug:
                    fd = travas.enter_context(_directory(storage, slug, lock=True))
                else:
                    travas.enter_context(meeting_lock(bronze_b))
            op = _load(fd)
            if op and op.get("phase") == "applying":
                _apply(fd, storage, op)  # exclusão interrompida: a quarentena precisa existir antes de anexar
                op = _load(fd)
            if not move_pending(op):
                return False
            if op.get("id") != visto.get("id") or op["moved_to"].get("slug") != dest_slug \
                    or op["moved_to"].get("job_id") != visto["moved_to"].get("job_id"):
                continue  # o journal mudou enquanto esperávamos: recomeça com o destino certo
            new_job_id = str(op["moved_to"]["job_id"])
            title = str(op["moved_to"].get("title") or dest_slug)
            dest_meta = storage._read_bronze_metadata(dest_slug) or {}
            if new_job_id in (dest_meta.get("removed_job_ids") or []):
                # Ele excluiu a gravação movida do destino antes desta retomada: o
                # movimento aconteceu e foi desfeito lá; não recriar o áudio nem
                # exigir nada do destino.
                op["moved_to"]["removed_at_destination"] = True
            else:
                # O destino pode ter mudado desde a interrupção: mesma validação do movimento.
                _destination_guards(dest_slug, slug, storage)
                with _archive(fd, op["id"]) as archive:
                    if _audio_hash(archive, "audio") != op["audio_sha256"]:
                        raise RelocationError("Áudio da quarentena diverge do original; movimento não concluído")
                _attach(storage, dest_slug, title, bronze_b, bronze_a / ARCHIVE_DIR / op["id"] / "audio",
                        op["audio_sha256"], _job_from_quarantine(fd, op), new_job_id, bronze_a, slug, _agora())
            _finish_origin(fd, slug, op, title, storage)
            return True
    raise RelocationError("O journal do movimento mudou repetidamente; tente de novo")


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
