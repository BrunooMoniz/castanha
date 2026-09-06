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
        # 400 é pedido inválido: repetir não ajuda, então não vira pendência.
        err = io.StringIO()
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=_http_error(400, "pedido ruim")), \
             patch("castanha.summarizer.sys.stderr", err):
            self.assertEqual(self.s._call_llm("sys", "user"), "")
        self.assertIn("400", err.getvalue())

    def test_cota_esgotada_em_todas_as_tentativas_vira_LlmUnavailable(self):
        from castanha.summarizer import LlmUnavailable
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=_http_error(429, "tpm")), \
             patch("castanha.summarizer.time.sleep"), patch("castanha.summarizer.sys.stderr", io.StringIO()):
            with self.assertRaises(LlmUnavailable):
                self.s._call_llm("sys", "user")

    def test_chave_recusada_fica_pendente_em_vez_de_nota_sem_resumo(self):
        from castanha.summarizer import LlmUnavailable
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=_http_error(401, "chave ruim")), \
             patch("castanha.summarizer.sys.stderr", io.StringIO()):
            with self.assertRaises(LlmUnavailable) as ctx:
                self.s._call_llm("sys", "user")
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("chave ruim", str(ctx.exception))

    def test_provedor_que_nao_e_groq_nunca_chama_a_groq(self):
        from castanha.summarizer import LlmUnavailable
        self.s.provider = "openai"
        with patch("castanha.summarizer.urllib.request.urlopen") as u:
            with self.assertRaises(LlmUnavailable):
                self.s._call_llm("sys", "user")
        u.assert_not_called()

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

    def test_gold_tenta_sem_modo_json_e_extrai_o_objeto(self):
        chamadas = []

        def groq(system_prompt, user_prompt, json_mode=False):
            chamadas.append(json_mode)
            if json_mode:
                return ""  # json_validate_failed
            return 'Aqui vai:\n```json\n{"facts": [], "decisions": ["d2"], "action_items": [], "people_notes": []}\n```'

        with patch.object(MeetingSummarizer, "_call_llm", side_effect=groq):
            gold = self.s.generate_gold(self._meta(), "# n", "Fala.")
        self.assertEqual(chamadas, [True, False])
        self.assertEqual(gold["decisions"], ["d2"])

    def test_erro_de_llm_nao_vira_resumo_inventado(self):
        with patch.object(MeetingSummarizer, "_call_llm", return_value=""):
            silver = self.s.generate_silver(self._meta(), "Fala. Fala.")
        self.assertIn("Sem resumo", silver)


