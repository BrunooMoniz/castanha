"""Evento de dia inteiro na agenda: aparece na lista, mas não vira 'a próxima reunião'."""

import datetime
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.agenda import collect_upcoming, next_timed
from castanha.calendar import MeetingEvent


def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def _timed(uid, title, em_horas):
    agora = datetime.datetime.now(datetime.timezone.utc)
    return MeetingEvent(uid=uid, title=title, start=agora + datetime.timedelta(hours=em_horas),
                        end=agora + datetime.timedelta(hours=em_horas + 1), attendees=[])


def _all_day(uid, title, dias=0):
    hoje = datetime.datetime.now(_local_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    inicio = hoje + datetime.timedelta(days=dias)
    return MeetingEvent(uid=uid, title=title, start=inicio, end=inicio + datetime.timedelta(days=1),
                        attendees=[], all_day=True)


class TestAgendaDiaInteiro(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp)})
        self.env.start()
        self.cfg = {"calendar": {"feeds": [{"url": "x"}], "zinom": {"enabled": False}}, "zinom": {"token": ""}}

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _coletar(self, eventos):
        with patch("castanha.agenda.get_upcoming_meetings", return_value=eventos):
            return collect_upcoming(self.cfg)

    def test_dia_inteiro_de_hoje_aparece_mesmo_tendo_comecado_a_meia_noite(self):
        eventos = [_all_day("lembrete", "Pagar condomínio"), _timed("reuniao_20260905T130000Z", "Nora Weekly", 2)]
        titulos = [m.title for m in self._coletar(eventos)]
        self.assertIn("Pagar condomínio", titulos)

    def test_reuniao_com_hora_vem_antes_do_dia_inteiro(self):
        eventos = [_all_day("lembrete", "Pagar condomínio"), _timed("reuniao_20260905T130000Z", "Nora Weekly", 2)]
        titulos = [m.title for m in self._coletar(eventos)]
        self.assertEqual(titulos, ["Nora Weekly", "Pagar condomínio"])

    def test_dia_inteiro_de_ontem_nao_aparece(self):
        eventos = [_all_day("ontem", "Coisa de ontem", dias=-1)]
        self.assertEqual(self._coletar(eventos), [])

    def test_dia_inteiro_nao_e_a_proxima_reuniao(self):
        eventos = [_all_day("lembrete", "Pagar condomínio"), _timed("reuniao_20260905T130000Z", "Nora Weekly", 2)]
        proxima = next_timed(self._coletar(eventos))
        self.assertEqual(proxima.title, "Nora Weekly")

    def test_so_dia_inteiro_nao_tem_proxima(self):
        eventos = [_all_day("lembrete", "Pagar condomínio")]
        self.assertIsNone(next_timed(self._coletar(eventos)))

    def test_escondido_some_tambem_quando_e_dia_inteiro(self):
        from castanha import hidden
        hidden.hide("lembrete_20260905", "Pagar condomínio")
        eventos = [_all_day("lembrete_20260905", "Pagar condomínio")]
        self.assertEqual(self._coletar(eventos), [])


