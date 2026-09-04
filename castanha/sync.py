"""Reenviar uma reunião já gravada para o Zinom.

A ingestão original roda uma vez, no fim da gravação. Se o hub estava fora do
ar, sem rede ou com a cota estourada, aquilo se perdia: os arquivos ficavam em
disco e a memória durável nunca recebia nada. Este módulo é a segunda chance,
e ele é a resposta à pergunta "dá para forçar a sincronização?".

Reenviar é seguro: a nota é EDITADA pelo id que ficou gravado no metadata do
Bronze, e `brain_fact` supersede o fato do mesmo par sujeito-predicado em vez
de duplicar.
"""

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from castanha.storage import MeetingStorage
from castanha.zinom_adapter import ZinomAdapter


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def meeting_needs_sync(metadata: Dict[str, Any]) -> bool:
    """Precisa de sync quando nunca foi, ou quando a última tentativa falhou."""
    z = metadata.get("zinom") or {}
    return z.get("status") not in ("ok", "skipped")


def sync_meeting(slug: str, storage: Optional[MeetingStorage] = None) -> Dict[str, Any]:
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
    resultado = ZinomAdapter().ingest_meeting(metadata, silver, gold, previous_remember_id=anterior)

    remember = resultado.get("remember") or {}
    metadata["zinom"] = {
        "status": resultado.get("status", "error"),
        "remember_id": remember.get("id") or anterior,
        "facts_ingested": resultado.get("facts_ingested", 0),
        "errors": resultado.get("errors", []),
        "reason": resultado.get("reason"),
        "synced_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"slug": slug, **metadata["zinom"]}


def sync_pending(limit: int = 20, storage: Optional[MeetingStorage] = None) -> List[Dict[str, Any]]:
    """Reenvia tudo que ainda não entrou, da mais recente para a mais antiga."""
    storage = storage or MeetingStorage()
    saida = []
    for nota in storage.list_recent_meetings(limit=limit):
        metadata = _read_json(storage.bronze_dir / nota["slug"] / "metadata.json")
        if meeting_needs_sync(metadata):
            saida.append(sync_meeting(nota["slug"], storage))
    return saida
