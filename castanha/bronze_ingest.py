"""Contrato Castanha/F4. Construção pura, sem rede nem escrita de memória.

Transcrição é projeção do áudio, nunca original sonoro nem prova de presença.
O chamador persiste o pedido antes da rede e reutiliza exatamente o mesmo pedido
ao recuperar uma resposta perdida. A fronteira MCP resolve a conta autenticada.
"""
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from castanha.durability import meeting_lock, write_json
from castanha.zinom_adapter import ZinomError, tool_json


class BronzeIngestError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _capture_instant(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BronzeIngestError("Instante de captura inválido") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BronzeIngestError("Instante de captura exige fuso explícito")
    return parsed.isoformat()


def _source_timestamp(value: Any) -> dict:
    if not value:
        return {"valor": None, "origem": "sem_data"}
    try:
        if len(value) == 10:
            return {"valor": date.fromisoformat(value).isoformat(), "origem": "fonte"}
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BronzeIngestError("Data da gravação inválida") from exc
    # O formato legado guardava hora local sem fuso. Não inventar um offset,
    # sobretudo se a fila for retomada em outra máquina ou após uma viagem.
    return {"valor": parsed.isoformat() if parsed.tzinfo else parsed.date().isoformat(),
            "origem": "fonte"}


def build_transcript_request(slug: str, metadata: dict, transcript: str, *,
                             captured_at: str, account_id: str | None = None,
                             workspace: str | None = None, recording_id: str | None = None) -> dict:
    """Envelope integral + chave determinística. Sem fatos sem passagem citada."""
    if not isinstance(slug, str) or not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        raise BronzeIngestError("Identidade da reunião inválida")
    if not isinstance(metadata, dict):
        raise BronzeIngestError("Metadados inválidos")
    if not isinstance(workspace, str) or not workspace.strip() or workspace != workspace.strip() or len(workspace) > 256 or "\x00" in workspace:
        raise BronzeIngestError("Workspace de destino explícito obrigatório")
    delivery = metadata.get("zinom")
    if delivery is not None and not isinstance(delivery, dict):
        raise BronzeIngestError("Recibo Zinom inválido")
    if (delivery or {}).get("status") == "tombstoned":
        raise BronzeIngestError("Origem excluída; reingestão proibida")
    if metadata.get("processing_status") == "pending":
        raise BronzeIngestError("Transcrição ainda pendente")
    if metadata.get("transcription_provider") in ("mock", "failed"):
        raise BronzeIngestError("Transcrição simulada ou falha não entra na memória")
    if metadata.get("audio_status") == "sem_audio":
        raise BronzeIngestError("Gravação sem áudio não entra na memória")
    memory_ids = metadata.get("memory_recording_ids")
    for recording in metadata.get("recordings", []):
        if not isinstance(recording, dict):
            raise BronzeIngestError("Gravação inválida nos metadados")
        if memory_ids is not None and recording.get("id") not in memory_ids:
            continue
        if recording.get("transcription_provider") in ("mock", "failed"):
            raise BronzeIngestError("Gravação simulada ou falha incluída na transcrição")
    if not isinstance(transcript, str) or not transcript.strip() or "\x00" in transcript:
        raise BronzeIngestError("Transcrição vazia")
    # Limite conservador em bytes, compatível com o teto do servidor.
    if len(transcript.encode("utf-8")) > 8 * 1024 * 1024:
        raise BronzeIngestError("Transcrição excede limite; original preservado, sem truncamento")
    title = metadata.get("title") or "Reunião"
    if not isinstance(title, str) or len(title) > 1000 or "\x00" in title:
        raise BronzeIngestError("Título inválido")
    if not isinstance(recording_id, str) or not recording_id.strip() or len(recording_id) > 256 or "\x00" in recording_id:
        raise BronzeIngestError("Identidade nativa da gravação obrigatória")
    origin = "castanha:" + _sha(recording_id)
    envelope = {
        "schema": "bruno.wiki.bronze.documento", "versao": 1,
        "produtor": {"nome": "castanha", "versao": "0.1.0"},
        "source_type": "castanha", "source_id": origin, "workspace": workspace,
        "source_url": None, "titulo": title,
        "timestamp": _source_timestamp(metadata.get("recorded_at")),
        "fidelidade": "projecao", "texto": transcript,
        "proveniencia": {"capturado_em": _capture_instant(captured_at),
                          "sha256_texto": _sha(transcript), "referencia": "castanha",
                          "origem_id": origin},
    }
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 256 or "\x00" in account_id:
            raise BronzeIngestError("Conta explícita inválida")
        envelope["account_id"] = account_id
    # Mudanças de conteúdo ou metadados são revisões novas; retry do mesmo
    # checkpoint tem a mesma chave, mesmo depois de mudar de processo.
    payload = {"envelope": envelope, "facts": []}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"idempotency_key": "castanha:v1:" + _sha(canonical), **payload}


