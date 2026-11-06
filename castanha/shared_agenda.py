"""Agenda lida de um snapshot que outro coletor da máquina já grava em disco.

Por que existe: o painel de agenda do desktop e o Castanha consultavam o Zinom
cada um por si, agenda por agenda, a cada poucos minutos. Com um coletor só, o
Castanha lê o arquivo dele e não vai ao hub para mostrar a agenda nem para
avisar da próxima reunião. Consulta direta fica para quando o arquivo some ou
envelhece demais, e para o que a pessoa pede na hora (vincular uma gravação).

Formato esperado (JSON, schema 2): `generated_at` (ISO com fuso), `ok`,
`stale`/`stale_since`, `calendars[]` com `ref`, `summary`, `email`, `primary`,
`owner` e `access_role`, e `events[]` com `uid`, `title`, `start`, `end`,
`all_day`, `calendar`, `calendar_ref`, `account`, `meet_url`,
`conference_provider`, `attendees[]` (`email`, `name`, `response`,
`organizer`, `optional`), `organizer`, `description`, `location`, `html_link`.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMAS_ACEITOS = (2,)
LIMITE_BYTES = 16 * 1024 * 1024
TIMEOUT_REFRESH_SEC = 150
MOTIVO_COLETOR = "o coletor da agenda falhou"


def _instante(valor: Any) -> Optional[float]:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        dt = datetime.datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


class SharedSnapshot:
    """Lê (com cache por mtime) e, se configurado, pede coleta nova ao coletor."""

    def __init__(self, cfg: Dict[str, Any]):
        self.path = Path(str(cfg.get("path") or "")).expanduser()
        try:
            self.max_age_sec = max(60, int(cfg.get("max_age_sec", 43200)))
        except (TypeError, ValueError):
            self.max_age_sec = 43200
        comando = cfg.get("refresh_command") or []
        self.refresh_command: List[str] = (
            [os.path.expanduser(str(a)) for a in comando]
            if isinstance(comando, list) and comando and all(isinstance(a, str) and a for a in comando)
            else []
        )
        self._marca: Optional[Tuple[int, int]] = None
        self._doc: Optional[Dict[str, Any]] = None
        self.problema: Optional[str] = None

    def load(self) -> Optional[Dict[str, Any]]:
        """O documento atual, ou None (com `problema` dizendo por quê)."""
        try:
            info = os.stat(self.path)
        except OSError:
            self._marca, self._doc, self.problema = None, None, "arquivo da agenda compartilhada ausente"
            return None
        marca = (info.st_mtime_ns, info.st_size)
        if marca == self._marca:
            return self._doc
        self._marca, self._doc = marca, None
        if info.st_size > LIMITE_BYTES:
            self.problema = "arquivo da agenda compartilhada grande demais"
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            # Escrita do coletor é tmp+rename; ilegível aqui é arquivo quebrado.
            self.problema = "arquivo da agenda compartilhada ilegível"
            return None
        if (not isinstance(doc, dict) or doc.get("schema") not in SCHEMAS_ACEITOS
                or not isinstance(doc.get("events"), list) or not isinstance(doc.get("calendars"), list)):
            self.problema = "formato da agenda compartilhada desconhecido"
            return None
        self._doc, self.problema = doc, None
        return doc

    @staticmethod
    def events_stale(doc: Dict[str, Any]) -> bool:
        """Os eventos vieram da coleta anterior (a última falhou para a agenda)?

        `stale` é do arquivo inteiro: tarefas que falharam sozinhas também o
        ligam. O que diz dos eventos é `stats.preserved`, como no painel.
        """
        if not doc.get("stale"):
            return False
        stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
        preservados = stats.get("preserved")
        if not isinstance(preservados, list):
            return True  # sem detalhe: na dúvida, trata como velho
        return "events" in preservados

    @classmethod
    def data_time(cls, doc: Dict[str, Any]) -> Optional[float]:
        """Quando os eventos do arquivo foram de fato coletados."""
        if cls.events_stale(doc) and doc.get("stale_since"):
            return _instante(doc.get("stale_since"))
        return _instante(doc.get("generated_at"))

    def refresh(self) -> bool:
        """Pede uma coleta ao coletor (botão "atualizar"). True se rodou com código 0."""
        if not self.refresh_command:
            return False
        try:
            r = subprocess.run(self.refresh_command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=TIMEOUT_REFRESH_SEC, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[Castanha] Coletor da agenda não rodou: {e.__class__.__name__}", file=sys.stderr)
            return False
        if r.returncode != 0:
            print(f"[Castanha] Coletor da agenda saiu com código {r.returncode}", file=sys.stderr)
        return r.returncode == 0


def raw_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Evento do snapshot no formato cru do hub, para passar pela MESMA conversão."""
    dia_inteiro = bool(ev.get("all_day"))
    inicio, fim = ev.get("start"), ev.get("end")
    if dia_inteiro:
        start = {"date": str(inicio)[:10]} if inicio else None
        end = {"date": str(fim)[:10]} if fim else None
    else:
        start = {"dateTime": inicio} if inicio else None
        end = {"dateTime": fim} if fim else None
    titulo = ev.get("title")
    return {
        "id": ev.get("uid"),
        # O coletor troca título vazio por "(sem título)"; aqui volta a ser "Reunião".
        "summary": None if titulo == "(sem título)" else titulo,
        "start": start,
        "end": end,
        "attendees": ev.get("attendees") or [],
        "organizer": {"email": ev["organizer"]} if ev.get("organizer") else None,
        "conference_url": ev.get("meet_url"),
        "conference_provider": ev.get("conference_provider"),
        "location": ev.get("location"),
        "htmlLink": ev.get("html_link"),
        "description": ev.get("description"),
    }


def calendar_selected(cal: Dict[str, Any], wanted: List[str], normalize) -> bool:
    """Mesma regra da consulta direta: lista explícita, ou principal + owner/writer."""
    if wanted:
        alvo = {normalize(w) for w in wanted}
        return bool({normalize(cal.get("ref")), normalize(cal.get("summary")), normalize(cal.get("email"))} & alvo)
    papel = cal.get("access_role")
    if papel is None:
        papel = "owner" if cal.get("owner") else None
    return bool(cal.get("primary")) or papel in ("owner", "writer")
