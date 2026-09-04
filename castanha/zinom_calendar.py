"""Agenda pelo hub Zinom, que é onde as contas Google do Bruno estão conectadas.

Por que aqui e não um cliente de Google Calendar próprio: ele usa cinco contas
Google, e quem já resolve isso é o Zinom. O Castanha não guarda credencial de
ninguém, só fala com o hub.

Usa `list_event_details`, que devolve o evento inteiro (participantes,
organizador e o link da chamada já resolvido). A `list_events` fica como
segunda opção, para o caso de o hub ainda não ter a tool nova: aí a reunião
aparece com título e horário, sem participantes e sem link.
"""

import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional

from castanha.calendar import Attendee, MeetingEvent, extract_conference_url
from castanha.config import load_config
from castanha.zinom_adapter import ZinomError, ZinomMcpClient, tool_json

# A lista de agendas quase não muda; os eventos mudam.
CALENDARS_TTL_SEC = 3600

# De quanto em quanto tempo tentar de novo a tool rica depois de ela ter faltado.
# Sem isto, um daemon que subiu antes do hub ganhar a tool ficava na listagem
# magra PARA SEMPRE, e reunião com participante aparecia sem participante até
# alguém reiniciar o processo. Foi o que aconteceu em 04/09.
DETALHE_RETRY_SEC = 1800


def _parse_google_dt(node: Optional[Dict[str, Any]]) -> Optional[datetime.datetime]:
    """Aceita o par {dateTime, timeZone} e o {date} de evento de dia inteiro."""
    if not isinstance(node, dict):
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


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _nome_de_email(email: str) -> str:
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()


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
        # Vazio quer dizer "a agenda principal de cada conta conectada": é o que
        # sobra de reunião de verdade depois de tirar feriado e aniversário.
        self.wanted = [str(x) for x in (c_cfg.get("calendars") or [])]

        # A agenda não muda de minuto em minuto, e o endpoint tem rate limit
        # de 60 requisições por minuto para tudo o que o Bruno usa.
        self.poll_interval_sec = int(c_cfg.get("poll_interval_sec", 300))

        self._client: Optional[ZinomMcpClient] = None
        self._calendars: List[Dict[str, Any]] = []
        self._calendars_at: float = 0.0
        self._detalhe_disponivel: Optional[bool] = None
        self._detalhe_negado_em: float = 0.0
        self._cache: List[MeetingEvent] = []
        self._cache_at: float = 0.0

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
            return tool_json(self._connect().call_tool(name, args))
        except ZinomError:
            # Uma segunda tentativa com sessão nova antes de desistir.
            self._client = None
            return tool_json(self._connect().call_tool(name, args))

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
        return [
            c for c in todas
            if {_normalize(c.get("calendar_ref")), _normalize(c.get("summary")), _normalize(c.get("email"))} & alvo
        ]

    # ---------------------------------------------------------------- eventos
    def upcoming(self, window_hours: Optional[int] = None, force: bool = False) -> List[MeetingEvent]:
        if not self.enabled:
            return []
        if not force and self._cache_at and (time.time() - self._cache_at) < self.poll_interval_sec:
            return self._cache

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
            for raw in self._events_for(ref, t_min, t_max, cal):
                if self.skip_all_day and _is_all_day(raw):
                    continue
                evento = self._to_meeting(raw, cal)
                if evento is None or evento.uid in vistos:
                    continue
                vistos.add(evento.uid)
                eventos.append(evento)

        eventos.sort(key=lambda e: e.start)
        self._cache = eventos
        self._cache_at = time.time()
        return eventos

    def _quer_detalhe(self) -> bool:
        """A tool rica volta a ser tentada depois de um tempo.

        O hub pode ganhar a tool a qualquer momento (um deploy), e o daemon é um
        processo longo: desistir de vez seria congelar a agenda pobre até o
        próximo reinício.
        """
        if self._detalhe_disponivel is not False:
            return True
        return (time.time() - self._detalhe_negado_em) > DETALHE_RETRY_SEC

    def _events_for(self, ref: str, t_min: str, t_max: str, cal: Dict[str, Any]) -> List[Dict[str, Any]]:
        args = {"calendar_ref": ref, "time_min": t_min, "time_max": t_max}

        if self._quer_detalhe():
            try:
                payload = self._call("list_event_details", args)
                self._detalhe_disponivel = True
                return payload.get("events", []) or []
            except ZinomError as e:
                # Hub antigo, sem a tool: cai para a listagem magra e não tenta
                # de novo nas próximas agendas do mesmo ciclo.
                if "not found" in str(e).lower() or "unknown tool" in str(e).lower():
                    self._detalhe_disponivel = False
                    self._detalhe_negado_em = time.time()
                else:
                    print(f"[Castanha] Agenda {cal.get('summary')!r} falhou: {e}")
                    return []
            except Exception as e:
                print(f"[Castanha] Agenda {cal.get('summary')!r} falhou: {e}")
                return []

        try:
            return self._call("list_events", args).get("events", []) or []
        except Exception as e:
            print(f"[Castanha] Agenda {cal.get('summary')!r} falhou: {e}")
            return []

    def _to_meeting(self, raw: Dict[str, Any], cal: Dict[str, Any]) -> Optional[MeetingEvent]:
        inicio = _parse_google_dt(raw.get("start"))
        if inicio is None:
            return None
        fim = _parse_google_dt(raw.get("end")) or (inicio + datetime.timedelta(hours=1))

        local = raw.get("location") or None
        conf = raw.get("conference_url") or extract_conference_url(local or "")

        organizador = raw.get("organizer") or {}
        if isinstance(organizador, dict):
            organizador_email = organizador.get("email") or cal.get("email")
        else:
            organizador_email = cal.get("email")

        return MeetingEvent(
            uid=str(raw.get("id") or f"{cal.get('calendar_ref')}::{inicio.isoformat()}"),
            title=str(raw.get("summary") or "Reunião"),
            start=inicio,
            end=fim,
            attendees=_parse_attendees(raw.get("attendees")),
            organizer=organizador_email,
            conference_url=conf,
            conference_provider=raw.get("conference_provider"),
            description=raw.get("description"),
            location=local,
            html_link=raw.get("htmlLink") or None,
            calendar_name=cal.get("summary") or None,
            account=cal.get("email") or None,
            source="zinom",
        )


def _parse_attendees(lista: Any) -> List[Attendee]:
    if not isinstance(lista, list):
        return []
    pessoas = []
    for a in lista:
        if not isinstance(a, dict):
            continue
        email = str(a.get("email") or "").strip()
        nome = str(a.get("name") or "").strip()
        if not (email or nome):
            continue
        pessoas.append(Attendee(name=nome or _nome_de_email(email), email=email))
    return pessoas
