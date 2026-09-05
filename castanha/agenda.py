"""A agenda que o Castanha enxerga: feeds iCal mais as contas Google do Zinom.

Existe separado de `calendar.py` porque aquele módulo é o leitor de iCal e o
`zinom_calendar` já depende dele; juntar os dois daria import circular.
"""

import datetime
from typing import Any, Dict, List, Optional

from castanha.calendar import MeetingEvent, get_upcoming_meetings
from castanha.config import load_config
from castanha.hidden import is_hidden, load_hidden
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
    force: bool = False,
) -> List[MeetingEvent]:
    """As próximas reuniões, das duas fontes, ordenadas e sem repetição."""
    cfg = config or load_config()
    cal_cfg = cfg.get("calendar", {}) or {}

    eventos: List[MeetingEvent] = []

    feeds = cal_cfg.get("feeds") or []
    if feeds:
        eventos.extend(get_upcoming_meetings(feeds, window_minutes=window_minutes))

    try:
        eventos.extend(_zinom_source(cfg).upcoming(window_hours=max(1, window_minutes // 60), force=force))
    except Exception as e:
        import sys as _sys
        print(f"[Castanha] Agenda do Zinom indisponível: {e}", file=_sys.stderr)

    agora = datetime.datetime.now(datetime.timezone.utc)
    corte = agora - datetime.timedelta(minutes=15)
    limite = agora + datetime.timedelta(minutes=window_minutes)

    # Mesma reunião vinda do iCal e do Zinom: fica a que tem link de chamada.
    escondidos = load_hidden()
    por_chave: Dict[str, MeetingEvent] = {}
    for e in eventos:
        if is_hidden(e.uid, escondidos):
            continue
        inicio = e.start if e.start.tzinfo else e.start.replace(tzinfo=datetime.timezone.utc)
        fim = e.end if e.end.tzinfo else e.end.replace(tzinfo=datetime.timezone.utc)
        if e.all_day:
            # Dia inteiro começa à meia-noite, sempre antes do corte: vale
            # enquanto o dia não acabou, e entra na janela pelo começo.
            if fim <= agora or inicio > limite:
                continue
        elif not (corte <= inicio <= limite):
            continue
        chave = f"{e.title.strip().lower()}|{inicio.isoformat()}"
        atual = por_chave.get(chave)
        if atual is None or (not atual.conference_url and e.conference_url):
            por_chave[chave] = e

    # Reunião com hora primeiro; o que é do dia inteiro vai para o fim.
    saida = sorted(por_chave.values(), key=lambda e: (e.all_day, e.start))
    return saida[:limit]


def next_timed(eventos: List[MeetingEvent]) -> Optional[MeetingEvent]:
    """A próxima reunião de verdade: evento de dia inteiro não conta.

    Ele iria para a barra o dia todo e dispararia o aviso de "reunião em
    instantes" às 23h58 da véspera.
    """
    return next((m for m in eventos if not m.all_day), None)


def next_meeting(config: Optional[Dict[str, Any]] = None) -> Optional[MeetingEvent]:
    return next_timed(collect_upcoming(config, window_minutes=720))