class TestZinomDiaInteiro(unittest.TestCase):
    def test_data_de_dia_inteiro_fica_no_fuso_local(self):
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        raw = {"id": "abc_20260905", "summary": "Aniversário", "start": {"date": "2026-09-05"}, "end": {"date": "2026-09-06"}}
        ev = fonte._to_meeting(raw, {"summary": "Principal", "email": "x@y"})
        self.assertTrue(ev.all_day)
        self.assertEqual((ev.start.year, ev.start.month, ev.start.day, ev.start.hour), (2026, 9, 5, 0))
        self.assertEqual(ev.start.tzinfo, _local_tz())
        self.assertEqual(ev.end - ev.start, datetime.timedelta(days=1))

    def test_dia_inteiro_entra_por_padrao(self):
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        self.assertFalse(fonte.skip_all_day)

    def test_sem_lista_fica_so_a_agenda_principal(self):
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        fonte._calendars = [
            {"calendar_ref": "a@x", "summary": "a@x", "primary": True},
            {"calendar_ref": "feriados", "summary": "Feriados", "primary": False},
        ]
        import time as _t
        fonte._calendars_at = _t.time()
        self.assertEqual([c["calendar_ref"] for c in fonte.selected_calendars()], ["a@x"])

    def test_sem_lista_entram_as_agendas_que_ele_pode_editar(self):
        """07/09: a reunião do dia só existia na agenda de grupo 'Eventos Nora'."""
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        fonte._calendars = [
            {"calendar_ref": "principal", "summary": "Bruno", "primary": True, "accessRole": "owner"},
            {"calendar_ref": "grupo", "summary": "Eventos Nora", "primary": False, "accessRole": "owner"},
            {"calendar_ref": "compartilhada", "summary": "Time", "primary": False, "accessRole": "writer"},
            {"calendar_ref": "feriados", "summary": "Feriados", "primary": False, "accessRole": "reader"},
            {"calendar_ref": "de-outro", "summary": "luigi@x", "primary": False, "accessRole": "freeBusyReader"},
        ]
        import time as _t
        fonte._calendars_at = _t.time()
        self.assertEqual([c["calendar_ref"] for c in fonte.selected_calendars()],
                         ["principal", "grupo", "compartilhada"])

    def test_agenda_de_grupo_nao_vira_participante_nem_organizadora(self):
        """Evento real de 07/09: organizer e um attendee eram a própria 'Eventos Nora'."""
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        grupo = "c_abc123@group.calendar.google.com"
        raw = {
            "id": "ev1", "summary": "Contrato Primata",
            "start": {"dateTime": "2026-09-07T16:00:00-03:00"}, "end": {"dateTime": "2026-09-07T16:45:00-03:00"},
            "organizer": {"email": grupo, "name": "Eventos Nora", "self": True},
            "attendees": [
                {"email": grupo, "name": "Eventos Nora", "response": "accepted", "organizer": True},
                {"email": "luigi@x", "name": "Luigi", "response": "needsAction"},
            ],
        }
        ev = fonte._to_meeting(raw, {"calendar_ref": "grupo", "summary": "Eventos Nora", "email": "moniz@x"})
        self.assertEqual([a.email for a in ev.attendees], ["luigi@x"])
        self.assertEqual(ev.organizer, "moniz@x")

    def test_lista_explicita_continua_mandando(self):
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {"calendars": ["Eventos Nora"]}}})
        fonte._calendars = [
            {"calendar_ref": "principal", "summary": "Bruno", "primary": True, "accessRole": "owner"},
            {"calendar_ref": "grupo", "summary": "Eventos Nora", "primary": False, "accessRole": "owner"},
        ]
        import time as _t
        fonte._calendars_at = _t.time()
        self.assertEqual([c["calendar_ref"] for c in fonte.selected_calendars()], ["grupo"])


if __name__ == "__main__":
    unittest.main()


