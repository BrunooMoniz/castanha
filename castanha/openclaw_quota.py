"""Cota do OpenClaw lida na VPS, sem transportar credencial de volta.

O `openclaw models status --json` traz rótulos de perfil com pedaços de chave
("zai:conta = abcd...wxyz"). Nada disso pode viajar: o filtro roda NA VPS e
só devolve {"blocked": bool} ou {"unavailable": true}.
"""

# Estado de perfil que ainda autentica. "expiring" avisa validade curta, mas
# a chamada seguinte ainda passa; bloquear aqui deixaria o resumo parado à toa.
USABLE_STATUS = ("ok", "expiring", "static", "valid", "active")


def quota_state(status, provider):
    """Bloqueia só quando o provedor pedido não tem credencial utilizável.

    `runtimeAuthRoutes` fica de fora de propósito: o runtime `codex` aparece
    como "unavailable" na .68 mesmo com o provider `openai` autenticado, e isso
    não impede o `anthropic` de responder.
    """
    if not isinstance(status, dict):
        return {"blocked": True}
    auth = status.get("auth")
    if not isinstance(auth, dict):
        return {"blocked": True}
    missing = auth.get("missingProvidersInUse")
    if isinstance(missing, list) and provider in missing:
        return {"blocked": True}
    oauth = auth.get("oauth") if isinstance(auth.get("oauth"), dict) else {}
    rows = [row for row in (oauth.get("providers") or [])
            if isinstance(row, dict) and row.get("provider") == provider]
    if rows:
        if any(str(row.get("status", "")).lower() in USABLE_STATUS for row in rows):
            return {"blocked": False}
        return {"blocked": True}
    # Sem linha em oauth: o provedor pode viver só em auth.providers (api key
    # pura). Presente ali basta; ausente das duas listas é bloqueio.
    listed = [row for row in (auth.get("providers") or [])
              if isinstance(row, dict) and row.get("provider") == provider]
    return {"blocked": not listed}


def remote_command(provider, env_prefix):
    """Comando de shell que roda na VPS e imprime só o estado sanitizado."""
    import inspect
    import shlex

    script = (
        "import json,sys\n"
        + "USABLE_STATUS = " + repr(USABLE_STATUS) + "\n"
        + inspect.getsource(quota_state) + "\n"
        + "try:\n"
        + " raw=sys.stdin.read(4194304)\n"
        + " status=json.loads(raw)\n"
        + "except (ValueError, OSError): print('{\"unavailable\":true}'); sys.exit(0)\n"
        + "print(json.dumps(quota_state(status,sys.argv[1])))\n"
    )
    return (env_prefix + "openclaw models status --agent " + shlex.quote("main")
            + " --json 2>/dev/null | python3 -c " + shlex.quote(script)
            + " " + shlex.quote(provider))
