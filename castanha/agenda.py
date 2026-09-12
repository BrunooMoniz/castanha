"""A agenda que o Castanha enxerga: feeds iCal mais as contas Google do Zinom.

Existe separado de `calendar.py` porque aquele módulo é o leitor de iCal e o
`zinom_calendar` já depende dele; juntar os dois daria import circular.
"""

import datetime
import sys
import time
from typing import Any, Dict, List, Optional

from castanha.calendar import MeetingEvent, fetch_feed_events, get_upcoming_meetings
from castanha.config import load_config
from castanha.hidden import is_hidden, load_hidden
from castanha.zinom_calendar import ZinomCalendar, motivo_curto

_zinom: Optional[ZinomCalendar] = None
# Evento sem hora de fim (iCal sem DTEND) continua valendo 15 min depois de
# começar, como sempre valeu.
CARENCIA_APOS_COMECO = datetime.timedelta(minutes=15)
# Falha fora do ZinomCalendar (rede de segurança): a fonte não deve levantar,
# mas se levantar o painel precisa saber.
_erro_coleta: Optional[str] = None


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

    global _erro_coleta
    try:
        eventos.extend(_zinom_source(cfg).upcoming(window_hours=max(1, window_minutes // 60), force=force))
        _erro_coleta = None
    except Exception as e:
        _erro_coleta = motivo_curto(e)
        print(f"[Castanha] Agenda do Zinom indisponível: {e}", file=sys.stderr)

    agora = datetime.datetime.now(datetime.timezone.utc)
    limite = agora + datetime.timedelta(minutes=window_minutes)

    # Mesma reunião vinda do iCal e do Zinom: fica a que tem link de chamada.
    escondidos = load_hidden()
    por_chave: Dict[str, MeetingEvent] = {}
    for e in eventos:
        if is_hidden(e.uid, escondidos):
            continue
        inicio = e.start if e.start.tzinfo else e.start.replace(tzinfo=datetime.timezone.utc)
        fim = e.end if e.end.tzinfo else e.end.replace(tzinfo=datetime.timezone.utc)
        if inicio > limite:
            continue
        if e.all_day:
            # Dia inteiro começa à meia-noite: vale enquanto o dia não acabou.
            if fim <= agora:
                continue
        elif max(fim, inicio + CARENCIA_APOS_COMECO) <= agora:
            # Reunião em andamento fica na lista até acabar: é a que ele quer
            # gravar. Antes o corte era 15 min depois do começo, e em 12/09/2026
            # a Nora Weekly das 10:30 ficou de fora: a máquina acordou sem rede
            # às 10:57, a agenda só voltou às 11:02 e a reunião já tinha
            # "passado". Ele gravou na mão, sem título nem participantes.
            continue
        chave = f"{e.title.strip().lower()}|{inicio.isoformat()}"
        atual = por_chave.get(chave)
        if atual is None or (not atual.conference_url and e.conference_url):
            por_chave[chave] = e

    # Reunião com hora primeiro; o que é do dia inteiro vai para o fim.
    saida = sorted(por_chave.values(), key=lambda e: (e.all_day, e.start))
    return saida[:limit]


def events_on_day(dia: datetime.date, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Todas as reuniões com hora de um dia, nas agendas dele, escondidas inclusive.

    É a lista para vincular uma gravação ao evento certo depois do fato; um
    lembrete de dia inteiro não é reunião e fica fora. O hub filtra pelo fim do
    evento, então o começo é conferido aqui para não vazar a véspera.
    """
    cfg = config or load_config()
    # Meia-noite local DAQUELE dia, pelas regras do fuso da máquina (horário de
    # verão inclusive): o offset de agora serviria só para hoje.
    inicio = datetime.datetime(dia.year, dia.month, dia.day).astimezone()
    fim = (datetime.datetime(dia.year, dia.month, dia.day) + datetime.timedelta(days=1)).astimezone()
    try:
        eventos, avisos = _zinom_source(cfg).events_between(inicio, fim)
    except Exception as e:  # a lista de agendas do hub falhou: os feeds ainda valem
        eventos, avisos = [], [f"Zinom: {motivo_curto(e)}"]
    # Feeds iCal configurados entram como na agenda normal: sem Zinom, são a única fonte.
    # O leitor de feed imprime o erro em stdout; aqui isso vira aviso, porque a
    # saída JSON destes comandos precisa ficar só com JSON.
    import contextlib
    import io
    for feed in (cfg.get("calendar") or {}).get("feeds") or []:
        url = feed.get("url") if isinstance(feed, dict) else None
        if not url:
            continue
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            eventos.extend(fetch_feed_events(url))
        if saida.getvalue().strip():
            avisos.append(f"Feed iCal {feed.get('name') or url}: indisponível")
    do_dia, vistos = [], set()
    for e in sorted(eventos, key=lambda e: e.start):
        if e.all_day or e.uid in vistos or not (inicio <= e.start < fim):
            continue
        vistos.add(e.uid)
        do_dia.append(e)
    return {"meetings": do_dia, "warnings": avisos}


def find_event(dia: datetime.date, uid: str, config: Optional[Dict[str, Any]] = None) -> Optional[MeetingEvent]:
    """O evento do dia pelo uid exato (ou pela chave da série, se só ela veio)."""
    from castanha.hidden import series_key
    alvo = str(uid or "").strip()
    if not alvo:
        return None
    eventos = events_on_day(dia, config)["meetings"]
    exato = next((e for e in eventos if e.uid == alvo), None)
    if exato is not None:
        return exato
    return next((e for e in eventos if series_key(e.uid) == series_key(alvo)), None)


def next_timed(eventos: List[MeetingEvent]) -> Optional[MeetingEvent]:
    """A reunião de agora: a que está em andamento, ou a próxima a começar.

    Evento de dia inteiro não conta: iria para a barra o dia todo e dispararia
    o aviso de "reunião em instantes" às 23h58 da véspera.
    """
    return next((m for m in eventos if not m.all_day), None)


def next_meeting(config: Optional[Dict[str, Any]] = None) -> Optional[MeetingEvent]:
    return next_timed(collect_upcoming(config, window_minutes=720))


def agenda_warning(config: Optional[Dict[str, Any]] = None, erro: Optional[BaseException] = None) -> Optional[str]:
    """O aviso que vai para o painel, ou None quando o último ciclo foi inteiro bom.

    Antes, uma agenda que falhava (limite de chamadas do hub, compartilhado
    com o resto da máquina) virava lista vazia sem aviso, e o painel dizia
    "nada nas próximas horas" como se fosse verdade.
    """
    fonte = _zinom_source(config or load_config())
    motivo = (motivo_curto(erro) if erro is not None else None) or fonte.last_error or _erro_coleta
    if not motivo:
        return None
    desde = fonte.stale_since
    if desde:
        # Mais de um dia sem ciclo bom: só a hora enganaria.
        formato = "%H:%M" if (time.time() - desde) < 86400 else "%d/%m %H:%M"
        return f"Agenda desatualizada desde {time.strftime(formato, time.localtime(desde))}: {motivo}"
    return f"Agenda indisponível: {motivo}"
