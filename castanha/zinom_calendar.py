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
import sys
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

# Como o hub diz que uma tool não existe (sondado em 07/09/2026):
# `isError` com o texto "MCP error -32602: Tool <nome> not found". Só isso
# rebaixa para a listagem magra. Um 404 do Google numa agenda também termina
# em "Not Found", e antes derrubava TODAS as agendas por meia hora.
_TOOL_AUSENTE = re.compile(r"MCP error -32602: Tool (\S+) not found", re.IGNORECASE)

MOTIVO_LIMITE = "limite de chamadas do Zinom"
MOTIVO_CONEXAO = "sem conexão com o Zinom"


def tool_ausente(e: BaseException, name: str) -> bool:
    """True só para o erro JSON-RPC -32602 do hub sobre ESTA tool."""
    m = _TOOL_AUSENTE.search(str(e))
    return bool(m) and m.group(1) == name and (getattr(e, "tool", None) in (None, name))


def motivo_curto(e: BaseException) -> str:
    """Linguagem de produto para o painel: sem URL, sem token, sem prefixo de log."""
    texto = str(e) or e.__class__.__name__
    baixo = texto.lower()
    if re.search(r"\b429\b", baixo) or "rate limit" in baixo or "too many requests" in baixo:
        return MOTIVO_LIMITE
    if any(p in baixo for p in ("urlopen error", "timed out", "connection", "name resolution",
                                "não respondeu", "http 5")):
        return MOTIVO_CONEXAO
    texto = re.sub(r"^(?:tools/call|[\w.-]+ devolveu erro|[\w.-]+):\s*", "", texto)
    texto = re.sub(r"https?://\S+", "", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" :") or e.__class__.__name__
    return texto if len(texto) <= 80 else texto[:79] + "…"


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
            # Evento de dia inteiro: usa o fuso local para a data não voltar 1 dia
            local_tz = datetime.datetime.now().astimezone().tzinfo
            return datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=local_tz)
        except ValueError:
            return None
    return None


def _is_all_day(event: Dict[str, Any]) -> bool:
    return "date" in (event.get("start") or {})


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _nome_de_email(email: str) -> str:
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()


def _e_agenda_de_grupo(email: Any) -> bool:
    """A agenda de grupo ("Eventos Nora") aparece como organizadora e convidada
    de si mesma. Não é pessoa: fora da lista, senão vira "participante" no
    popup, no Silver e no Zinom, e o Gold passa a descartar fato que cite "Nora"."""
    return str(email or "").strip().lower().endswith("@group.calendar.google.com")


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
        # Dia inteiro entra por padrão desde 05/09/2026: lembrete e evento sem
        # link também são agenda, e o que não for reunião ele esconde no painel.
        self.skip_all_day = bool(c_cfg.get("skip_all_day", False))
        # Vazio quer dizer "as agendas dele": a principal de cada conta mais as
        # que ele pode editar (owner/writer), como a de grupo "Eventos Nora".
        # Feriados e agendas de outras pessoas vêm só-leitura e ficam de fora.
        # Uma lista explícita em `calendars` substitui essa regra.
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
        # Honestidade da agenda: em falha, a última lista boa fica e o painel
        # é avisado. Sem isto, rate limit virava "nada nas próximas horas".
        self._good_at: float = 0.0
        self.last_error: Optional[str] = None

    @property
    def stale_since(self) -> Optional[float]:
        """Hora do último ciclo inteiro bom (None se nunca houve): é desde
        quando a lista está parada quando `last_error` está preenchido."""
        return self._good_at or None

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

    def selected_calendars(self, force: bool = False) -> List[Dict[str, Any]]:
        todas = self.calendars(force=force)
        if not self.wanted:
            # Em 07/09 a reunião do dia estava só em "Eventos Nora" (agenda de
            # grupo, não principal) e o painel mostrou "nada nas próximas horas".
            return [c for c in todas if c.get("primary") or c.get("accessRole") in ("owner", "writer")]

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
        falha: Optional[str] = None
        try:
            agendas = self.selected_calendars(force=force)
        except Exception as e:
            print(f"[Castanha] Lista de agendas falhou: {e}", file=sys.stderr)
            agendas, falha = [], motivo_curto(e)
        for cal in agendas:
            ref = cal.get("calendar_ref")
            if not ref:
                continue
            try:
                brutos = self._events_for(ref, t_min, t_max)
            except Exception as e:
                print(f"[Castanha] Agenda {cal.get('summary')!r} falhou: {e}", file=sys.stderr)
                motivo = motivo_curto(e)
                falha = falha or motivo
                if motivo == MOTIVO_LIMITE:
                    break  # as outras agendas vão bater no mesmo limite
                continue
            for raw in brutos:
                if self.skip_all_day and _is_all_day(raw):
                    continue
                evento = self._to_meeting(raw, cal)
                if evento is None or evento.uid in vistos:
                    continue
                vistos.add(evento.uid)
                eventos.append(evento)

        # Mesma cadência com ou sem falha: bater de novo a cada minuto num
        # limite compartilhado só piora.
        self._cache_at = time.time()
        self.last_error = falha
        if falha is not None:
            return self._cache
        eventos.sort(key=lambda e: e.start)
        self._cache = eventos
        self._good_at = self._cache_at
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

    def _events_for(self, ref: str, t_min: str, t_max: str) -> List[Dict[str, Any]]:
        """Eventos brutos de uma agenda. Qualquer falha sobe: é desta agenda, neste ciclo."""
        args = {"calendar_ref": ref, "time_min": t_min, "time_max": t_max}

        if self._quer_detalhe():
            try:
                payload = self._call("list_event_details", args)
                self._detalhe_disponivel = True
                return payload.get("events", []) or []
            except ZinomError as e:
                if not tool_ausente(e, "list_event_details"):
                    raise
                # Hub antigo, sem a tool: cai para a listagem magra e não tenta
                # de novo nas próximas agendas do mesmo ciclo.
                self._detalhe_disponivel = False
                self._detalhe_negado_em = time.time()

        return self._call("list_events", args).get("events", []) or []

    def _to_meeting(self, raw: Dict[str, Any], cal: Dict[str, Any]) -> Optional[MeetingEvent]:
        inicio = _parse_google_dt(raw.get("start"))
        if inicio is None:
            return None
        is_all_day = _is_all_day(raw)
        fim = _parse_google_dt(raw.get("end")) or (inicio + (datetime.timedelta(days=1) if is_all_day else datetime.timedelta(hours=1)))

        local = raw.get("location") or None
        conf = raw.get("conference_url") or extract_conference_url(local or "")

        organizador = raw.get("organizer") or {}
        if isinstance(organizador, dict) and not _e_agenda_de_grupo(organizador.get("email")):
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
            all_day=is_all_day,
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
        if not (email or nome) or _e_agenda_de_grupo(email):
            continue
        pessoas.append(Attendee(
            name=nome or _nome_de_email(email),
            email=email,
            response=str(a.get("response") or ""),
            organizer=bool(a.get("organizer")),
            optional=bool(a.get("optional")),
        ))
    # Organizador primeiro, depois quem aceitou, e por fim quem não respondeu:
    # é a ordem em que a informação é útil ao olhar a reunião.
    ordem = {"accepted": 1, "tentative": 2, "needsAction": 3, "declined": 4}
    pessoas.sort(key=lambda p: (0 if p.organizer else 1, ordem.get(p.response, 5), p.name.lower()))
    return pessoas
