"""Reenviar uma reunião já gravada para o Zinom.

A ingestão original roda uma vez, no fim da gravação. Se o hub estava fora do
ar, sem rede ou com a cota estourada, aquilo se perdia: os arquivos ficavam em
disco e a memória durável nunca recebia nada. Este módulo é a segunda chance,
e ele é a resposta à pergunta "dá para forçar a sincronização?".

Reenviar é seguro: a nota é EDITADA pelo id que ficou gravado no metadata do
Bronze. Fatos atômicos ficam pendentes até haver suporte a linhagem no servidor.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from castanha.storage import MeetingStorage
from castanha.config import load_config
from castanha.durability import meeting_lock, write_json
from castanha.zinom_adapter import ZinomAdapter
from castanha.state import StateManager


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def meeting_needs_sync(metadata: Dict[str, Any]) -> bool:
    """Precisa de sync quando nunca foi, ou quando a última tentativa falhou."""
    z = metadata.get("zinom") or {}
    if not isinstance(z, dict):
        return True
    if z.get("status") in ("tombstoned", "superseded"):
        return False
    if metadata.get("processing_status") == "pending":
        return True
    if z.get("status") == "skipped":
        # Legado: skipped sem motivo representava descarte deliberado.
        reason = str(z.get("reason") or "").lower()
        return "token" in reason or "credencia" in reason or "desligada" in reason
    return z.get("status") != "ok"


def _finalizer_status(state, slug):
    if state.get("status") != "processing" or state.get("capture_slug") != slug:
        return None
    pid = state.get("processing_pid")
    if type(pid) is not int or pid <= 0:
        return "protected"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except (OSError, OverflowError):
        return "protected"
    return "protected"


def _reconcile_finished_capture_locked(slug, storage, state_mgr=None):
    """Chamador mantém meeting_lock; só ESRCH permite publicar idle."""
    state_mgr = state_mgr or StateManager()
    state = state_mgr.read()
    if _finalizer_status(state, slug) != "dead":
        return False
    bronze = storage.bronze_dir / slug
    jobs = [_read_json(p) for p in (bronze / ".jobs").glob("*.json")]
    if not jobs or any(job.get("stage") != "done" for job in jobs):
        return False
    metadata = _read_json(bronze / "metadata.json")
    state_mgr.write({"status": "idle", "pid": None, "processing_pid": None, "audio_path": None,
                     "capture_slug": None, "capture_job_id": None, "current_meeting": None,
                     "elapsed_seconds": 0,
                     "last_result": {"slug": slug, "title": metadata.get("title", slug),
                                     "zinom": metadata.get("zinom") or {},
                                     "bronze_dir": str(bronze),
                                     "silver_file": str(storage.silver_dir / f"{slug}.md"),
                                     "gold_file": str(storage.gold_dir / f"{slug}.json")}})
    return True


def reconcile_finished_capture(storage):
    """Também alcança entrega concluída/tombstone, fora do inventário de retry."""
    state_mgr = StateManager()
    state = state_mgr.read()
    slug = state.get("capture_slug")
    if not isinstance(slug, str) or not slug or slug in (".", "..") or Path(slug).name != slug:
        return
    if _finalizer_status(state, slug) != "dead":
        return
    bronze = storage.bronze_dir / slug
    if bronze.is_dir():
        with meeting_lock(bronze):
            if _reconcile_finished_capture_locked(slug, storage, state_mgr):
                return slug


def sync_meeting(slug: str, storage: Optional[MeetingStorage] = None) -> Dict[str, Any]:
    storage = storage or MeetingStorage()
    if not isinstance(slug, str) or not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        return {"slug": slug, "status": "error", "errors": ["Identidade da reunião inválida"]}
    bronze = storage.bronze_dir / slug
    if not bronze.exists():
        return {"slug": slug, "status": "error", "errors": [f"Reunião {slug} não existe no Bronze"]}
    # Manifestos legados congelam inclusive metadata.json. Não passar pelo
    # escritor nativo: somente retomar uma recuperação já iniciada explicitamente.
    recovery = bronze / ".legacy-recovery"
    # O destino só é gravado após validar a entrega legada. O diretório
    # sozinho pode sobrar de uma síntese recusada antes dessa validação.
    if (recovery.exists() or recovery.is_symlink()) and not (bronze / ".brain-ingest" / "destination.json").exists():
        from castanha.legacy_recovery import resume_legacy_recovery
        return {"slug": slug, **resume_legacy_recovery(bronze, load_config().get("zinom", {}))}
    with meeting_lock(bronze):
        metadata = _read_json(bronze / "metadata.json")
        if metadata.get("zinom") is not None and not isinstance(metadata["zinom"], dict):
            return {"slug": slug, "status": "error", "errors": ["Recibo Zinom inválido; arquivos preservados"]}
        jobs = [_read_json(p) for p in (bronze / ".jobs").glob("*.json")]
        if any(job.get("stage") != "done" for job in jobs):
            from castanha.engine import CastanhaEngine
            engine = CastanhaEngine()
            engine.storage = storage
            result = engine._process_pending_locked(slug)["result"]
            _reconcile_finished_capture_locked(slug, storage, engine.state_mgr)
            return {"slug": slug, **result["zinom"]}
        if jobs:
            if _finalizer_status(StateManager().read(), slug) == "protected":
                from castanha.i18n import t
                return {"slug": slug, "status": "pending", "reason": t("sync.reason_finalizer_active")}
            reconciled = _reconcile_finished_capture_locked(slug, storage)
            delivery = metadata.get("zinom") or {}
            delivered_current = (delivery.get("note_status") == "ok" and delivery.get("remember_id")
                                 and delivery.get("local_content_sha256") == storage.delivery_content_sha256(slug))
            # O recibo legado pode encerrar sua nota, não a validação por origem
            # da ponte Bronze (revisão, destino e tombstone podem ter mudado).
            source = delivery.get("source") or {}
            bronze_transport = (load_config().get("zinom", {}).get("bronze_ingest_enabled") is True
                                or (bronze / ".brain-ingest").exists()
                                or (isinstance(source, dict) and source.get("transport") == "bronze"))
            if not bronze_transport and (not meeting_needs_sync(metadata) or (reconciled and delivered_current)):
                return {"slug": slug, **(metadata.get("zinom") or {})}
        return _sync_meeting_locked(slug, storage)


def _sync_meeting_locked(slug: str, storage: Optional[MeetingStorage] = None) -> Dict[str, Any]:
    storage = storage or MeetingStorage()
    bronze = storage.bronze_dir / slug
    metadata_file = bronze / "metadata.json"

    if not metadata_file.exists():
        return {"slug": slug, "status": "error", "errors": [f"Reunião {slug} não existe no Bronze"]}

    metadata = _read_json(metadata_file)
    if not metadata:
        return {"slug": slug, "status": "error", "errors": ["Metadados inválidos; arquivos preservados"]}
    if metadata.get("zinom") is not None and not isinstance(metadata["zinom"], dict):
        return {"slug": slug, "status": "error", "errors": ["Recibo Zinom inválido; arquivos preservados"]}
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
            from castanha.i18n import audio_status_message
            from castanha.audio import classify_audio, measure_channel_levels
            levels = measure_channel_levels(audio_file, mode=metadata.get("mode", "dual"))
            st = classify_audio(levels)
            metadata["audio_status"] = st
            metadata["audio_diagnostico"] = audio_status_message(st)

    silver = silver_file.read_text(encoding="utf-8") if silver_file.exists() else ""
    gold = _read_json(gold_file)

    anterior = (metadata.get("zinom") or {}).get("remember_id")
    write_json(metadata_file, metadata)
    resultado = ZinomAdapter().ingest_meeting(
        metadata, silver, gold, previous_remember_id=anterior, bronze_directory=bronze,
        on_remember=lambda receipt: storage.record_zinom_result(slug, receipt),
    )
    storage.record_zinom_result(slug, resultado)
    return {"slug": slug, **storage._read_bronze_metadata(slug)["zinom"]}


def pending_candidates(storage: MeetingStorage):
    """Inventário comum ao comando manual e à retomada automática."""
    reconciled_slug = reconcile_finished_capture(storage)
    candidates = []
    z_cfg = load_config().get("zinom", {})
    for bronze in storage.bronze_dir.iterdir():
        if not bronze.is_dir():
            continue
        recovery = bronze / ".legacy-recovery"
        if (recovery.exists() or recovery.is_symlink()) and not (bronze / ".brain-ingest" / "destination.json").exists():
            from castanha.legacy_recovery import legacy_recovery_pending
            if legacy_recovery_pending(bronze):
                candidates.append(("", bronze.name))
            continue
        path = bronze / "metadata.json"
        if not path.exists() and not any((bronze / ".jobs").glob("*.json")):
            continue
        metadata = _read_json(path)
        delivery = metadata.get("zinom")
        if delivery is not None and not isinstance(delivery, dict):
            # O executor reportará o recibo inválido sem sobrescrevê-lo.
            candidates.append(("", bronze.name))
            continue
        delivery = delivery or {}
        from castanha.bronze_ingest import has_origin_receipts
        if delivery.get("status") in ("tombstoned", "superseded") and not has_origin_receipts(delivery):
            continue
        if (bronze.name == reconciled_slug and delivery.get("note_status") == "ok"
                and delivery.get("remember_id")
                and delivery.get("local_content_sha256") == storage.delivery_content_sha256(bronze.name)):
            continue
        unfinished = any(_read_json(p).get("stage") != "done" for p in (path.parent / ".jobs").glob("*.json"))
        bronze_pending = False
        source = delivery.get("source") or {}
        if z_cfg.get("bronze_ingest_enabled", False) is True or (bronze / ".brain-ingest").exists() or (
                isinstance(source, dict) and source.get("transport") == "bronze"):
            from castanha.bronze_ingest import bronze_needs_sync
            silver_file = storage.silver_dir / f"{bronze.name}.md"
            bronze_pending = bronze_needs_sync(bronze, bronze.name, metadata,
                workspace=z_cfg.get("workspace"), account_id=z_cfg.get("account_id"),
                endpoint=z_cfg.get("endpoint", "https://zinom.ai/mcp"), token=z_cfg.get("token", ""),
                silver_text=silver_file.read_text(encoding="utf-8") if silver_file.exists() else "",
                gold=_read_json(storage.gold_dir / f"{bronze.name}.json"))
        if meeting_needs_sync(metadata) or unfinished or bronze_pending:
            synced_at = delivery.get("synced_at")
            candidates.append((synced_at if isinstance(synced_at, str) else "", path.parent.name))
    candidates.sort()
    return candidates


def sync_pending(limit: Optional[int] = None, storage: Optional[MeetingStorage] = None) -> List[Dict[str, Any]]:
    """Varre o Bronze inteiro; uma falha não descarta o restante da fila."""
    storage = storage or MeetingStorage()
    candidates = pending_candidates(storage)
    if limit is not None:
        candidates = candidates[:max(0, limit)]
    results = []
    for _, slug in candidates:
        try:
            results.append(sync_meeting(slug, storage))
        except Exception as exc:
            # Não publicar traceback com paths, transcrição ou credenciais.
            results.append({"slug": slug, "status": "error",
                            "errors": [f"Falha ao retomar reunião ({type(exc).__name__}); original preservado"]})
    return results