class TestAgendaAviso(unittest.TestCase):
    """O aviso da agenda em linguagem de produto, e o daemon gravando-o no estado."""

    def setUp(self):
        from castanha import agenda
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp / "state"),
                                             "XDG_CONFIG_HOME": str(self.temp / "config")})
        self.env.start()
        self.cfg = {"calendar": {"feeds": [], "zinom": {}}, "zinom": {"token": "t"}}
        agenda._zinom = None
        agenda._erro_coleta = None

    def tearDown(self):
        from castanha import agenda
        agenda._zinom = None
        agenda._erro_coleta = None
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _fonte(self, last_error=None, good_at=0.0):
        from castanha import agenda
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar(self.cfg)
        fonte.last_error = last_error
        fonte._good_at = good_at
        agenda._zinom = fonte
        return fonte

    def test_sem_erro_nao_ha_aviso(self):
        from castanha.agenda import agenda_warning
        self._fonte()
        self.assertIsNone(agenda_warning(self.cfg))

    def test_sem_ciclo_bom_diz_indisponivel(self):
        from castanha.agenda import agenda_warning
        self._fonte(last_error="limite de chamadas do Zinom")
        self.assertEqual(agenda_warning(self.cfg), "Agenda indisponível: limite de chamadas do Zinom")

    def test_depois_de_ciclo_bom_diz_desde_quando(self):
        import time
        from castanha.agenda import agenda_warning
        bom_em = time.time() - 600
        self._fonte(last_error="sem conexão com o Zinom", good_at=bom_em)
        hora = time.strftime("%H:%M", time.localtime(bom_em))
        self.assertEqual(agenda_warning(self.cfg), f"Agenda desatualizada desde {hora}: sem conexão com o Zinom")

    def test_ciclo_bom_de_mais_de_um_dia_traz_a_data(self):
        import time
        from castanha.agenda import agenda_warning
        bom_em = time.time() - 2 * 86400
        self._fonte(last_error="sem conexão com o Zinom", good_at=bom_em)
        self.assertIn(time.strftime("%d/%m %H:%M", time.localtime(bom_em)), agenda_warning(self.cfg))

    def test_excecao_da_coleta_vira_aviso_e_some_no_ciclo_bom(self):
        from castanha.agenda import agenda_warning
        fonte = self._fonte()
        with patch.object(fonte, "upcoming", side_effect=RuntimeError("<urlopen error timed out>")):
            self.assertEqual(collect_upcoming(self.cfg), [])
        self.assertEqual(agenda_warning(self.cfg), "Agenda indisponível: sem conexão com o Zinom")
        with patch.object(fonte, "upcoming", return_value=[]):
            collect_upcoming(self.cfg)
        self.assertIsNone(agenda_warning(self.cfg))

    def test_erro_explicito_do_daemon_vira_o_mesmo_texto(self):
        from castanha.agenda import agenda_warning
        self._fonte()
        self.assertEqual(agenda_warning(self.cfg, RuntimeError("HTTP 429: Too Many Requests")),
                         "Agenda indisponível: limite de chamadas do Zinom")

    # ---- o daemon escreve o aviso no estado
    def _daemon(self):
        import json
        from castanha.daemon import CastanhaDaemon
        cfg_dir = self.temp / "config" / "castanha"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        meetings = self.temp / "meetings"
        (cfg_dir / "config.json").write_text(json.dumps({
            "storage": {"base_dir": str(meetings), "bronze_dir": str(meetings / "bronze"),
                        "silver_dir": str(meetings / "silver"), "gold_dir": str(meetings / "gold")},
            "transcription": {"groq_api_key": "", "vps_ssh_host": ""},
            "llm": {"api_key": ""},
            "zinom": {"enabled": False, "token": "t"},
        }), encoding="utf-8")
        daemon = CastanhaDaemon()

        def _para(_):
            daemon.running = False
        return daemon, _para

    def test_daemon_grava_o_aviso_no_estado_a_cada_ciclo(self):
        from castanha.state import StateManager
        daemon, para = self._daemon()
        self._fonte(last_error="limite de chamadas do Zinom")
        with patch("castanha.daemon.collect_upcoming", return_value=[]), \
             patch("castanha.daemon.signal.signal"), patch("castanha.daemon.time.sleep", side_effect=para):
            daemon.run()
        estado = StateManager().read()
        self.assertEqual(estado["agenda_error"], "Agenda indisponível: limite de chamadas do Zinom")
        self.assertEqual(estado["upcoming_meetings"], [])

    def test_daemon_limpa_o_aviso_quando_o_ciclo_e_bom(self):
        from castanha.state import StateManager
        daemon, para = self._daemon()
        StateManager().write({"agenda_error": "Agenda indisponível: limite de chamadas do Zinom"})
        self._fonte()
        with patch("castanha.daemon.collect_upcoming", return_value=[]), \
             patch("castanha.daemon.signal.signal"), patch("castanha.daemon.time.sleep", side_effect=para):
            daemon.run()
        self.assertIsNone(StateManager().read()["agenda_error"])

    def test_daemon_traduz_a_propria_excecao(self):
        from castanha.state import StateManager
        daemon, para = self._daemon()
        self._fonte()
        with patch("castanha.daemon.collect_upcoming", side_effect=RuntimeError("HTTP 429: Too Many Requests")), \
             patch("castanha.daemon.signal.signal"), patch("castanha.daemon.time.sleep", side_effect=para):
            daemon.run()
        self.assertEqual(StateManager().read()["agenda_error"], "Agenda indisponível: limite de chamadas do Zinom")

    def test_daemon_preserva_eventos_quando_a_atualizacao_falha(self):
        from castanha.state import StateManager
        daemon, _ = self._daemon()
        anteriores = [{"uid": "existente", "title": "Reunião preservada"}]
        daemon.state_mgr.write({
            "next_meeting": anteriores[0],
            "upcoming_meetings": anteriores,
        })

        daemon._apply_agenda_result(([], RuntimeError("HTTP 429: Too Many Requests")))

        estado = StateManager().read()
        self.assertEqual(estado["next_meeting"], anteriores[0])
        self.assertEqual(estado["upcoming_meetings"], anteriores)
        self.assertEqual(estado["agenda_error"], "Agenda indisponível: limite de chamadas do Zinom")

    def test_aviso_e_da_reuniao_prestes_a_comecar_mesmo_com_outra_em_andamento(self):
        """A em andamento vira `next_meeting` (é a que se grava); o aviso é da emenda."""
        from castanha.state import StateManager
        daemon, _ = self._daemon()
        agora = datetime.datetime.now(datetime.timezone.utc)
        em_curso = MeetingEvent(uid="em_curso", title="Em curso", attendees=[],
                                start=agora - datetime.timedelta(minutes=30), end=agora + datetime.timedelta(minutes=30))
        emenda = MeetingEvent(uid="emenda", title="Emenda", attendees=[],
                              start=agora + datetime.timedelta(seconds=60), end=agora + datetime.timedelta(minutes=61))
        self._fonte()
        avisadas = []
        with patch.object(daemon, "_trigger_meeting_alert", side_effect=lambda m: avisadas.append(m.uid)):
            daemon._apply_agenda_result(([em_curso, emenda], None))
            for th in threading.enumerate():
                if th.name.startswith("alert-"):
                    th.join(timeout=2)
        self.assertEqual(avisadas, ["emenda"])
        self.assertEqual(daemon.notified_meeting_uids, {"emenda"})
        self.assertEqual(StateManager().read()["next_meeting"]["uid"], "em_curso")

    def test_agenda_lenta_nao_bloqueia_a_projecao_do_audio(self):
        daemon, _ = self._daemon()
        daemon.state_mgr.write({
            "status": "recording",
            "started_at": datetime.datetime.now().isoformat(),
            "audio_peak_path": "/tmp/castanha_rec_1.peak",
        })
        release = threading.Event()
        sleeps = 0

        def slow_agenda(_config):
            release.wait(timeout=1)
            return []

        def stop_after_two_ticks(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 2:
                daemon.running = False
                release.set()

        with patch("castanha.daemon.collect_upcoming", side_effect=slow_agenda), \
             patch("castanha.daemon.read_audio_peak", return_value=0.8) as peak, \
             patch("castanha.daemon.signal.signal"), \
             patch("castanha.daemon.time.sleep", side_effect=stop_after_two_ticks):
            daemon.run()
        if daemon._agenda_thread:
            daemon._agenda_thread.join(timeout=1)

        self.assertGreaterEqual(peak.call_count, 2)
        self.assertEqual(daemon.state_mgr.read()["audio_peak"], 0.8)


class TestAgendaEmAndamento(unittest.TestCase):
    """Reunião que já começou fica na lista até acabar: é a que ele quer gravar.

    Em 12/09/2026 a Nora Weekly das 10:30 começou com a máquina suspensa. A
    agenda só voltou às 11:02, e o corte de 15 minutos depois do começo já tinha
    descartado a reunião: o painel não a mostrou e ele gravou na mão.
    """

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp)})
        self.env.start()
        self.cfg = {"calendar": {"feeds": [{"url": "x"}], "zinom": {"enabled": False}}, "zinom": {"token": ""}}

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _coletar(self, eventos):
        with patch("castanha.agenda.get_upcoming_meetings", return_value=eventos):
            return collect_upcoming(self.cfg)

    @staticmethod
    def _reuniao(uid, title, comecou_ha_min, dura_min=60):
        agora = datetime.datetime.now(datetime.timezone.utc)
        inicio = agora - datetime.timedelta(minutes=comecou_ha_min)
        return MeetingEvent(uid=uid, title=title, start=inicio,
                            end=inicio + datetime.timedelta(minutes=dura_min), attendees=[])

    def test_em_andamento_continua_na_lista_e_e_a_proxima(self):
        eventos = [self._reuniao("weekly_20260912T133000Z", "Nora Weekly", comecou_ha_min=32, dura_min=60),
                   _timed("depois", "Outra", 3)]
        lista = self._coletar(eventos)
        self.assertEqual([m.title for m in lista], ["Nora Weekly", "Outra"])
        self.assertEqual(next_timed(lista).title, "Nora Weekly")

    def test_reuniao_que_acabou_sai(self):
        eventos = [self._reuniao("acabou", "Já acabou", comecou_ha_min=90, dura_min=60)]
        self.assertEqual(self._coletar(eventos), [])

    def test_sem_hora_de_fim_vale_a_carencia_de_15_minutos(self):
        recente = self._reuniao("recente", "Sem fim, recente", comecou_ha_min=10, dura_min=0)
        antiga = self._reuniao("antiga", "Sem fim, antiga", comecou_ha_min=20, dura_min=0)
        self.assertEqual([m.title for m in self._coletar([recente, antiga])], ["Sem fim, recente"])

    def test_fora_da_janela_continua_fora(self):
        eventos = [_timed("longe", "Amanhã de noite", 30)]
        self.assertEqual(self._coletar(eventos), [])
