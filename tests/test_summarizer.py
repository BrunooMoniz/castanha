"""Resumo de reunião longa: a Groq gratuita dá 8.000 tokens por minuto, e uma reunião de 2h não cabe."""

import io
import json
import re
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
        self.s.provider = "groq"  # Estes testes exercitam a Groq; o padrão agora é hermes_ssh.
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
        self.s.provider = "groq"  # Estes testes exercitam a Groq; o padrão agora é hermes_ssh.
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

    def test_gold_filters_presence_claims_in_every_collection_but_keeps_calendar_names(self):
        # Presença/fala continua barrada. Nome da agenda sozinho não barra mais:
        # o fato é citado e apagável por origem, e "Nora" é sujeito legítimo.
        fake = {'facts': [{'subject': 'Luigi', 'predicate': 'participou', 'object': 'reunião'},
                          {'subject': 'Projeto', 'predicate': 'custo', 'object': '10',
                           'evidencia': 'o projeto custa 10 mil reais'}],
                'decisions': [{'decision': 'Luigi aprovou o orçamento', 'evidencia': 'aprovou o orçamento de outubro'},
                              {'decision': 'Rossi falou do orçamento', 'evidencia': 'x'}],
                'action_items': [{'task': 'Enviar documento', 'assignee': 'Rossi', 'evidencia': 'enviar o documento até sexta'},
                                 {'task': 'Planejar', 'assignee': None, 'evidencia': 'quem esteve presente planeja'}],
                'people_notes': [{'name': 'Luigi Rossi', 'note': 'participou'},
                                 {'name': 'luigi@example.invalid', 'note': 'expert'}]}
        with patch.object(self.s, '_call_llm', return_value=json.dumps(fake)):
            gold = self.s.generate_gold(self.meta, 'Notas', self.transcript)
        self.assertEqual(gold, {'facts': [fake['facts'][1]], 'decisions': [fake['decisions'][0]],
                                'action_items': [fake['action_items'][0]],
                                'people_notes': [fake['people_notes'][1]]})
        self.assertEqual(gold['facts'][0]['evidencia'], 'o projeto custa 10 mil reais', 'a passagem citada fica no Gold local')

    def test_gold_prompt_asks_for_a_verbatim_quote_per_item(self):
        self.assertIn('"evidencia"', S.GOLD_SYSTEM_PROMPT)
        self.assertIn('LITERALMENTE', S.GOLD_SYSTEM_PROMPT)
        self.assertIn('"decision": str, "evidencia": str', S.GOLD_SYSTEM_PROMPT)

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

    def test_generated_person_notes_keep_context_but_never_presence(self):
        # Contexto sobre um convidado fica; presença ou fala atribuída não.
        with patch.object(self.s, '_call_llm', return_value=json.dumps({
                'people_notes': [{'name': 'Luigi', 'note': 'sabe sobre orçamento'},
                                 {'name': 'Luigi', 'note': 'esteve presente e falou do orçamento'}]})):
            gold = self.s.generate_gold(self.meta, 'nota', 'Áudio do sistema: Luigi sabe sobre orçamento.')
        self.assertEqual(gold['people_notes'], [{'name': 'Luigi', 'note': 'sabe sobre orçamento'}])


    def test_native_calendar_response_and_organizer_are_separate_from_attendance(self):
        self.meta['calendar_event']['attendees'][0] = {
            'name': 'Luigi Rossi', 'email': 'luigi@example.invalid', 'response': 'accepted'}
        self.meta['calendar_event']['organizer'] = 'owner@example.invalid'
        with patch.object(self.s, '_call_llm', return_value='owner@example.invalid aprovou o orçamento.'):
            silver = self.s.generate_silver(self.meta, self.transcript)
        self.assertIn('Resumo retido', silver)
        self.assertIn('rsvp: "accepted"', silver)
        self.assertIn('presence: "unverified"', silver)
        # No Gold, o organizador é sujeito válido de um fato citado; só presença barra.
        with patch.object(self.s, '_call_llm', return_value=json.dumps({
                'facts': [{'subject': 'owner@example.invalid', 'predicate': 'aprovou', 'object': 'orçamento'},
                          {'subject': 'owner@example.invalid', 'predicate': 'compareceu', 'object': 'reunião'}]})):
            gold = self.s.generate_gold(self.meta, 'nota', self.transcript)
        self.assertEqual(gold['facts'], [{'subject': 'owner@example.invalid', 'predicate': 'aprovou', 'object': 'orçamento'}])


def _proc(returncode, stdout=""):
    class P:
        pass
    proc = P()
    proc.returncode, proc.stdout, proc.stderr = returncode, stdout, ""
    return proc


