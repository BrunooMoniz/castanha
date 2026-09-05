"""Reenviar uma reunião já gravada para o Zinom.

A ingestão original roda uma vez, no fim da gravação. Se o hub estava fora do
ar, sem rede ou com a cota estourada, aquilo se perdia: os arquivos ficavam em
disco e a memória durável nunca recebia nada. Este módulo é a segunda chance,
e ele é a resposta à pergunta "dá para forçar a sincronização?".

Reenviar é seguro: a nota é EDITADA pelo id que ficou gravado no metadata do
Bronze. Fatos atômicos ficam pendentes até haver suporte a linhagem no servidor.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from castanha.storage import MeetingStorage
from castanha.durability import meeting_lock, write_json
from castanha.zinom_adapter import ZinomAdapter


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def meeting_needs_sync(metadata: Dict[str, Any]) -> bool:
    """Precisa de sync quando nunca foi, ou quando a última tentativa falhou."""
    z = metadata.get("zinom") or {}
    if z.get("status") == "tombstoned":
        return False
    if metadata.get("processing_status") == "pending":
        return True
    if z.get("status") == "skipped":
        # Legado: skipped sem motivo representava descarte deliberado.
        reason = (z.get("reason") or "").lower()
        return "token" in reason or "credencia" in reason or "desligada" in reason
    return z.get("status") != "ok"


def sync_meeting(slug: str, storage: Optional[MeetingStorage] = None) -> Dict[str, Any]:
    storage = storage or MeetingStorage()
    bronze = storage.bronze_dir / slug
    if not bronze.exists():
        return {"slug": slug, "status": "error", "errors": [f"Reunião {slug} não existe no Bronze"]}
    jobs = [_read_json(p) for p in (bronze / ".jobs").glob("*.json")]
    if any(job.get("stage") != "done" for job in jobs):
        from castanha.engine import CastanhaEngine
        engine = CastanhaEngine()
        engine.storage = storage
        result = engine.process_pending(slug)["result"]
        state = engine.state_mgr.read()
        if state.get("status") == "processing" and state.get("capture_slug") == slug:
            engine.state_mgr.write({"status": "idle", "pid": None, "audio_path": None,
                                    "capture_slug": None, "capture_job_id": None,
                                    "current_meeting": None, "last_result": result})
        return {"slug": slug, **result["zinom"]}
    with meeting_lock(bronze):
        return _sync_meeting_locked(slug, storage)


def _sync_meeting_locked(slug: str, storage: Optional[MeetingStorage] = None) -> Dict[str, Any]:
    storage = storage or MeetingStorage()
    bronze = storage.bronze_dir / slug
    metadata_file = bronze / "metadata.json"

    if not metadata_file.exists():
        return {"slug": slug, "status": "error", "errors": [f"Reunião {slug} não existe no Bronze"]}

    metadata = _read_json(metadata_file)
    silver_file = storage.silver_dir / f"{slug}.md"
    gold_file = storage.gold_dir / f"{slug}.json"

    # Reunião legada gravada antes da checagem de áudio: reavalia se houver áudio
    if "audio_status" not in metadata:
        audio_file = None
        if metadata.get("bronze_audio_file") and Path(metadata["bronze_audio_file"]).exists():
            audio_file = Path(metadata["bronze_audio_file"])
        elif (bronze / "audio.ogg").exists():
            audio_file = bronze / "audio.ogg"
        else:
            candidates = list(bronze.glob("audio.*"))
            if candidates:
                audio_file = candidates[0]

        if audio_file:
            from castanha.audio import AUDIO_STATUS_MESSAGES, classify_audio, measure_channel_levels
            levels = measure_channel_levels(audio_file, mode=metadata.get("mode", "dual"))
            st = classify_audio(levels)
            metadata["audio_status"] = st
            metadata["audio_diagnostico"] = AUDIO_STATUS_MESSAGES.get(st, "")

    silver = silver_file.read_text(encoding="utf-8") if silver_file.exists() else ""
    gold = _read_json(gold_file)

    anterior = (metadata.get("zinom") or {}).get("remember_id")
    write_json(metadata_file, metadata)
    resultado = ZinomAdapter().ingest_meeting(
        metadata, silver, gold, previous_remember_id=anterior,
        on_remember=lambda receipt: storage.record_zinom_result(slug, receipt),
    )
    storage.record_zinom_result(slug, resultado)
    return {"slug": slug, **storage._read_bronze_metadata(slug)["zinom"]}


def sync_pending(limit: Optional[int] = None, storage: Optional[MeetingStorage] = None) -> List[Dict[str, Any]]:
    """Varre o Bronze inteiro, incluindo jobs sem Silver. Limite conta pendências."""
    storage = storage or MeetingStorage()
    candidates = []
    for bronze in storage.bronze_dir.iterdir():
        if not bronze.is_dir():
            continue
        path = bronze / "metadata.json"
        if not path.exists() and not any((bronze / ".jobs").glob("*.json")):
            continue
        metadata = _read_json(path)
        unfinished = any(_read_json(p).get("stage") != "done" for p in (path.parent / ".jobs").glob("*.json"))
        if meeting_needs_sync(metadata) or unfinished:
            candidates.append(((metadata.get("zinom") or {}).get("synced_at") or "", path.parent.name))
    candidates.sort()
    if limit is not None:
        candidates = candidates[:max(0, limit)]
    return [sync_meeting(slug, storage) for _, slug in candidates]
