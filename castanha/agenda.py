"""A agenda que o Castanha enxerga: feeds iCal mais as contas Google do Zinom.

Existe separado de `calendar.py` porque aquele módulo é o leitor de iCal e o
`zinom_calendar` já depende dele; juntar os dois daria import circular.
"""

import datetime
from typing import Any, Dict, List, Optional

from castanha.calendar import MeetingEvent, get_upcoming_meetings
from castanha.config import load_config
from castanha.zinom_calendar import ZinomCalendar

_zinom: Optional[ZinomCalendar] = None


def _zinom_source(config: Dict[str, Any]) -> ZinomCalendar:
    # Instância única, para a sessão MCP e os caches sobreviverem entre ciclos.
    global _zinom
    if _zinom is None:
        _zinom = ZinomCalendar(config)
    return _zinom


def collect_upcoming(
    config: Optional[Dict[str, Any]] = None,
    window_minutes: int = 720,
    limit: int = 8,
) -> List[MeetingEvent]:
    """As próximas reuniões, das duas fontes, ordenadas e sem repetição."""
    cfg = config or load_config()
    cal_cfg = cfg.get("calendar", {}) or {}

    eventos: List[MeetingEvent] = []

    feeds = cal_cfg.get("feeds") or []
    if feeds:
        eventos.extend(get_upcoming_meetings(feeds, window_minutes=window_minutes))

    try:
        eventos.extend(_zinom_source(cfg).upcoming(window_hours=max(1, window_minutes // 60)))
    except Exception as e:
        print(f"[Castanha] Agenda do Zinom indisponível: {e}")

    agora = datetime.datetime.now(datetime.timezone.utc)
    corte = agora - datetime.timedelta(minutes=15)
    limite = agora + datetime.timedelta(minutes=window_minutes)

    # Mesma reunião vinda do iCal e do Zinom: fica a que tem link de chamada.
    por_chave: Dict[str, MeetingEvent] = {}
    for e in eventos:
        inicio = e.start if e.start.tzinfo else e.start.replace(tzinfo=datetime.timezone.utc)
        if not (corte <= inicio <= limite):
            continue
        chave = f"{e.title.strip().lower()}|{inicio.isoformat()}"
        atual = por_chave.get(chave)
        if atual is None or (not atual.conference_url and e.conference_url):
            por_chave[chave] = e

    saida = sorted(por_chave.values(), key=lambda e: e.start)
    return saida[:limit]


def next_meeting(config: Optional[Dict[str, Any]] = None) -> Optional[MeetingEvent]:
    proximas = collect_upcoming(config, window_minutes=720, limit=1)
    return proximas[0] if proximas else None