class FakeVps:
    """Estado remoto do job Hermes, sem rede: UPLOAD → RUNNING → DONE ou FAILED."""

    def __init__(self, outcomes, polls=1):
        self.outcomes = outcomes  # "provider/model" -> ("done", saída) | ("fail", exit.txt + worker.log)
        self.polls = polls
        self.argv = []
        self.timeouts = []
        self.jobs = {}
        self.uploads = {}

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        self.argv.append(list(argv))
        self.timeouts.append((argv[0], timeout))
        if argv[0] == "scp":
            local, target = argv[-2], argv[-1]
            self.uploads[target.split(":", 1)[1]] = Path(local).read_text(encoding="utf-8")
            return _proc(0)
        command = argv[-1]
        remote = re.search(r"\.local/state/castanha/llm/[0-9a-f]{64}", command).group(0)
        job = self.jobs.setdefault(remote, {"stage": "UPLOAD", "polls": 0})
        if "echo UPLOAD" in command:
            if job["stage"] == "RUNNING":
                job["polls"] += 1
                if job["polls"] >= self.polls:
                    job["stage"] = "DONE" if job["outcome"][0] == "done" else "FAILED"
            return _proc(0, job["stage"])
        if command.startswith("chmod 600 ") and command.endswith("/prompt.txt"):
            job["prompt"] = self.uploads.pop(command.split()[2])
            return _proc(0)
        if "nohup flock" in command:
            m = re.search(r"--provider (\S+) -m (\S+) --reasoning (\S+)", command)
            job["outcome"] = self.outcomes[f"{m.group(1)}/{m.group(2)}"]
            job.update(stage="RUNNING", launch=command)
            return _proc(0)
        if command == f"cat {remote}/result.txt":
            return _proc(0, job["outcome"][1])
        if command.startswith(f"cat {remote}/exit.txt"):
            return _proc(0, job["outcome"][1])
        raise AssertionError(command)


MODELOS = [{"provider": "anthropic", "model": "claude-opus-5"},
           {"provider": "openai-codex", "model": "gpt-5.5"}]