def revision_fingerprint(request: dict) -> str:
    identity = {**request["envelope"], "proveniencia": {
        key: value for key, value in request["envelope"]["proveniencia"].items()
        if key != "capturado_em"
    }}
    fingerprint = _sha(json.dumps(identity, sort_keys=True, ensure_ascii=False))
    return fingerprint


def prepare_transcript_upload(directory: Path, slug: str, metadata: dict, transcript: str, *,
                              captured_at: str, account_id: str | None = None,
                              workspace: str | None = None, recording_id: str | None = None) -> Path:
    """Publica checkpoint antes da rede. O chamador mantém meeting_lock.

    O relógio da tentativa não muda a identidade de uma revisão já preparada.
    Cada revisão tem arquivo próprio; uma falha não apaga recibos anteriores.
    """
    request = build_transcript_request(slug, metadata, transcript,
                                       captured_at=captured_at, account_id=account_id, workspace=workspace, recording_id=recording_id)
    checkpoint = Path(directory) / (revision_fingerprint(request) + ".json")
    if checkpoint.exists():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            instant = saved["request"]["envelope"]["proveniencia"]["capturado_em"]
            expected = build_transcript_request(slug, metadata, transcript,
                                                captured_at=instant, account_id=account_id, workspace=workspace, recording_id=recording_id)
            if saved["request"] != expected:
                raise BronzeIngestError("Checkpoint diverge do original")
        except (ValueError, KeyError, TypeError) as exc:
            raise BronzeIngestError("Checkpoint inválido; preservado sem reenviar") from exc
        return checkpoint
    write_json(checkpoint, {"request": request, "status": "prepared"})
    return checkpoint


