"""Eventos marcados como 'não mostrar'."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha import hidden


class TestSeriesKey(unittest.TestCase):
    def test_instancia_de_recorrente_vira_chave_da_serie(self):
        # Esconder uma ocorrência esconde a série: é lembrete semanal.
        self.assertEqual(hidden.series_key("h4jh17v524h2appmv7eiiu0a6o_20260905T120000Z"),
                         "h4jh17v524h2appmv7eiiu0a6o")
        self.assertEqual(hidden.series_key("abc123_20260904"), "abc123")

    def test_evento_simples_fica_como_esta(self):
        self.assertEqual(hidden.series_key("evento-avulso-42"), "evento-avulso-42")

    def test_uid_de_ical_com_underscore_nao_e_mutilado(self):
        self.assertEqual(hidden.series_key("meeting_abc@google.com"), "meeting_abc@google.com")

    def test_vazio(self):
        for v in ("", None, "   "):
            self.assertEqual(hidden.series_key(v), "")


class TestHidden(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_esconder_e_reexibir(self):
        chave = hidden.hide("serie_20260905T120000Z", "Pagar condomínio")
        self.assertEqual(chave, "serie")
        self.assertTrue(hidden.is_hidden("serie_20260905T120000Z"))
        # Outra ocorrência da MESMA série também está escondida.
        self.assertTrue(hidden.is_hidden("serie_20261005T120000Z"))
        self.assertTrue(hidden.unhide("serie"))
        self.assertFalse(hidden.is_hidden("serie_20260905T120000Z"))

    def test_reexibir_pelo_id_da_instancia(self):
        hidden.hide("serie_20260905T120000Z", "x")
        self.assertTrue(hidden.unhide("serie_20261005T120000Z"))
        self.assertFalse(hidden.is_hidden("serie"))

    def test_nao_esconde_evento_diferente(self):
        hidden.hide("serie-a_20260905T120000Z", "A")
        self.assertFalse(hidden.is_hidden("serie-b_20260905T120000Z"))

    def test_listar_traz_titulo(self):
        hidden.hide("abc", "Fechamento Semanal")
        listado = hidden.listar()
        self.assertEqual(len(listado), 1)
        self.assertEqual(listado[0]["title"], "Fechamento Semanal")

    def test_limpar_tudo(self):
        hidden.hide("a", "A")
        hidden.hide("b", "B")
        self.assertEqual(hidden.unhide_all(), 2)
        self.assertEqual(hidden.listar(), [])

    def test_arquivo_corrompido_nao_derruba(self):
        hidden.hidden_file().parent.mkdir(parents=True, exist_ok=True)
        hidden.hidden_file().write_text("isto não é json", encoding="utf-8")
        self.assertEqual(hidden.load_hidden(), {})
        self.assertFalse(hidden.is_hidden("qualquer"))

    def test_agenda_filtra_o_que_esta_escondido(self):
        import datetime
        from castanha.agenda import collect_upcoming
        from castanha.calendar import MeetingEvent

        agora = datetime.datetime.now(datetime.timezone.utc)
        eventos = [
            MeetingEvent(uid="lembrete_20260905T120000Z", title="Pagar condomínio",
                         start=agora + datetime.timedelta(hours=1),
                         end=agora + datetime.timedelta(hours=2), attendees=[]),
            MeetingEvent(uid="reuniao_20260905T130000Z", title="Nora Weekly",
                         start=agora + datetime.timedelta(hours=3),
                         end=agora + datetime.timedelta(hours=4), attendees=[]),
        ]
        hidden.hide("lembrete", "Pagar condomínio")

        cfg = {"calendar": {"feeds": [], "zinom": {"enabled": False}}, "zinom": {"token": ""}}
        with patch("castanha.agenda.get_upcoming_meetings", return_value=eventos):
            cfg["calendar"]["feeds"] = [{"url": "x"}]
            restantes = collect_upcoming(cfg)

        self.assertEqual([m.title for m in restantes], ["Nora Weekly"])


if __name__ == "__main__":
    unittest.main()