class TestHermesSshLlm(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.temp), "XDG_STATE_HOME": str(self.temp)})
        self.env.start()
        self.sleeps = []
        self.now = 0.0

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def clock(self):
        self.now += 100.0
        return self.now

    def hermes(self, timeout_sec=900):
        return S.HermesSshLlm("vps-fixture", MODELOS, timeout_sec=timeout_sec,
                              sleep=self.sleeps.append, clock=self.clock)

    def test_upload_running_done_e_prompt_so_no_arquivo(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("done", "session_id: abc-123\n\n\n# Resumo\n\nDecidido.\n")}, polls=2)
        h = self.hermes()
        with patch("castanha.summarizer.subprocess.run", side_effect=vps):
            answer = h.complete("SISTEMA sigiloso", "Transcrição privada da reunião")
        self.assertEqual(answer, "# Resumo\n\nDecidido.")
        self.assertEqual(h.last_model, "anthropic/claude-opus-5")
        job = next(iter(vps.jobs.values()))
        self.assertIn("SISTEMA sigiloso\n\nTranscrição privada da reunião", job["prompt"])
        for argv in vps.argv:
            self.assertNotIn("sigiloso", " ".join(argv))
            self.assertNotIn("privada", " ".join(argv))
        self.assertIn("hermes chat -Q --oneshot --safe-mode -t none --source tool --query-file", job["launch"])
        self.assertIn("--provider anthropic -m claude-opus-5 --reasoning medium", job["launch"])
        self.assertIn("timeout --kill-after=30s 900s hermes", job["launch"])
        self.assertIn("exit.txt", job["launch"])
        for argv in vps.argv:
            self.assertEqual(argv[1:9], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                         "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1"])
        self.assertEqual({t for tool, t in vps.timeouts if tool == "ssh"}, {15})
        self.assertEqual({t for tool, t in vps.timeouts if tool == "scp"}, {30})
        self.assertEqual(self.sleeps, [5, 5])
        # Estados vistos: UPLOAD, RUNNING, RUNNING, DONE; nada é relançado.
        self.assertEqual(sum("nohup flock" in a[-1] for a in vps.argv), 1)
        self.assertEqual(list((self.temp / "castanha" / "llm").iterdir()), [], "arquivo local do prompt apagado")

    def test_falha_de_um_modelo_passa_ao_proximo(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("fail", "1\nError: /root/.hermes/auth.json expirado\n"),
                       "openai-codex/gpt-5.5": ("done", "session_id: x\n\nResposta da segunda")})
        h = self.hermes()
        err = io.StringIO()
        with patch("castanha.summarizer.subprocess.run", side_effect=vps), patch("castanha.summarizer.sys.stderr", err):
            self.assertEqual(h.complete("s", "u"), "Resposta da segunda")
        self.assertEqual(h.last_model, "openai-codex/gpt-5.5")
        self.assertEqual(len(vps.jobs), 2, "job novo por modelo")
        self.assertIn("tentando o próximo modelo", err.getvalue())
        self.assertNotIn("/root", err.getvalue())

    def test_cadeia_inteira_falha_vira_LlmUnavailable_sem_caminho(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("fail", "127\nsh: hermes: not found\n"),
                       "openai-codex/gpt-5.5": ("fail", "1\nTraceback /root/x.py: boom\n")})
        with patch("castanha.summarizer.subprocess.run", side_effect=vps), patch("castanha.summarizer.sys.stderr", io.StringIO()):
            with self.assertRaises(S.LlmUnavailable) as ctx:
                self.hermes().complete("s", "u")
        msg = str(ctx.exception)
        self.assertTrue(msg.startswith("Hermes indisponível na VPS: "), msg)
        self.assertIn("saída 127", msg)
        self.assertIn("hermes: not found", msg)
        self.assertNotIn("/root", msg)
        self.assertNotIsInstance(ctx.exception, S.HermesInProgress)

    def test_tempo_esgotado_com_job_rodando_fica_pendente_e_retoma_sem_reenviar(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("done", "session_id: y\n\nPronto depois")}, polls=50)
        h = self.hermes(timeout_sec=900)
        with patch("castanha.summarizer.subprocess.run", side_effect=vps):
            with self.assertRaises(S.HermesInProgress) as ctx:
                h.complete("s", "u")
        self.assertEqual(str(ctx.exception), "Resumo ainda em andamento na VPS; retomada automática")
        self.assertIsInstance(ctx.exception, S.LlmUnavailable)
        self.assertLess(len(self.sleeps), 12)
        # Retomada: mesmo prompt, mesmo job; o resultado já está lá.
        job = next(iter(vps.jobs.values()))
        job["stage"] = "DONE"
        scps = sum(a[0] == "scp" for a in vps.argv)
        with patch("castanha.summarizer.subprocess.run", side_effect=vps):
            self.assertEqual(self.hermes().complete("s", "u"), "Pronto depois")
        self.assertEqual(len(vps.jobs), 1)
        self.assertEqual(sum(a[0] == "scp" for a in vps.argv), scps)
        self.assertEqual(sum("nohup flock" in a[-1] for a in vps.argv), 1)

    def test_ssh_fora_vira_LlmUnavailable(self):
        for falha in (lambda *a, **k: _proc(255), OSError("sem ssh"),
                      __import__("subprocess").TimeoutExpired("ssh", 15)):
            with self.subTest(falha=falha), patch("castanha.summarizer.subprocess.run", side_effect=falha):
                with self.assertRaises(S.LlmUnavailable) as ctx:
                    self.hermes().complete("s", "u")
            self.assertEqual(str(ctx.exception), "SSH da VPS indisponível")

    def test_modo_json_pede_so_o_objeto(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("done", "session_id: z\n\n{\"facts\": []}")})
        with patch("castanha.summarizer.subprocess.run", side_effect=vps):
            self.assertEqual(self.hermes().complete("s", "u", json_mode=True), '{"facts": []}')
        job = next(iter(vps.jobs.values()))
        self.assertTrue(job["prompt"].rstrip().endswith(S.HERMES_JSON_INSTRUCTION))

    def test_sem_host_ou_sem_modelos_nao_chama_ssh(self):
        with patch("castanha.summarizer.subprocess.run") as run:
            with self.assertRaises(S.LlmUnavailable):
                S.HermesSshLlm("", MODELOS).complete("s", "u")
            with self.assertRaises(S.LlmUnavailable):
                S.HermesSshLlm("vps-fixture", []).complete("s", "u")
        run.assert_not_called()


