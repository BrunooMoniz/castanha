"""Agenda pelo hub Zinom, que é onde as contas Google do Bruno estão conectadas.

Por que aqui e não um cliente de Google Calendar: ele usa cinco contas Google e
quem resolve isso é o Zinom. O Castanha não guarda credencial de ninguém, só
fala com o hub.

Limite conhecido da tool `list_events`: ela devolve título, início, fim, local
e o link do evento, e **não** devolve participantes nem o link da chamada.
Essas duas coisas existem no acervo indexado do Zinom, então a próxima reunião
(só ela, que é onde isso importa) é enriquecida por uma busca estrita lá.
"""

import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional

from castanha.calendar import Attendee, MeetingEvent, extract_conference_url
from castanha.config import load_config
from castanha.zinom_adapter import ZinomError, ZinomMcpClient, _tool_text

# A lista de agendas quase não muda; os eventos mudam.
CALENDARS_TTL_SEC = 3600


def _tool_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(_tool_text(result))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_google_dt(node: Optional[Dict[str, Any]]) -> Optional[datetime.datetime]:
    """Aceita o par {dateTime, timeZone} e o {date} de evento de dia inteiro."""
    if not node:
        return None
    raw = node.get("dateTime")
    if raw:
        try:
            return datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None
    raw = node.get("date")
    if raw:
        try:
            d = datetime.date.fromisoformat(raw)
            return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    return None


def _is_all_day(event: Dict[str, Any]) -> bool:
    return "date" in (event.get("start") or {})


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


class ZinomCalendar:
    """Cliente de agenda. Guarda a sessão MCP e a lista de agendas entre chamadas."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or load_config()
        z_cfg = cfg.get("zinom", {})
        c_cfg = (cfg.get("calendar", {}) or {}).get("zinom", {}) or {}

        self.endpoint = z_cfg.get("endpoint", "https://zinom.ai/mcp")
        self.token = z_cfg.get("token", "")
        self.enabled = bool(c_cfg.get("enabled", True)) and bool(self.token)
        self.window_hours = int(c_cfg.get("window_hours", 12))
        self.skip_all_day = bool(c_cfg.get("skip_all_day", True))
        self.enrich_next = bool(c_cfg.get("enrich_next", True))
        # Vazio quer dizer "a agenda principal de cada conta conectada": é o que
        # sobra de reunião de verdade quando se tira feriado e aniversário.
        self.wanted = [str(x) for x in (c_cfg.get("calendars") or [])]

        self._client: Optional[ZinomMcpClient] = None
        self._calendars: List[Dict[str, Any]] = []
        self._calendars_at: float = 0.0

    # ------------------------------------------------------------- transporte
    def _connect(self) -> ZinomMcpClient:
        if self._client is not None:
            return self._client
        client = ZinomMcpClient(self.endpoint, self.token)
        client.connect()
        self._client = client
        return client

    def _call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return _tool_payload(self._connect().call_tool(name, args))
        except ZinomError:
            # Sessão morta ou expirada: uma segunda tentativa com sessão nova.
            self._client = None
            return _tool_payload(self._connect().call_tool(name, args))

    # ---------------------------------------------------------------- agendas
    def calendars(self, force: bool = False) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        if not force and self._calendars and (time.time() - self._calendars_at) < CALENDARS_TTL_SEC:
            return self._calendars
        payload = self._call("list_calendars", {})
        self._calendars = payload.get("calendars", []) or []
        self._calendars_at = time.time()
        return self._calendars

    def selected_calendars(self) -> List[Dict[str, Any]]:
        todas = self.calendars()
        if not self.wanted:
            return [c for c in todas if c.get("primary")]

        alvo = {_normalize(w) for w in self.wanted}
        escolhidas = []
        for c in todas:
            chaves = {
                _normalize(c.get("calendar_ref")),
                _normalize(c.get("summary")),
                _normalize(c.get("email")),
            }
            if chaves & alvo:
                escolhidas.append(c)
        return escolhidas

    # ---------------------------------------------------------------- eventos
    def upcoming(self, window_hours: Optional[int] = None) -> List[MeetingEvent]:
        if not self.enabled:
            return []

        horas = window_hours if window_hours is not None else self.window_hours
        agora = datetime.datetime.now().astimezone()
        # Quinze minutos para trás: reunião que começou agora ainda é a de agora.
        t_min = (agora - datetime.timedelta(minutes=15)).isoformat()
        t_max = (agora + datetime.timedelta(hours=horas)).isoformat()

        eventos: List[MeetingEvent] = []
        vistos = set()
        for cal in self.selected_calendars():
            ref = cal.get("calendar_ref")
            if not ref:
                continue
            try:
                payload = self._call("list_events", {
                    "calendar_ref": ref, "time_min": t_min, "time_max": t_max,
                })
            except Exception as e:
                print(f"[Castanha] Agenda {cal.get('summary')!r} falhou: {e}")
                continue

            for raw in payload.get("events", []) or []:
                if self.skip_all_day and _is_all_day(raw):
                    continue
                evento = self._to_meeting(raw, cal)
                if evento is None or evento.uid in vistos:
                    continue
                vistos.add(evento.uid)
                eventos.append(evento)

        eventos.sort(key=lambda e: e.start)
        return eventos

    def _to_meeting(self, raw: Dict[str, Any], cal: Dict[str, Any]) -> Optional[MeetingEvent]:
        inicio = _parse_google_dt(raw.get("start"))
        if inicio is None:
            return None
        fim = _parse_google_dt(raw.get("end")) or (inicio + datetime.timedelta(hours=1))

        local = raw.get("location") or None
        # Às vezes o link da chamada mora no campo de local.
        conf = extract_conference_url(local or "")

        return MeetingEvent(
            uid=str(raw.get("id") or f"{cal.get('calendar_ref')}::{inicio.isoformat()}"),
            title=str(raw.get("summary") or "Reunião"),
            start=inicio,
            end=fim,
            attendees=[],
            organizer=cal.get("email"),
            conference_url=conf,
            description=None,
            location=local,
            html_link=raw.get("htmlLink") or None,
            calendar_name=cal.get("summary") or None,
            account=cal.get("email") or None,
            source="zinom",
        )

    # ------------------------------------------------------------ enriquecimento
    def enrich(self, event: MeetingEvent) -> MeetingEvent:
        """Participantes e link da chamada, do acervo indexado.

        Só aceita um documento que bata título E data. Trazer os participantes da
        reunião errada é pior do que não trazer nenhum.
        """
        if not (self.enabled and self.enrich_next) or event is None:
            return event
        if event.attendees and event.conference_url:
            return event

        try:
            payload = self._call("brain_search", {"query": event.title, "limit": 6})
        except Exception:
            return event

        dia = event.start.date().isoformat()
        alvo = _normalize(event.title)

        for item in payload.get("results", []) or []:
            if item.get("source_type") != "calendar":
                continue
            if _normalize(item.get("title")) != alvo:
                continue
            texto = str(item.get("text") or "")
            if dia not in texto:
                continue

            if not event.conference_url:
                event.conference_url = extract_conference_url(texto)
            if not event.attendees:
                event.attendees = _parse_attendees(texto)
            break

        return event


def _parse_attendees(texto: str) -> List[Attendee]:
    match = re.search(r"\*\*Participantes:\*\*\s*(.+)", texto)
    if not match:
        return []
    pessoas = []
    for pedaco in match.group(1).split(","):
        email = pedaco.strip()
        if not email or "@" not in email:
            continue
        pessoas.append(Attendee(name=email.split("@")[0].replace(".", " ").title(), email=email))
    return pessoas
