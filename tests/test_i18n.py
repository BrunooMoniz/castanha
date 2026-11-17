"""Catálogo i18n: pt-BR padrão, inglês por escolha explícita, sem depender do ambiente."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.i18n import STRINGS, audio_status_message, resolve_locale, t


class TestResolveLocale(unittest.TestCase):
    def _resolve(self, **env):
        with patch.dict(os.environ, env, clear=True):
            return resolve_locale()

    def test_castanha_lang_beats_lang(self):
        self.assertEqual(self._resolve(CASTANHA_LANG="pt_BR", LANG="en_US.UTF-8"), "pt")

    def test_castanha_lang_beats_lc_all(self):
        self.assertEqual(self._resolve(CASTANHA_LANG="en", LC_ALL="pt_BR.UTF-8"), "en")

    def test_castanha_lang_case_insensitive(self):
        self.assertEqual(self._resolve(CASTANHA_LANG="PT_br"), "pt")

    def test_locale_desconhecido_resolves_en(self):
        self.assertEqual(self._resolve(CASTANHA_LANG="xx_YY"), "en")

    def test_sem_nada_resolves_pt(self):
        self.assertEqual(self._resolve(), "pt")

    def test_desktop_ingles_nao_troca_idioma_do_produto(self):
        self.assertEqual(self._resolve(LANG="en_US.UTF-8", LC_ALL="C"), "pt")


class TestT(unittest.TestCase):
    def test_formata_placeholders(self):
        self.assertEqual(t("cli.notes.not_found", locale="en", slug="x"), "Meeting 'x' not found.")
        self.assertEqual(t("cli.notes.not_found", locale="pt", slug="x"), "Reunião 'x' não encontrada.")

    def test_chave_inexistente_devolve_a_propria_chave(self):
        self.assertEqual(t("nope.nunca", locale="pt"), "nope.nunca")

    def test_toda_chave_tem_en_e_pt(self):
        for key, entry in STRINGS.items():
            with self.subTest(key=key):
                self.assertTrue(entry.get("en"))
                self.assertTrue(entry.get("pt"))


class TestAudioStatusMessage(unittest.TestCase):
    def test_en(self):
        self.assertEqual(audio_status_message("ok", locale="en"), "Audio captured on both channels.")
        self.assertEqual(
            audio_status_message("mic_mudo", locale="en"),
            "The microphone channel was silent: the mic was muted (keyboard or system). Only the call audio was recorded.",
        )
        self.assertEqual(audio_status_message("audio_apagado", locale="en"),
                         "Audio deleted to free space (notes and transcript preserved).")

    def test_pt(self):
        self.assertEqual(audio_status_message("ok", locale="pt"), "Áudio capturado nos dois canais.")
        self.assertEqual(
            audio_status_message("mic_mudo", locale="pt"),
            "O canal do microfone saiu em silêncio: o mic estava mudo (teclado ou sistema). "
            "Só o áudio da chamada foi gravado.",
        )
        self.assertEqual(audio_status_message("sem_audio", locale="pt"),
                         "Nenhum canal captou áudio: a gravação está em silêncio do início ao fim.")
        self.assertEqual(audio_status_message("desconhecido", locale="pt"),
                         "Não foi possível medir os níveis do áudio.")

    def test_status_desconhecido_devolve_vazio(self):
        self.assertEqual(audio_status_message("status_que_nao_existe", locale="pt"), "")


class TestCliHelpLocale(unittest.TestCase):
    def setUp(self):
        self.cli_bin = Path(__file__).resolve().parent.parent / "bin" / "castanha"

    def _help(self, lang, *args):
        env = dict(os.environ)
        env["CASTANHA_LANG"] = lang
        return subprocess.run(
            [sys.executable, str(self.cli_bin), *args, "--help"],
            capture_output=True, text=True, env=env,
        )

    def test_help_em_ingles(self):
        res = self._help("en", "status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Output in JSON format", res.stdout)
        self.assertNotIn("Saída em formato JSON", res.stdout)
        top = self._help("en")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("Show the recorder's current status", top.stdout)

    def test_help_em_portugues(self):
        res = self._help("pt_BR", "status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Saída em formato JSON", res.stdout)
        top = self._help("pt_BR")
        self.assertIn("Exibe o status atual do gravador", top.stdout)


if __name__ == "__main__":
    unittest.main()
