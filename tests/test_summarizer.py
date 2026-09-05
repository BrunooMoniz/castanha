"""Resumo de reunião longa: a Groq gratuita dá 8.000 tokens por minuto, e uma reunião de 2h não cabe."""

import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from castanha import summarizer as S
from castanha.summarizer import LlmTooLarge, MeetingSummarizer, _dividir_texto


def _http_error(code, body="", headers=None):
    h = Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://api.groq.com", code, "err", h, io.BytesIO(body.encode("utf-8")))


def _resposta(texto):
    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": texto}}]}).encode("utf-8")
    return R()


CORPO_413 = ('{"error":{"message":"Request too large for model `x` on tokens per minute (TPM): '
             'Limit 8000, Requested 23454, please reduce your message size","type":"tokens"}}')


class TestDividirTexto(unittest.TestCase):
    def test_corta_em_fim_de_frase_e_cobre_tudo(self):
        texto = " ".join(f"Frase número {i} da reunião." for i in range(200))
        partes = _dividir_texto(texto, 500)
        self.assertGreater(len(partes), 1)
        for p in partes:
            self.assertLessEqual(len(p), 500)
            self.assertTrue(p.endswith("."), p[-20:])
        self.assertEqual(" ".join(partes), texto)

    def test_texto_curto_fica_inteiro(self):
        self.assertEqual(_dividir_texto("oi. tudo bem?", 100), ["oi. tudo bem?"])

    def test_frase_gigante_leva_corte_seco(self):
        partes = _dividir_texto("a" * 1000, 300)
        self.assertEqual(sum(len(p) for p in partes), 1000)
        self.assertTrue(all(len(p) <= 300 for p in partes))


class TestCallLlm(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.temp), "XDG_STATE_HOME": str(self.temp)})
        self.env.start()
        self.s = MeetingSummarizer()
        self.s.api_key = "gsk_teste"

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_413_vira_LlmTooLarge_com_os_numeros(self):
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=_http_error(413, CORPO_413)):
            with self.assertRaises(LlmTooLarge) as ctx:
                self.s._call_llm("sys", "user")
        self.assertEqual((ctx.exception.limit, ctx.exception.requested), (8000, 23454))

    def test_429_espera_o_retry_after_e_tenta_de_novo(self):
        respostas = [_http_error(429, "rate limit", {"Retry-After": "37"}), _resposta("pronto")]

        def fake(req, timeout=None):
            r = respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=fake), \
             patch("castanha.summarizer.time.sleep") as dorme:
            self.assertEqual(self.s._call_llm("sys", "user"), "pronto")
        self.assertEqual([c.args[0] for c in dorme.call_args_list], [37.0])

    def test_erro_definitivo_devolve_vazio_e_avisa_no_stderr(self):
        err = io.StringIO()
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=_http_error(401, "chave ruim")), \
             patch("castanha.summarizer.sys.stderr", err):
            self.assertEqual(self.s._call_llm("sys", "user"), "")
        self.assertIn("401", err.getvalue())

    def test_sem_chave_nao_chama(self):
        self.s.api_key = ""
        with patch("castanha.summarizer.urllib.request.urlopen") as u:
            self.assertEqual(self.s._call_llm("sys", "user"), "")
        u.assert_not_called()


