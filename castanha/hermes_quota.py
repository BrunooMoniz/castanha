"""Leitura de cota já conhecida pelo Hermes, sem refresh ou chamada ao provedor."""

import datetime
import json
import math


def _epoch(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        try:
            date = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
            number = date.timestamp()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number > 100_000_000_000 else number


def quota_state(auth, provider, now):
    """Bloqueia só quando todas as credenciais conhecidas estão esgotadas.

    O reset conhecido permite retomar. Sem reset, a marca persiste até o
    próprio Hermes registrar recuperação; não inventamos uma chamada de prova.
    Nenhum dado da credencial integra a resposta.
    """
    pool = auth.get("credential_pool", {})
    rows = pool.get(provider, []) if isinstance(pool, dict) else []
    if not isinstance(rows, list) or not rows:
        return {"blocked": False}
    resets = []
    for row in rows:
        if isinstance(row, dict) and row.get("last_status") == "dead":
            continue
        if not isinstance(row, dict) or row.get("last_status") != "exhausted":
            return {"blocked": False}
        reset = _epoch(row.get("last_error_reset_at"))
        if reset is not None and reset <= now:
            return {"blocked": False}
        if reset is not None:
            resets.append(reset)
    return {"blocked": True, "reset_at": min(resets) if resets else None}


def remote_command(provider):
    """Envia apenas código stdlib, devolve apenas estado sanitizado via SSH."""
    import inspect
    import shlex

    script = (
        "import datetime,json,math,os,sys,time\nfrom pathlib import Path\n"
        + inspect.getsource(_epoch) + "\n" + inspect.getsource(quota_state) + "\n"
        + "path=Path(os.environ.get('HERMES_HOME') or str(Path.home()/'.hermes'))/'auth.json'\n"
        + "try:\n"
        + " with path.open('rb') as stream: raw=stream.read(1048577)\n"
        + " if len(raw)>1048576: raise ValueError('size')\n"
        + " auth=json.loads(raw)\n"
        + " if not isinstance(auth,dict): raise ValueError('shape')\n"
        + "except FileNotFoundError: auth={}\n"
        + "except (OSError,ValueError): print('{\"unavailable\":true}'); sys.exit(0)\n"
        + "print(json.dumps(quota_state(auth,sys.argv[1],time.time())))\n"
    )
    return "python3 -c " + shlex.quote(script) + " " + shlex.quote(provider)
