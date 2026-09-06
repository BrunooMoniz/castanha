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

from castanha.bronze_ingest import BronzeIngestError, build_transcript_request, submit_transcript_upload
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
    if (bronze / ".jobs").exists() or (bronze / ".brain-ingest").exists():
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
        if (directory / "destination.json").exists() or (directory / "upload.json").exists():
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
    envelope["source_id"] = "castanha-legacy:" + migration_id
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
        checkpoint = directory / "upload.json"
        if checkpoint.exists() and not target.exists():
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
        if checkpoint.exists():
            if json.loads(checkpoint.read_bytes()).get("request") != request:
                raise BronzeIngestError("Pedido congelado diverge do original")
        else:
            write_json(checkpoint, {"request": request, "status": "prepared"})
        # Evidência independente precede até connect(): perder o recibo depois
        # de uma tentativa nunca autoriza reconstruir um envio novo.
        write_json(target, {"destination": destination, "attempted": True})
        return submit_transcript_upload(checkpoint, client)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepara manifesto legado, sem enviar ao Zinom")
    parser.add_argument("bronze", type=Path)
    args = parser.parse_args()
    result = prepare_legacy_manifest(args.bronze)
    print(json.dumps({"status": "manifest_ready", "migration_id": result["migration_id"],
                      "sent": False}))
