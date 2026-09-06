"""Fila automática com checkpoint em disco; calendário não espera a transcrição."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from castanha.durability import write_json
from castanha.storage import MeetingStorage
from castanha.sync import pending_candidates, sync_meeting


def _object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Checkpoint não é um objeto")
    return value


def drain_queue(storage=None, now=None, limit=5):
    """Uma rodada limitada. Lock global cobre inclusive reinício do daemon.

    O prazo é persistido ANTES da tentativa, portanto um processo morto também
    respeita o backoff. Recibos corrompidos são preservados para recuperação.
    """
    storage = storage or MeetingStorage()
    now = time.time() if now is None else now
    results = []
    with (storage.bronze_dir / ".sync-run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        attempted = 0
        for _, slug in pending_candidates(storage):
            if attempted >= limit:
                break
            bronze = storage.bronze_dir / slug
            receipt_path = bronze / ".sync-retry.json"
            try:
                metadata_path = bronze / "metadata.json"
                metadata = _object(metadata_path) if metadata_path.exists() else {}
                delivery = metadata.get("zinom")
                if delivery is not None and not isinstance(delivery, dict):
                    raise ValueError("Recibo Zinom inválido")
                delivery = delivery or {}
                if delivery.get("status") == "tombstoned":
                    continue
                if (delivery.get("status") == "pending"
                        and delivery.get("facts_status") == "pending_lineage" and delivery.get("remember_id")
                        and delivery.get("note_status") in (None, "ok")
                        and not any(reason in str(delivery.get("reason") or "").lower()
                                    for reason in ("token", "credencia", "desligada"))
                        and (not delivery.get("local_content_sha256") or
                             delivery["local_content_sha256"] == storage.delivery_content_sha256(slug))
                        and metadata.get("processing_status") != "pending"):
                    # Repetir remember não cria a capacidade ausente no servidor.
                    continue
                receipt = _object(receipt_path) if receipt_path.exists() else {}
                if float(receipt.get("next_attempt_at", 0)) > now:
                    continue
                attempts = min(max(int(receipt.get("attempts", 0)), 0) + 1, 20)
                delay = min(60 * 2 ** (attempts - 1), 900)
                receipt = {"attempts": attempts, "last_attempt_at": now,
                           "next_attempt_at": now + delay, "status": "running"}
                write_json(receipt_path, receipt)
                attempted += 1
                try:
                    result = sync_meeting(slug, storage)
                except Exception as exc:
                    result = {"slug": slug, "status": "error", "error_type": type(exc).__name__}
                receipt["status"] = result.get("status", "error")
                if receipt["status"] in ("ok", "tombstoned", "skipped"):
                    receipt.update(attempts=0, next_attempt_at=0)
                write_json(receipt_path, receipt)
                results.append(result)
            except (OSError, ValueError, TypeError) as exc:
                results.append({"slug": slug, "status": "error", "error_type": type(exc).__name__,
                                "reason": "Checkpoint indisponível; arquivos preservados"})
    return results


class RetryScheduler:
    """Polling de processo filho, sem thread escrevendo state.json em paralelo."""
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.child = None
        self.next_check = 0

    def tick(self, now=None, capture_status="idle", processing_pid=None):
        now = time.monotonic() if now is None else now
        if not self.enabled:
            return
        if self.child is not None:
            code = self.child.poll()
            if code is None:
                return
            if code:
                print(f"[Castanha] Rodada automática retornou {code}; pendências preservadas")
            self.child = None
        if now < self.next_check or capture_status in ("recording", "paused"):
            return
        if capture_status == "processing":
            # Só recuperar automaticamente um finalizador comprovadamente morto.
            # Estado legado sem PID não distingue processo vivo de interrompido.
            if not isinstance(processing_pid, int) or processing_pid <= 0:
                return
            try:
                os.kill(processing_pid, 0)
                return
            except ProcessLookupError:
                pass
            except OSError:
                return
        self.next_check = now + 30
        try:
            self.child = subprocess.Popen(
                [sys.executable, "-B", "-m", "castanha.retry"],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            print(f"[Castanha] Falha ao iniciar retomada ({type(exc).__name__}); nova tentativa em 30s")


def main():
    try:
        results = drain_queue()
        errors = sum(r.get("status") == "error" for r in results)
        if results:
            print(f"[Castanha] Retomada automática: {len(results)} itens, {errors} erros")
        return 1 if errors else 0
    except Exception as exc:
        print(f"[Castanha] Fila indisponível ({type(exc).__name__}); arquivos preservados")
        return 1


if __name__ == "__main__":
    sys.exit(main())
