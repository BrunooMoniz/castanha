"""Eventos que o Bruno não quer ver na agenda do Castanha.

Ele usa o calendário também como lembrete pessoal ("Pagar Condomínio",
"Fechamento Semanal"), e isso não é reunião para um gravador de reunião. Em vez
de adivinhar por heurística, ele marca o que não quer, e a marca vale para a
série inteira: esconder uma instância de um evento recorrente esconde todas.

Mora no diretório de estado, e não na config, porque quem escreve aqui é o
clique dele no painel, e a config é dele para editar à mão.
"""

import datetime
import json
import re
from typing import Any, Dict, List, Optional

from castanha.config import get_state_dir

# Instância de evento recorrente do Google: "<id-da-serie>_20260904T120000Z".
_SUFIXO_DE_INSTANCIA = re.compile(r"^(.+?)_\d{8}(?:T\d{6}Z?)?$")


def hidden_file():
    return get_state_dir() / "hidden_events.json"


def series_key(uid: Any) -> str:
    """A chave da SÉRIE, não da instância de hoje."""
    texto = str(uid or "").strip()
    if not texto:
        return ""
    match = _SUFIXO_DE_INSTANCIA.match(texto)
    return match.group(1) if match else texto


def load_hidden() -> Dict[str, Dict[str, Any]]:
    caminho = hidden_file()
    if not caminho.exists():
        return {}
    try:
        data = json.loads(caminho.read_text(encoding="utf-8"))
        return data.get("hidden", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(hidden: Dict[str, Dict[str, Any]]) -> None:
    caminho = hidden_file()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps({"hidden": hidden}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(caminho)


def hide(uid: Any, title: str = "") -> Optional[str]:
    chave = series_key(uid)
    if not chave:
        return None
    hidden = load_hidden()
    hidden[chave] = {
        "title": title or hidden.get(chave, {}).get("title", ""),
        "hidden_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save(hidden)
    return chave


def unhide(chave_ou_uid: Any) -> bool:
    hidden = load_hidden()
    chave = series_key(chave_ou_uid)
    # Aceita a chave da série e também o id de uma instância.
    if chave in hidden:
        del hidden[chave]
        _save(hidden)
        return True
    return False


def unhide_all() -> int:
    quantos = len(load_hidden())
    _save({})
    return quantos


def is_hidden(uid: Any, hidden: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    chave = series_key(uid)
    if not chave:
        return False
    return chave in (hidden if hidden is not None else load_hidden())


def listar() -> List[Dict[str, Any]]:
    return [
        {"key": chave, "title": dados.get("title", ""), "hidden_at": dados.get("hidden_at", "")}
        for chave, dados in sorted(load_hidden().items(), key=lambda kv: kv[1].get("hidden_at", ""))
    ]