def submit_transcript_upload(checkpoint: Path, client) -> dict:
    """Envia pedido previamente persistido, sem declarar ACK como indexação.

    Cliente é o transporte MCP existente. Falha conserva o pedido e nunca cai
    para remember, porque o legado não garante idempotência de resposta perdida.
    O chamador mantém meeting_lock e faz nova tentativa com o mesmo checkpoint.
    """
    checkpoint = Path(checkpoint)
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    request = saved["request"]
    # Verificação local do hash antes de cada envio; arquivo alterado/corrompido
    # não pode transformar uma tentativa de recuperação em outra publicação.
    canonical = json.dumps({"envelope": request["envelope"], "facts": request["facts"]},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if request["idempotency_key"] != "castanha:v1:" + _sha(canonical):
        raise BronzeIngestError("Pedido persistido corrompido; envio recusado")
    previous = saved.get("result") or {}
    if saved.get("status") in ("tombstoned", "superseded") and previous.get("status") == saved["status"]:
        return previous
    try:
        client.connect()
        lookup = saved.get("attempted") is True
        # Persistir ANTES do envio também cobre morte após aceitação sem recibo.
        saved = {**saved, "attempted": True}
        write_json(checkpoint, saved)
        try:
            payload = tool_json(client.call_tool("brain_ingest",
                {"idempotency_key": request["idempotency_key"]} if lookup else request))
        except ZinomError as exc:
            if not (lookup and exc.tool == "brain_ingest" and exc.code == "unknown_idempotency_key"):
                raise
            # A fronteira autentica novamente workspace/conta. Nunca mudar destino
            # para contornar uma chave invisível ou uma autorização revogada.
            lookup = False
            payload = tool_json(client.call_tool("brain_ingest", request))
        if (("sourceId" in payload and payload["sourceId"] != request["envelope"]["source_id"]) or
                ("sourceType" in payload and payload["sourceType"] != "castanha")):
            raise BronzeIngestError("Recibo remoto não corresponde à origem")
        if lookup and (payload.get("sourceId") != request["envelope"]["source_id"] or
                       payload.get("sourceType") != request["envelope"]["source_type"] or
                       payload.get("replay") is not True or
                       type(payload.get("attempts")) is not int or payload["attempts"] < 0 or
                       "lastError" not in payload or
                       (payload["lastError"] is not None and not isinstance(payload["lastError"], str))):
            raise BronzeIngestError("Recibo remoto não corresponde à origem")
        state = payload.get("status")
        if (payload.get("ok") is not True or
                type(payload.get("jobId")) is not int or not 0 < payload["jobId"] <= 9007199254740991 or
                type(payload.get("revisionId")) is not int or not 0 < payload["revisionId"] <= 9007199254740991 or
                type(payload.get("checkpoint")) is not int or not 0 <= payload["checkpoint"] <= 2 or
                (state == "completed" and payload["checkpoint"] != 2) or
                type(payload.get("replay")) is not bool or
                state not in ("pending", "processing", "retry", "completed", "failed", "tombstoned", "superseded")):
            raise BronzeIngestError("Recibo remoto inválido; entrega não confirmada")
        identity = saved.get("remote_identity")
        received_identity = {"job_id": payload["jobId"], "revision_id": payload["revisionId"]}
        if identity is not None and identity != received_identity:
            raise BronzeIngestError("Identidade do job mudou para a mesma chave")
        saved["remote_identity"] = received_identity
        result = {"status": "ok" if state == "completed" else
                  "tombstoned" if state == "tombstoned" else
                  "superseded" if state == "superseded" else
                  "error" if state == "failed" else "pending",
                  "ingestion": {"job_id": payload["jobId"], "revision_id": payload["revisionId"],
                                "state": state, "source_id": request["envelope"]["source_id"]}}
    except ZinomError as exc:
        deleted = exc.tool == "brain_ingest" and exc.code == "source_tombstoned"
        result = {"status": "tombstoned" if deleted else "error",
                  "error_type": type(exc).__name__,
                  "reason": "Origem excluída no servidor" if deleted else
                            "Ingestão não confirmada; pedido preservado para retomada"}
    except Exception as exc:
        # Nada de payloads, transcrições ou credenciais no relatório de erro.
        result = {"status": "error", "error_type": type(exc).__name__,
                  "reason": "Ingestão não confirmada; pedido preservado para retomada"}
    write_json(checkpoint, {**saved, "status": result["status"], "result": result})
    return result


def current_recordings(bronze: Path, metadata: dict):
    """Somente texto integral dos jobs nativos; legado sem identidade falha fechado."""
    records = metadata.get("recordings")
    if not isinstance(records, list) or not records:
        raise BronzeIngestError("Gravações nativas ausentes; original preservado")
    memory_ids = metadata.get("memory_recording_ids")
    if memory_ids is not None and (not isinstance(memory_ids, list) or
                                   any(not isinstance(i, str) for i in memory_ids)):
        raise BronzeIngestError("Seleção de gravações inválida")
    selected = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise BronzeIngestError("Gravação inválida")
        if memory_ids is not None and record.get("id") not in memory_ids:
            continue
        native_id = record.get("job_id")
        if (not isinstance(native_id, str) or not native_id or native_id in (".", "..")
                or "/" in native_id or "\\" in native_id or native_id in seen):
            raise BronzeIngestError("Identidade nativa ausente ou ambígua; original preservado")
        seen.add(native_id)
        job = json.loads((bronze / ".jobs" / (native_id + ".json")).read_bytes())
        if (job.get("id") != native_id or job.get("stage") not in ("done", "transcribed")
                or job.get("provider") in (None, "mock", "failed", "pending")
                or job.get("provider") != record.get("transcription_provider")):
            raise BronzeIngestError("Job não comprova transcrição atual da gravação")
        item_metadata = {**metadata, "recordings": [record],
                         "recorded_at": job.get("recorded_at"),
                         "transcription_provider": job["provider"]}
        selected.append((native_id, item_metadata, job.get("transcript")))
    if not selected or (memory_ids is not None and
                        set(memory_ids) != {r.get("id") for r in records if r.get("id") in memory_ids}):
        raise BronzeIngestError("Transcrição sem gravações de origem verificáveis")
    return selected


def current_requests(bronze: Path, slug: str, metadata: dict, *, workspace, account_id=None):
    instant = datetime.now(timezone.utc).isoformat()
    return [(native_id, item_metadata, text, build_transcript_request(
        slug, item_metadata, text, captured_at=instant, recording_id=native_id,
        workspace=workspace, account_id=account_id))
        for native_id, item_metadata, text in current_recordings(bronze, metadata)]


def bronze_needs_sync(bronze: Path, slug: str, metadata: dict, *, workspace, account_id=None) -> bool:
    """Um recibo antigo não conclui uma revisão alterada em disco."""
    try:
        requests = current_requests(bronze, slug, metadata, workspace=workspace, account_id=account_id)
        for _, _, _, request in requests:
            path = bronze / ".brain-ingest" / (revision_fingerprint(request) + ".json")
            saved = json.loads(path.read_bytes())
            old = saved["request"]
            canonical = json.dumps({"envelope": old["envelope"], "facts": old["facts"]},
                                   sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if old["idempotency_key"] != "castanha:v1:" + _sha(canonical):
                return True
            if revision_fingerprint(old) != revision_fingerprint(request):
                return True
            if saved.get("status") not in ("ok", "tombstoned", "superseded"):
                return True
            result = saved.get("result", {})
            if result.get("status") != saved["status"]:
                return True
            if saved["status"] == "ok":
                ingestion = result.get("ingestion", {})
                if (ingestion.get("state") != "completed" or
                        ingestion.get("source_id") != request["envelope"]["source_id"] or
                        saved.get("remote_identity") != {"job_id": ingestion.get("job_id"),
                                                        "revision_id": ingestion.get("revision_id")}):
                    return True
        return False
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return True


def ingest_current_recordings(bronze: Path, slug: str, metadata: dict, client, *,
                              workspace, account_id=None) -> dict:
    """Chamador mantém meeting_lock; lock adicional serializa a entrega e origem.

    O resultado usa `source`, campo preservado pelo storage e pelo fluxo durável.
    Destino é congelado antes da rede para não redirecionar uma resposta perdida.
    """
    directory = bronze / ".brain-ingest"
    directory.mkdir(exist_ok=True)
    with meeting_lock(directory):
        try:
            requests = current_requests(bronze, slug, metadata, workspace=workspace, account_id=account_id)
            destination = {"endpoint": client.endpoint, "workspace": workspace, "account_id": account_id,
                           "credential_sha256": _sha(client.token)}
            destination_path = directory / "destination.json"
            if destination_path.exists():
                if json.loads(destination_path.read_bytes()) != destination:
                    raise BronzeIngestError("Destino mudou; retome com a configuração original")
            else:
                write_json(destination_path, destination)
            results = []
            for native_id, item_metadata, text, request in requests:
                # Mesmo job copiado/movido para outra reunião não corre em paralelo.
                origin_lock = bronze.parent / ".brain-ingest-locks" / _sha(native_id)
                origin_lock.mkdir(parents=True, exist_ok=True)
                with meeting_lock(origin_lock):
                    path = prepare_transcript_upload(directory, slug, item_metadata, text,
                        captured_at=request["envelope"]["proveniencia"]["capturado_em"],
                        workspace=workspace, account_id=account_id, recording_id=native_id)
                    # Exclusão é da origem, inclusive quando o texto local mudou.
                    terminal = None
                    for old_path in directory.glob("*.json"):
                        old = json.loads(old_path.read_bytes())
                        if (old.get("request", {}).get("envelope", {}).get("source_id") ==
                                request["envelope"]["source_id"] and old.get("status") == "tombstoned"):
                            terminal = old.get("result")
                            if not isinstance(terminal, dict) or terminal.get("status") != "tombstoned":
                                raise BronzeIngestError("Recibo terminal corrompido")
                            break
                    result = terminal or submit_transcript_upload(path, client)
                    results.append({"checkpoint": path.name, **result})
            states = {r["status"] for r in results}
            status = ("error" if "error" in states else "pending" if "pending" in states else
                      "tombstoned" if "tombstoned" in states else
                      "superseded" if "superseded" in states else "ok")
            return {"status": status, "source": {"transport": "bronze", "revisions": results},
                    "facts_status": "none", "facts_pending": [], "facts_ingested": 0}
        except Exception as exc:
            return {"status": "error", "source": {"transport": "bronze"},
                    "reason": "Ingestão não confirmada; originais preservados para retomada",
                    "errors": [f"Ponte Bronze: {type(exc).__name__}"]}
