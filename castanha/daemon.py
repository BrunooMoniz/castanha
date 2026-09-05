"""Daemon de monitoramento em segundo plano (atualizador de status, relógio e calendário)."""

import datetime
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Set

from castanha.config import get_state_dir

from castanha.agenda import collect_upcoming, next_timed
from castanha.config import load_config
from castanha.engine import CastanhaEngine, notify
from castanha.state import StateManager
from castanha.retry import RetryScheduler

def pid_file():
    return get_state_dir() / "daemon.pid"


def running_daemon_pid():
    """PID do daemon vivo, ou None. Serve para não subir dois."""
    arquivo = pid_file()
    try:
        pid = int(arquivo.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return None
        if e.errno == errno.EPERM:
            return pid  # existe, é de outro usuário
        return None
    return pid


def claim_pid_file() -> bool:
    """True se este processo virou O daemon.

    Dois daemons brigam pelo mesmo state.json e o mais velho vence por último:
    em 04/09 um daemon que subiu antes de um deploy ficou sobrescrevendo a
    agenda boa com a versão pobre, e a reunião do Bruno apareceu sem os
    participantes. Uma instância, sempre.
    """
    outro = running_daemon_pid()
    if outro is not None:
        print(f"[Castanha] Já existe um daemon rodando (PID {outro}). Este não sobe.")
        return False
    arquivo = pid_file()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_pid_file() -> None:
    arquivo = pid_file()
    try:
        if arquivo.exists() and arquivo.read_text(encoding="utf-8").strip() == str(os.getpid()):
            arquivo.unlink()
    except Exception:
        pass


class CastanhaDaemon:
    def __init__(self):
        self.config = load_config()
        self.state_mgr = StateManager()
        self.engine = CastanhaEngine()
        self.running = True
        self.notified_meeting_uids: Set[str] = set()
        self.retry_scheduler = RetryScheduler(
            enabled=self.config.get("sync", {}).get("auto_retry_enabled", False))

    def stop(self, *args):
        self.running = False

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        last_calendar_check = 0.0
        cal_cfg = self.config.get("calendar", {})
        poll_interval = cal_cfg.get("poll_interval_sec", 60)
        notify_before_min = cal_cfg.get("notify_minutes_before", 2)
        feeds = cal_cfg.get("feeds", [])
        zinom_cfg = cal_cfg.get("zinom", {}) or {}
        agenda_ligada = bool(cal_cfg.get("enabled", True)) and (
            bool(feeds) or bool(zinom_cfg.get("enabled", True))
        )

        while self.running:
            now = time.time()
            state = self.state_mgr.read()
            self.retry_scheduler.tick(capture_status=state.get("status", "idle"))

            # 1. Atualizador de cronômetro durante gravação
            if state.get("status") == "recording":
                started_iso = state.get("started_at")
                if started_iso:
                    try:
                        started_dt = datetime.datetime.fromisoformat(started_iso)
                        now_dt = datetime.datetime.now(started_dt.tzinfo) if started_dt.tzinfo else datetime.datetime.now()
                        elapsed = int((now_dt - started_dt).total_seconds())
                        if elapsed != state.get("elapsed_seconds"):
                            self.state_mgr.write({"elapsed_seconds": max(0, elapsed)})
                    except Exception:
                        pass

            # 2. Verificação periódica de calendário (iCal + contas Google do Zinom)
            if agenda_ligada and (now - last_calendar_check > poll_interval):
                last_calendar_check = now
                try:
                    proximas = collect_upcoming(self.config)
                    proxima = next_timed(proximas)
                    self.state_mgr.write({
                        "next_meeting": proxima.to_dict() if proxima else None,
                        "upcoming_meetings": [m.to_dict() for m in proximas],
                        "agenda_error": None,
                    })

                    if proxima:
                        next_m = proxima
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        inicio = next_m.start if next_m.start.tzinfo else next_m.start.replace(
                            tzinfo=datetime.timezone.utc)
                        time_until = (inicio - now_utc).total_seconds()

                        if 0 <= time_until <= (notify_before_min * 60) and next_m.uid not in self.notified_meeting_uids:
                            self.notified_meeting_uids.add(next_m.uid)
                            threading.Thread(
                                target=self._trigger_meeting_alert,
                                args=(next_m,),
                                daemon=True,
                                name=f"alert-{next_m.uid}",
                            ).start()
                except Exception as e:
                    print(f"[Castanha Daemon] Erro ao checar calendário: {e}")
                    self.state_mgr.write({"agenda_error": str(e)})

            time.sleep(1)

    def _trigger_meeting_alert(self, meeting):
        title = meeting.title
        conf_url = meeting.conference_url
        attendees = ", ".join([a.name for a in meeting.attendees[:3]]) or "Sem convidados"

        actions = [("record", "Gravar Reunião 🌰")]
        if conf_url:
            actions.insert(0, ("join", "Entrar na Chamada 🌐"))

        chosen = notify(
            f"Reunião em Instantes: {title}",
            f"Participantes: {attendees}\nClique para entrar ou iniciar a gravação.",
            actions=actions,
            timeout=15000,
        )

        if chosen == "join" and conf_url:
            subprocess.Popen(["xdg-open", conf_url])
            # Se configurado auto_record, inicia
            if self.config.get("calendar", {}).get("auto_record", False):
                self.engine.start_recording(meeting_event=meeting)
        elif chosen == "record":
            self.engine.start_recording(meeting_event=meeting)
            if conf_url:
                subprocess.Popen(["xdg-open", conf_url])

def run_daemon():
    if not claim_pid_file():
        return
    try:
        CastanhaDaemon().run()
    finally:
        release_pid_file()

if __name__ == "__main__":
    run_daemon()
