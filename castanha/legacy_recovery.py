"""Recuperação explícita de uma gravação legada, sem fingir um job histórico.

Não modifica áudio, transcrição ou metadata. Operação real exige os gates de
backup, destino autenticado, revisão e canário da release; não é autorun.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import uuid

from castanha.bronze_ingest import (BronzeIngestError, build_transcript_request,
                                   revision_fingerprint, submit_transcript_upload)
from castanha.durability import file_sha256, meeting_lock, write_json


def _fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise BronzeIngestError("Original deve ser arquivo regular, sem symlink")
    before = path.stat()
    digest = file_sha256(path)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise BronzeIngestError("Original mudou durante a validação")
    if after.st_size <= 0:
        raise BronzeIngestError("Original vazio")
    return {"file": path.name, "sha256": digest, "bytes": after.st_size}


def _audio_info(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                             "-of", "json", str(path)], capture_output=True, timeout=20, check=True)
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "audio":
        raise BronzeIngestError("Áudio legado ambíguo")
    if info.get("format", {}).get("format_name") != "ogg" or streams[0].get("codec_name") != "opus":
        raise BronzeIngestError("Recuperação exige o original Ogg/Opus do Castanha")
    duration = float(info["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0 or streams[0].get("channels") not in (1, 2):
        raise BronzeIngestError("Duração ou canais inválidos")
    return {"mime": "audio/ogg", "codec": "opus", "channels": streams[0]["channels"],
            "duration_seconds": duration}


def _source(bronze):
    # Uma única gravação é requisito deliberado: não atribuir uma transcrição
    # agregada a um áudio individual por posição ou nome parecido.
    metadata_path = bronze / "metadata.json"
    metadata_fp = _fingerprint(metadata_path)
    metadata = json.loads(metadata_path.read_bytes())
    records = metadata.get("recordings")
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict)
            or records[0].get("id") != "audio.ogg" or records[0].get("job_id") is not None):
        raise BronzeIngestError("Recuperação exige uma única gravação legada explícita")
    if metadata.get("processing_status") == "pending" or metadata.get("audio_status") == "sem_audio":
        raise BronzeIngestError("Gravação pendente ou sem áudio")
    delivery = metadata.get("zinom") or {}
    if not isinstance(delivery, dict) or delivery.get("status") in ("tombstoned", "superseded") or delivery.get("remember_id"):
        raise BronzeIngestError("Entrega anterior exige reconciliação, não nova migração")
    provider = metadata.get("transcription_provider")
    if provider not in ("groq", "deepgram", "vps_whisper_large_v3"):
        raise BronzeIngestError("Provedor legado ausente, simulado ou desconhecido")
    if records[0].get("transcription_provider") in ("mock", "failed", "pending"):
        raise BronzeIngestError("Gravação legada inválida")
    ingest = bronze / ".brain-ingest"
    # Uma síntese recusada antes de validar a origem deixa só o lock vazio.
    # Qualquer outro conteúdo continua sendo evidência de fluxo nativo.
    has_ingest = ingest.is_symlink() or (ingest.exists() and (
        not ingest.is_dir() or any(path.name != ".processing.lock" or
                                  path.is_symlink() or not path.is_file() or path.stat().st_size
                                  for path in ingest.iterdir())))
    if (bronze / ".jobs").exists() or has_ingest:
        raise BronzeIngestError("Já existe fluxo nativo; recuperação legada não se aplica")
    audio = bronze / "audio.ogg"
    audio_fp = _fingerprint(audio)
    audio_info = _audio_info(audio)
    text_path = bronze / "transcript_raw.txt"
    text_fp = _fingerprint(text_path)
    if text_fp["bytes"] > 8 * 1024 * 1024:
        raise BronzeIngestError("Transcrição excede limite, sem truncamento")
    text = text_path.read_bytes().decode("utf-8")
    if not text.strip() or "\x00" in text:
        raise BronzeIngestError("Transcrição inválida")
    if (audio_fp != _fingerprint(audio) or text_fp != _fingerprint(text_path)
            or metadata_fp != _fingerprint(metadata_path)):
        raise BronzeIngestError("Originais mudaram durante a leitura")
    return {"audio": {**audio_fp, **audio_info}, "transcript": text_fp, "metadata": metadata_fp,
            "legacy_provider_global_unverified": provider, "historical_job_id": None}, metadata, text


def _validate(bronze, manifest):
    if manifest.get("schema") != "castanha.legacy-recovery.v1":
        raise BronzeIngestError("Manifesto inválido")
    if str(uuid.UUID(manifest["migration_id"])) != manifest["migration_id"]:
        raise BronzeIngestError("Identidade de migração inválida")
    current, metadata, text = _source(bronze)
    if current != manifest["source"]:
        raise BronzeIngestError("Original diverge do manifesto; recuperação interrompida")
    return metadata, text


def prepare_legacy_manifest(bronze):
    """Somente manifesto local; nenhum cliente, token ou destino é necessário."""
    bronze = Path(bronze)
    if bronze.is_symlink() or not bronze.is_dir():
        raise BronzeIngestError("Diretório Bronze inválido")
    with meeting_lock(bronze):
        directory = bronze / ".legacy-recovery"
        if directory.is_symlink():
            raise BronzeIngestError("Diretório de recuperação inválido")
        directory.mkdir(exist_ok=True)
        path = directory / "manifest.json"
        if path.exists():
            manifest = json.loads(path.read_bytes())
            _validate(bronze, manifest)
            return manifest
        if any((directory / name).exists() for name in ("destination.json", "upload.json", "uploads")):
            raise BronzeIngestError("Manifesto perdido após preparação de envio; recuperar identidade original")
        source, _, _ = _source(bronze)
        manifest = {"schema": "castanha.legacy-recovery.v1", "migration_id": str(uuid.uuid4()),
                    "prepared_at": datetime.now(timezone.utc).isoformat(), "source": source}
        write_json(path, manifest)
        return manifest


def legacy_request(manifest, metadata, text, *, workspace, account_id=None):
    migration_id = manifest["migration_id"]
    request = build_transcript_request("legacy-recovery", metadata, text,
        captured_at=manifest["prepared_at"], workspace=workspace, account_id=account_id,
        recording_id="migration:" + migration_id)
    envelope = request["envelope"]
    envelope["produtor"]["nome"] = "castanha-legacy-recovery"
    # O domínio migration: é distinto dos IDs nativos. O formato externo
    # continua o contrato Castanha validado pelo ledger, sem inventar job_id.
    envelope["proveniencia"].update(origem_id=envelope["source_id"],
        referencia="audio-sha256:" + manifest["source"]["audio"]["sha256"] + ";migration:" + migration_id)
    canonical = json.dumps({"envelope": envelope, "facts": []}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    request["idempotency_key"] = "castanha:v1:" + hashlib.sha256(canonical.encode()).hexdigest()
    return request


def submit_legacy_recovery(bronze, client, *, workspace, account_id=None):
    """Chamada explícita; manifesto existente, destino congelado e replay F4."""
    bronze = Path(bronze)
    if bronze.is_symlink() or not bronze.is_dir():
        raise BronzeIngestError("Diretório Bronze inválido")
    with meeting_lock(bronze):
        directory = bronze / ".legacy-recovery"
        if directory.is_symlink():
            raise BronzeIngestError("Diretório de recuperação inválido")
        manifest = json.loads((directory / "manifest.json").read_bytes())
        metadata, text = _validate(bronze, manifest)
        request = legacy_request(manifest, metadata, text, workspace=workspace, account_id=account_id)
        destination = {"endpoint": client.endpoint, "workspace": workspace, "account_id": account_id,
                       "credential_sha256": hashlib.sha256(client.token.encode()).hexdigest(),
                       "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True,
                           ensure_ascii=False).encode()).hexdigest()}
        target = directory / "destination.json"
        uploads = directory / "uploads"
        if uploads.is_symlink() or (directory / "upload.json").exists():
            raise BronzeIngestError("Layout de recuperação inválido ou anterior; reconciliação obrigatória")
        checkpoint = uploads / (revision_fingerprint(request) + ".json")
        if uploads.exists() and not target.exists():
            raise BronzeIngestError("Destino perdido; recuperar vínculo original antes da rede")
        if target.exists():
            saved_destination = json.loads(target.read_bytes())
            if (type(saved_destination.get("attempted")) is not bool or
                    saved_destination.get("destination") != destination):
                raise BronzeIngestError("Destino mudou; reconciliação obrigatória")
            if saved_destination["attempted"] and not checkpoint.exists():
                raise BronzeIngestError("Checkpoint perdido após tentativa; recuperar recibo original")
        else:
            write_json(target, {"destination": destination, "attempted": False})
        uploads.mkdir(exist_ok=True)
        if checkpoint.exists():
            if json.loads(checkpoint.read_bytes()).get("request") != request:
                raise BronzeIngestError("Pedido congelado diverge do original")
        else:
            write_json(checkpoint, {"request": request, "status": "prepared"})
        # Evidência independente precede até connect(): perder o recibo depois
        # de uma tentativa nunca autoriza reconstruir um envio novo.
        write_json(target, {"destination": destination, "attempted": True})
        return submit_transcript_upload(checkpoint, client)


def legacy_recovery_pending(bronze):
    """Inventário sem autorizar migração nova ou reescrever originais."""
    from castanha.bronze_ingest import _verify_terminal_receipts
    directory = Path(bronze) / ".legacy-recovery"
    try:
        destination = directory / "destination.json"
        if not destination.exists():
            return (directory / "uploads").exists()
        if json.loads(destination.read_bytes()).get("attempted") is not True:
            return False
        receipts = _verify_terminal_receipts(directory / "uploads", {})
        return not receipts or any(r.get("status") not in ("ok", "tombstoned", "superseded")
                                   for r in receipts.values())
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return True  # O executor reporta corrupção sem sobrescrever o original.


def legacy_delivery_projection(bronze):
    """Lê evidência de entrega sem alterar metadata, estado, originais ou recibos.

    Projeta o recibo da transcrição congelada; não revalida nem relê o áudio a
    cada atualização da UI. A validação sonora continua no executor de envio.
    """
    from castanha.bronze_ingest import _verify_terminal_receipts
    bronze = Path(bronze)
    directory = bronze / ".legacy-recovery"
    if not directory.exists() and not directory.is_symlink():
        return None
    try:
        paths = [bronze, directory, directory / "manifest.json", directory / "destination.json",
                 directory / "uploads"]
        if any(path.is_symlink() for path in paths):
            raise BronzeIngestError("Recuperação inválida")
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if (manifest.get("schema") != "castanha.legacy-recovery.v1" or
                str(uuid.UUID(manifest["migration_id"])) != manifest["migration_id"]):
            raise BronzeIngestError("Manifesto inválido")
        target = directory / "destination.json"
        if not target.exists() and not (directory / "uploads").exists():
            return {"status": "pending", "receipt_source": "legacy-recovery"}
        destination = json.loads(target.read_bytes())
        frozen = destination["destination"]
        if (type(destination.get("attempted")) is not bool or
                frozen["manifest_sha256"] != hashlib.sha256(json.dumps(manifest, sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest()):
            raise BronzeIngestError("Destino ou manifesto divergente")
        # Somente os pequenos originais textuais. Nunca executar ffprobe ou
        # reler dezenas de MB de áudio no polling de status.
        contents = {}
        for kind, name in (("metadata", "metadata.json"), ("transcript", "transcript_raw.txt")):
            path = bronze / name
            if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                raise BronzeIngestError("Original textual inválido")
            raw = path.read_bytes()
            if {"file": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} != manifest["source"][kind]:
                raise BronzeIngestError("Original textual diverge")
            contents[kind] = raw
        metadata = json.loads(contents["metadata"])
        request = legacy_request(manifest, metadata, contents["transcript"].decode("utf-8"),
                                 workspace=frozen["workspace"], account_id=frozen["account_id"])
        uploads = directory / "uploads"
        if any(path.is_symlink() for path in uploads.iterdir()):
            raise BronzeIngestError("Recibo inválido")
        receipts = _verify_terminal_receipts(uploads, {})
        name = revision_fingerprint(request) + ".json"
        if set(receipts) != {name} or receipts[name]["request"] != request:
            raise BronzeIngestError("Recibo ausente ou pedido divergente")
        saved = receipts[name]
        if destination["attempted"] is not True:
            raise BronzeIngestError("Tentativa não comprovada")
        result = saved.get("result") or {}
        status = saved.get("status")
        if status == "ok":
            ingestion = result.get("ingestion") or {}
            if (saved.get("attempted") is not True or ingestion.get("state") != "completed" or
                    any(type(ingestion.get(key)) is not int or not 0 < ingestion[key] <= 9007199254740991
                        for key in ("job_id", "revision_id"))):
                raise BronzeIngestError("Conclusão remota não comprovada")
            return {"status": "ok", "ingestion": ingestion, "receipt_source": "legacy-recovery"}
        if status in ("tombstoned", "superseded"):
            return {"status": status, "receipt_source": "legacy-recovery"}
        if status in ("prepared", "pending"):
            return {"status": "pending", "receipt_source": "legacy-recovery"}
        raise BronzeIngestError("Entrega não confirmada")
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"status": "error", "receipt_source": "legacy-recovery",
                "reason": "Recibo legado não confirma a entrega; originais preservados"}


def resume_legacy_recovery(bronze, config):
    """Retoma destino já autorizado; submit valida hash, identidade e lock."""
    from castanha.zinom_adapter import ZinomMcpClient
    try:
        directory = Path(bronze) / ".legacy-recovery"
        if directory.is_symlink():
            raise BronzeIngestError("Diretório de recuperação inválido")
        destination = directory / "destination.json"
        if not destination.exists() and not (directory / "uploads").exists():
            return {"status": "pending", "reason": "Recuperação legada requer envio explícito inicial"}
        if json.loads(destination.read_bytes()).get("attempted") is not True:
            return {"status": "pending", "reason": "Recuperação legada ainda não iniciada"}
        if config.get("enabled") is not True or config.get("bronze_ingest_enabled") is not True:
            return {"status": "pending", "reason": "Ingestão Bronze desligada"}
        client = ZinomMcpClient(config.get("endpoint", "https://zinom.ai/mcp"), config.get("token", ""))
        return submit_legacy_recovery(bronze, client, workspace=config.get("workspace"),
                                      account_id=config.get("account_id"))
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__,
                "reason": "Recuperação não confirmada; manifesto e originais preservados"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepara manifesto legado, sem enviar ao Zinom")
    parser.add_argument("bronze", type=Path)
    args = parser.parse_args()
    result = prepare_legacy_manifest(args.bronze)
    print(json.dumps({"status": "manifest_ready", "migration_id": result["migration_id"],
                      "sent": False}))