class TestHermesNoSummarizer(unittest.TestCase):
    def summarizer(self, provider="hermes_ssh", fallback="groq", key="gsk_teste"):
        cfg = {"llm": {"provider": provider, "fallback_provider": fallback, "api_key": key,
                       "hermes_ssh_host": "vps-fixture", "hermes_models": MODELOS}}
        with patch("castanha.summarizer.load_config", return_value=cfg):
            return MeetingSummarizer()

    def _meta(self):
        return {"title": "Nora Weekly", "recorded_at": "2026-09-05T10:30:00", "duration_seconds": 10,
                "mode": "dual", "audio_status": "ok", "calendar_event": {"attendees": [{"name": "Luigi", "email": "l@x"}]}}

    def test_hermes_responde_e_registra_o_provedor(self):
        s = self.summarizer()
        with patch.object(s.hermes, "_run", return_value="ok") as run, \
             patch("castanha.summarizer.urllib.request.urlopen") as groq:
            self.assertEqual(s._call_llm("sys", "user"), "ok")
        groq.assert_not_called()
        self.assertEqual(run.call_args.args[:2], ("anthropic", "claude-opus-5"))
        self.assertEqual(s.last_provider, "hermes:anthropic/claude-opus-5")

    def test_reserva_groq_so_quando_configurada(self):
        err = io.StringIO()
        s = self.summarizer()
        with patch.object(s.hermes, "_run", side_effect=S.LlmUnavailable("SSH da VPS indisponível")), \
             patch("castanha.summarizer.urllib.request.urlopen", return_value=_resposta("da groq")), \
             patch("castanha.summarizer.sys.stderr", err):
            self.assertEqual(s._call_llm("sys", "user"), "da groq")
        self.assertEqual(s.last_provider, "groq")
        self.assertIn("[Castanha] Hermes indisponível (SSH da VPS indisponível); usando Groq como reserva", err.getvalue())
        for kwargs in ({"fallback": ""}, {"fallback": "groq", "key": ""}):
            with self.subTest(**kwargs):
                s = self.summarizer(**kwargs)
                with patch.object(s.hermes, "_run", side_effect=S.LlmUnavailable("SSH da VPS indisponível")), \
                     patch("castanha.summarizer.urllib.request.urlopen") as groq:
                    with self.assertRaises(S.LlmUnavailable):
                        s._call_llm("sys", "user")
                groq.assert_not_called()
                self.assertIsNone(s.last_provider)

    def test_resumo_em_andamento_nao_cai_na_groq(self):
        s = self.summarizer()
        with patch.object(s.hermes, "_run", side_effect=S.HermesInProgress("Resumo ainda em andamento na VPS; retomada automática")), \
             patch("castanha.summarizer.urllib.request.urlopen") as groq:
            with self.assertRaises(S.LlmUnavailable) as ctx:
                s._call_llm("sys", "user")
        groq.assert_not_called()
        self.assertIn("em andamento", str(ctx.exception))

    def test_silver_pelo_hermes_nao_pede_transcricao_estruturada_e_anexa_a_bruta(self):
        s = self.summarizer()
        chamadas = []

        def fake(system_prompt, user_prompt, json_mode=False):
            chamadas.append(system_prompt)
            return "# Nora Weekly\n\n## 📌 Resumo Executivo\nCurta.\n"

        with patch.object(s, "_call_llm", side_effect=fake):
            silver = s.generate_silver(self._meta(), "Fala um. Fala dois.")
        self.assertEqual(len(chamadas), 1)
        self.assertIs(chamadas[0], S.SILVER_NOTES_SYSTEM_PROMPT)
        self.assertNotIn("Transcrição Estruturada", chamadas[0])
        self.assertIn("Não inclua a transcrição", chamadas[0])
        self.assertIn(S.CHANNEL_GROUNDING, chamadas[0])
        self.assertIn("## 📌 Resumo Executivo\nCurta.\n\n## 📝 Transcrição Bruta\nFala um. Fala dois.\n", silver)
        self.assertEqual(silver.count("Transcrição Bruta"), 1)

    def test_silver_pela_groq_continua_igual(self):
        s = self.summarizer(provider="groq")
        chamadas = []
        with patch.object(s, "_call_llm", side_effect=lambda sp, up, json_mode=False: chamadas.append(sp) or "# Nora\n\nCurta."):
            silver = s.generate_silver(self._meta(), "Fala um.")
        self.assertIs(chamadas[0], S.SILVER_SYSTEM_PROMPT)
        self.assertIn("Transcrição Estruturada", chamadas[0])
        self.assertNotIn("Transcrição Bruta", silver)

    def test_barreira_de_identidade_olha_a_narrativa_e_nao_a_transcricao_anexada(self):
        s = self.summarizer()
        with patch.object(s, "_call_llm", return_value="# Nora\n\n## 📌 Resumo Executivo\nOrçamento aprovado."):
            silver = s.generate_silver(self._meta(), "Luigi participou e disse que sim.")
        self.assertNotIn("Resumo retido", silver)
        self.assertIn("Orçamento aprovado.", silver)
        with patch.object(s, "_call_llm", return_value="Luigi participou da reunião."):
            silver = s.generate_silver(self._meta(), "Fala neutra.")
        self.assertIn("Resumo retido", silver)


if __name__ == "__main__":
    unittest.main()