class TestReuniaoLonga(unittest.TestCase):
    """Simula a Groq: acima de N caracteres de prompt, 413; abaixo, responde."""

    LIMITE_CHARS = 30000

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.temp), "XDG_STATE_HOME": str(self.temp)})
        self.env.start()
        self.s = MeetingSummarizer()
        self.s.api_key = "gsk_teste"
        self.chamadas = []

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _groq_falsa(self, system_prompt, user_prompt, json_mode=False):
        self.chamadas.append((system_prompt, user_prompt, json_mode))
        if len(user_prompt) > self.LIMITE_CHARS:
            pedido = int(len(user_prompt) / 4.1)
            raise LlmTooLarge(8000, pedido, "413")
        if json_mode:
            return json.dumps({"facts": [{"subject": "Nora", "predicate": "decidiu", "object": "x"}],
                               "decisions": ["d"], "action_items": [], "people_notes": []})
        if system_prompt is S.PARTIAL_SYSTEM_PROMPT:
            return "## Resumo da parte\nparte ok\n\n## Decisões\nNenhuma nesta parte"
        if system_prompt is S.COMBINE_SYSTEM_PROMPT:
            return "# Nora Weekly\n\n## 📌 Resumo Executivo\nConsolidado.\n\n## 🎯 Decisões Tomadas\n- d"
        return "# Nora Weekly\n\n## 📌 Resumo Executivo\nCurta."

    def _meta(self):
        return {"title": "Nora Weekly", "recorded_at": "2026-09-05T10:30:00", "duration_seconds": 7969.75,
                "mode": "dual", "audio_status": "ok", "calendar_event": {"attendees": [{"name": "Luigi", "email": "l@x"}]}}

    def test_reuniao_curta_vai_numa_chamada(self):
        with patch.object(MeetingSummarizer, "_call_llm", side_effect=self._groq_falsa):
            silver = self.s.generate_silver(self._meta(), "Fala curta. Outra fala.")
        self.assertEqual(len(self.chamadas), 1)
        self.assertIn("Curta.", silver)

    def test_reuniao_longa_vai_em_partes_e_consolida(self):
        transcricao = " ".join(f"Frase {i} dita na reunião longa da Nora." for i in range(2400))
        self.assertGreater(len(transcricao), self.LIMITE_CHARS)
        with patch.object(MeetingSummarizer, "_call_llm", side_effect=self._groq_falsa):
            silver = self.s.generate_silver(self._meta(), transcricao)

        sistemas = [c[0] for c in self.chamadas]
        self.assertIs(sistemas[0], S.SILVER_SYSTEM_PROMPT, "primeiro tenta inteira")
        parciais = [c for c in self.chamadas if c[0] is S.PARTIAL_SYSTEM_PROMPT]
        self.assertGreaterEqual(len(parciais), 2)
        for _, prompt, _ in parciais:
            self.assertLessEqual(len(prompt), self.LIMITE_CHARS, "cada parte tem que caber")
        self.assertIs(sistemas[-1], S.COMBINE_SYSTEM_PROMPT, "termina consolidando")
        self.assertIn("Consolidado.", silver)
        self.assertIn("## 📝 Transcrição Bruta", silver)
        self.assertIn("Frase 2399 dita", silver, "a transcrição inteira fica anexada")
        self.assertNotIn("Sem resumo", silver)

    def test_toda_a_transcricao_chega_a_alguma_parte(self):
        transcricao = " ".join(f"Frase {i} dita na reunião longa da Nora." for i in range(2400))
        with patch.object(MeetingSummarizer, "_call_llm", side_effect=self._groq_falsa):
            self.s.generate_silver(self._meta(), transcricao)
        texto_enviado = " ".join(c[1] for c in self.chamadas if c[0] is S.PARTIAL_SYSTEM_PROMPT)
        for i in (0, 777, 2399):
            self.assertIn(f"Frase {i} dita", texto_enviado)

    def test_consolidacao_que_nao_cabe_mantem_as_parciais(self):
        transcricao = " ".join(f"Frase {i} dita na reunião longa da Nora." for i in range(2400))

        def sem_consolidar(system_prompt, user_prompt, json_mode=False):
            if system_prompt is S.COMBINE_SYSTEM_PROMPT:
                raise LlmTooLarge(8000, 9000, "413")
            return self._groq_falsa(system_prompt, user_prompt, json_mode)

        with patch.object(MeetingSummarizer, "_call_llm", side_effect=sem_consolidar):
            silver = self.s.generate_silver(self._meta(), transcricao)
        self.assertIn("### Parte 1 de", silver)
        self.assertIn("Resumo Executivo", silver)
        self.assertNotIn("Sem resumo", silver)

    def test_gold_nao_manda_a_transcricao_que_esta_no_silver(self):
        transcricao = " ".join(f"Frase {i} dita na reunião longa da Nora." for i in range(2400))
        silver = "# Nora\n\n## 📌 Resumo Executivo\nok\n\n## 📝 Transcrição Bruta\n" + transcricao
        with patch.object(MeetingSummarizer, "_call_llm", side_effect=self._groq_falsa):
            gold = self.s.generate_gold(self._meta(), silver, transcricao)
        _, prompt, json_mode = self.chamadas[-1]
        self.assertTrue(json_mode)
        self.assertLess(len(prompt), 8000)
        self.assertNotIn("Frase 2399 dita", prompt)
        self.assertEqual(gold["decisions"], ["d"])

    def test_erro_de_llm_nao_vira_resumo_inventado(self):
        with patch.object(MeetingSummarizer, "_call_llm", return_value=""):
            silver = self.s.generate_silver(self._meta(), "Fala. Fala.")
        self.assertIn("Sem resumo", silver)


if __name__ == "__main__":
    unittest.main()
