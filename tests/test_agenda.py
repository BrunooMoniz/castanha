"""Evento de dia inteiro na agenda: aparece na lista, mas não vira 'a próxima reunião'."""

import datetime
import shutil
import tempfile
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


if __name__ == "__main__":
    unittest.main()
