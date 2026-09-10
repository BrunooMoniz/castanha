"""Cota diária conhecida persiste entre processos, isolada por credencial."""
import hashlib
import math
import time
from email.utils import parsedate_to_datetime

from castanha.config import get_state_dir
from castanha.secure_io import ensure_private_dir, read_private_json, write_private_json


def _path(api_key):
    directory = ensure_private_dir(get_state_dir() / "llm" / "quota")
    identity = hashlib.sha256(api_key.encode()).hexdigest()
    return directory / f"groq-{identity}.json"


def blocked_until(api_key):
    saved = read_private_json(_path(api_key))
    if saved is None:
        return None
    if not isinstance(saved, dict):
        raise ValueError("Invalid quota checkpoint")
    until = saved.get("until")
    if (saved.get("version") != 1 or saved.get("reason") != "daily_quota"
            or type(until) not in (int, float) or not math.isfinite(until)):
        raise ValueError("Invalid quota checkpoint")
    return until if until > time.time() else None


def record_daily_quota(api_key, retry_after=None):
    now = time.time()
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(retry_after)
            delay = date.timestamp() - now if date.tzinfo else 0
        except (TypeError, ValueError, OverflowError):
            delay = 0
    if not math.isfinite(delay) or delay <= 0:
        # Sem prazo fornecido, não sondar a cota diariamente esgotada.
        # Outros provedores configurados continuam livres para retomar.
        delay = 86400
    until = now + delay
    write_private_json(_path(api_key), {"version": 1, "reason": "daily_quota",
                                      "observed_at": now, "until": until})
    return until