class TestCalendarGrounding(unittest.TestCase):
    def setUp(self):
        with patch('castanha.summarizer.load_config', return_value={'llm': {'api_key': ''}}):
            self.s = MeetingSummarizer()
        self.meta = {'title': 'Reunião sintética', 'calendar_event': {
            'attendees': [{'name': 'Luigi Rossi', 'email': 'luigi@example.invalid',
                           'responseStatus': 'accepted'}]}}
        self.transcript = 'Áudio do sistema: orçamento aprovado para outubro.'

    def test_silver_refuses_hallucinated_calendar_person_or_collective_attendance(self):
        for claim in ('Luigi participou da reunião.', 'LUIGI ROSSI estava presente.',
                      'luigi@example.invalid confirmou o orçamento.', 'Todos os convidados participaram.',
                      'Rossi explicou a proposta.'):
            with self.subTest(claim=claim), patch.object(self.s, '_call_llm', return_value=claim):
                silver = self.s.generate_silver(self.meta, self.transcript)
            self.assertIn('Resumo retido', silver)
            self.assertNotIn(claim, silver)
            self.assertIn(self.transcript, silver)
            self.assertIn('evidence: "calendar_invitation"', silver)
            self.assertIn('rsvp: "accepted"', silver)
            self.assertIn('presence: "unverified"', silver)
            self.assertIn('speech: "unverified"', silver)

    def test_gold_filters_calendar_claims_in_every_collection(self):
        fake = {'facts': [{'subject': 'Luigi', 'predicate': 'participou', 'object': 'reunião'},
                          {'subject': 'Projeto', 'predicate': 'custo', 'object': '10'}],
                'decisions': ['Luigi aprovou o orçamento', 'Orçamento aprovado'],
                'action_items': [{'task': 'Enviar documento', 'assignee': 'Rossi'},
                                 {'task': 'Planejar', 'assignee': None}],
                'people_notes': [{'name': 'Luigi Rossi', 'note': 'participou'},
                                 {'name': 'luigi@example.invalid', 'note': 'expert'}]}
        with patch.object(self.s, '_call_llm', return_value=json.dumps(fake)):
            gold = self.s.generate_gold(self.meta, 'Notas', self.transcript)
        self.assertEqual(gold, {'facts': [fake['facts'][1]], 'decisions': ['Orçamento aprovado'],
                                'action_items': [fake['action_items'][1]], 'people_notes': []})

    def test_mention_invitation_or_rsvp_never_proves_voice(self):
        for transcript in ('Convidamos Luigi.', 'Luigi aceitou o convite.',
                           'Vamos falar com Luigi depois.', 'Eu acho que Luigi esteve lá.'):
            with self.subTest(transcript=transcript), patch.object(self.s, '_call_llm', return_value=json.dumps({
                'facts': [{'subject': 'Luigi', 'predicate': 'participou', 'object': 'reunião'}],
                'people_notes': [{'name': 'Luigi', 'note': 'presente'}]})):
                gold = self.s.generate_gold(self.meta, 'Luigi participou', transcript)
            self.assertEqual(gold['facts'], [])
            self.assertEqual(gold['people_notes'], [])

    def test_chunked_summary_final_output_passes_same_barrier(self):
        calls = [LlmTooLarge(100, 200, '413')]
        with patch.object(self.s, '_call_llm', side_effect=calls), \
             patch.object(self.s, '_silver_em_partes', return_value='Luigi participou da reunião.'):
            silver = self.s.generate_silver(self.meta, self.transcript)
        self.assertIn('Resumo retido', silver)
        self.assertNotIn('Luigi participou', silver)
        self.assertIn(self.transcript, silver)

    def test_generated_person_notes_without_calendar_voice_evidence_are_refused(self):
        # Nome de agenda nunca recebe identidade pelo canal, mesmo citado no áudio.
        with patch.object(self.s, '_call_llm', return_value=json.dumps({
                'people_notes': [{'name': 'Luigi', 'note': 'sabe sobre orçamento'}]})):
            gold = self.s.generate_gold(self.meta, 'nota', 'Áudio do sistema: Luigi sabe sobre orçamento.')
        self.assertEqual(gold['people_notes'], [])


    def test_native_calendar_response_and_organizer_are_separate_from_attendance(self):
        self.meta['calendar_event']['attendees'][0] = {
            'name': 'Luigi Rossi', 'email': 'luigi@example.invalid', 'response': 'accepted'}
        self.meta['calendar_event']['organizer'] = 'owner@example.invalid'
        with patch.object(self.s, '_call_llm', return_value='owner@example.invalid aprovou o orçamento.'):
            silver = self.s.generate_silver(self.meta, self.transcript)
        self.assertIn('Resumo retido', silver)
        self.assertIn('rsvp: "accepted"', silver)
        self.assertIn('presence: "unverified"', silver)
        with patch.object(self.s, '_call_llm', return_value=json.dumps({
                'facts': [{'subject': 'owner@example.invalid', 'predicate': 'aprovou', 'object': 'orçamento'}]})):
            gold = self.s.generate_gold(self.meta, 'nota', self.transcript)
        self.assertEqual(gold['facts'], [])


if __name__ == "__main__":
    unittest.main()
