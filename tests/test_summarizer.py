"""Resumo de reunião longa: a Groq gratuita dá 8.000 tokens por minuto, e uma reunião de 2h não cabe."""

import fcntl
import io
import os
import shlex
import subprocess
import itertools
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


def _resposta(texto, finish_reason="stop"):
    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        _corpo = io.BytesIO(json.dumps({"choices": [{"message": {"content": texto}, "finish_reason": finish_reason}]}).encode("utf-8"))
        headers: dict = {}
        # read(amt) como no HTTPResponse real: a leitura limitada pede pedaços.
        def read(self, amt=None): return self._corpo.read(amt) if amt else self._corpo.read()
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
            with self.assertRaisesRegex(S.LlmUnavailable, "Resumo vazio"):
                self.s.generate_silver(self._meta(), "Fala. Fala.")


class TestCalendarGrounding(unittest.TestCase):
    def setUp(self):
        with patch('castanha.summarizer.load_config', return_value={'llm': {'api_key': ''}}):
            self.s = MeetingSummarizer()
        self.meta = {'title': 'Reunião sintética', 'calendar_event': {
            'attendees': [{'name': 'Luigi Rossi', 'email': 'luigi@example.invalid',
                           'responseStatus': 'accepted'}]}}
        self.transcript = 'Áudio do sistema: orçamento aprovado para outubro.'

    def test_silver_keeps_the_narrative_and_marks_presence_unverified(self):
        # 07/09: reter o Silver inteiro por citar convidado descartava todo resumo real.
        for claim in ('Luigi participou da reunião.', 'LUIGI ROSSI estava presente.',
                      'luigi@example.invalid confirmou o orçamento.', 'Rossi explicou a proposta.'):
            with self.subTest(claim=claim), patch.object(self.s, '_call_llm', return_value=claim):
                silver = self.s.generate_silver(self.meta, self.transcript)
            self.assertNotIn('Resumo retido', silver)
            self.assertIn(claim, silver)
            self.assertIn('evidence: "calendar_invitation"', silver)
            self.assertIn('presence: "unverified"', silver)
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
        # A citação pode falar em presença ("quem esteve presente planeja"); o item em si não.
        self.assertEqual(gold, {'facts': [fake['facts'][1]], 'decisions': [fake['decisions'][0]],
                                'action_items': fake['action_items'],
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

    def test_chunked_summary_final_output_is_kept(self):
        calls = [LlmTooLarge(100, 200, '413')]
        with patch.object(self.s, '_call_llm', side_effect=calls), \
             patch.object(self.s, '_silver_em_partes', return_value='Luigi participou da reunião.'):
            silver = self.s.generate_silver(self.meta, self.transcript)
        self.assertNotIn('Resumo retido', silver)
        self.assertIn('Luigi participou', silver)

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
        self.assertIn('aprovou o orçamento', silver)
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
        if command.startswith("python3 -c "):
            return _proc(0, '{"blocked": false}')
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
            if callable(job["outcome"]):
                job["outcome"] = job["outcome"](job["prompt"])
            job.update(stage="RUNNING", launch=command)
            return _proc(0)
        if command == f"cat {remote}/result.txt":
            return _proc(0, job["outcome"][1])
        if command.startswith(f"cat {remote}/exit.txt"):
            return _proc(0, job["outcome"][1])
        if command.startswith(f"rm -f {remote}/exit.txt"):
            job.update(stage="UPLOAD", polls=0, cleaned=job.get("cleaned", 0) + 1)
            return _proc(0)
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
        self.now += 5.0
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

    def test_retoma_prompt_orfao_sem_reenviar_nem_trocar_modelo(self):
        for reboot in (False, True):
            with self.subTest(reboot=reboot):
                vps = FakeVps({"anthropic/claude-opus-5": ("done", "Resumo retomado")})
                h = self.hermes()
                with patch("castanha.summarizer.subprocess.run", side_effect=vps), \
                     patch.object(h, "_launch", side_effect=S.LlmUnavailable("SSH caiu")):
                    with self.assertRaises(S.LlmUnavailable):
                        h.complete("s", "u")
                # Prompt persistido, nenhum worker. Após reboot é o mesmo estado.
                job = next(iter(vps.jobs.values()))
                job["stage"] = "RUNNING"
                if reboot:
                    job["result.part"] = "saída interrompida"
                scps = sum(a[0] == "scp" for a in vps.argv)
                probe = vps.__call__

                def sem_worker(argv, **kwargs):
                    if "echo UPLOAD" in argv[-1] and "launch" not in job:
                        return _proc(0, "RUNNING")
                    return probe(argv, **kwargs)

                with patch("castanha.summarizer.subprocess.run", side_effect=sem_worker):
                    self.assertEqual(self.hermes().complete("s", "u"), "Resumo retomado")
                self.assertEqual(len(vps.jobs), 1)
                self.assertEqual(sum(a[0] == "scp" for a in vps.argv), scps)
                self.assertEqual(sum("nohup flock" in a[-1] for a in vps.argv), 1)

    def test_launch_com_flock_real_preserva_worker_e_estados_terminais(self):
        remote = self.temp / "job"
        remote.mkdir()
        (remote / "prompt.txt").write_text("prompt de teste")
        hermes = self.temp / "hermes"
        hermes.write_text("#!/bin/sh\nprintf 'executou\n' >> calls.txt\nprintf 'Resumo pronto\n'\n")
        hermes.chmod(0o700)
        h = self.hermes()
        with patch.object(h, "_ssh") as ssh:
            h._launch(str(remote), "anthropic", "claude-opus-5")
        # Mesmo comando do worker, em primeiro plano para aguardar sua conclusão.
        tokens = shlex.split(ssh.call_args.args[0].split("nohup ", 1)[1])
        command = tokens[:tokens.index("sh") + 3]
        env = dict(os.environ, PATH=str(self.temp) + os.pathsep + os.environ["PATH"])

        def launch():
            return subprocess.run(command, cwd=self.temp, env=env, capture_output=True, timeout=5)

        with (remote / "job.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(launch().returncode, 1)
            self.assertFalse((self.temp / "calls.txt").exists())
        for terminal in ("result.txt", "exit.txt"):
            (remote / terminal).write_text("estado anterior")
            self.assertEqual(launch().returncode, 0)
            self.assertFalse((self.temp / "calls.txt").exists())
            self.assertEqual((remote / terminal).read_text(), "estado anterior")
            (remote / terminal).unlink()
        (remote / "result.part").write_text("saída incompleta antes do reboot")
        self.assertEqual(launch().returncode, 0)
        self.assertEqual((remote / "result.txt").read_text(), "Resumo pronto\n")
        self.assertEqual(launch().returncode, 0)
        self.assertEqual((self.temp / "calls.txt").read_text(), "executou\n")

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
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        env.start()
        self.addCleanup(env.stop)

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

    def test_narrativa_do_hermes_fica_inteira_com_a_transcricao_anexada(self):
        s = self.summarizer()
        with patch.object(s, "_call_llm", return_value="# Nora\n\n## 📌 Resumo Executivo\nLuigi defendeu o orçamento."):
            silver = s.generate_silver(self._meta(), "Luigi participou e disse que sim.")
        self.assertNotIn("Resumo retido", silver)
        self.assertIn("Luigi defendeu o orçamento.", silver)
        self.assertIn("## 📝 Transcrição Bruta\nLuigi participou e disse que sim.", silver)

    def test_session_id_e_removido_no_inicio_e_no_fim_da_resposta(self):
        self.assertEqual(S.HermesSshLlm._answer("session_id: a\n\n# Resumo\n\nDecidido.\n"), "# Resumo\n\nDecidido.")
        self.assertEqual(S.HermesSshLlm._answer("# Resumo\n\nDecidido.\n\n\nsession_id: 20260908_021756_3235f1\n"),
                         "# Resumo\n\nDecidido.")
        self.assertEqual(S.HermesSshLlm._answer('{"facts": []}\n\nsession_id: x\n'), '{"facts": []}')
        # 07/09: o aviso da CLI sobre "-t none" vazou para o Silver e para o Zinom.
        self.assertEqual(S.HermesSshLlm._answer("Warning: Unknown toolsets: none\n\n# Resumo\n\nsession_id: y\n"), "# Resumo")

    def test_job_que_falhou_e_relancado_na_proxima_tentativa(self):
        # Falha transitória (529, timeout) não pina o prompt na reserva para sempre.
        vps = FakeVps({"anthropic/claude-opus-5": ("fail", "124\nAPI call failed: HTTP 529 overloaded"),
                       "openai-codex/gpt-5.5": ("fail", "1\nHTTP 429: usage limit")})
        llm = S.HermesSshLlm("vps", [{"provider": "anthropic", "model": "claude-opus-5"},
                                     {"provider": "openai-codex", "model": "gpt-5.5"}],
                             sleep=lambda s: None, clock=itertools.count().__next__)
        with patch("castanha.summarizer.subprocess.run", vps):
            with self.assertRaises(S.LlmUnavailable):
                llm.complete("sistema", "pergunta")
            self.assertEqual([j.get("cleaned") for j in vps.jobs.values()], [1, 1])
            vps.outcomes["anthropic/claude-opus-5"] = ("done", "session_id: a\n\nAgora foi")
            self.assertEqual(llm.complete("sistema", "pergunta"), "Agora foi")

    def test_filtro_de_presenca_nao_olha_a_citacao(self):
        gold = S._ground_gold({"facts": [
            {"subject": "Nora", "predicate": "valuation", "object": "US$ 7 mi",
             "evidencia": "os participantes consideraram o valuation alto"},
            {"subject": "Luigi", "predicate": "participou de", "object": "reunião", "evidencia": "x"}],
            "decisions": [{"decision": "Centralizar leads", "evidencia": "Luigi disse que centraliza"}]})
        self.assertEqual([f["object"] for f in gold["facts"]], ["US$ 7 mi"])
        self.assertEqual(len(gold["decisions"]), 1)

    def test_gold_no_hermes_recebe_a_transcricao_inteira(self):
        s = self.summarizer()
        longa = "Fala. " * 2000
        prompts = []
        with patch.object(s, "_call_llm", side_effect=lambda sp, up, json_mode=False: prompts.append(up) or '{"facts": []}'):
            s.generate_gold(self._meta(), "# Nora\n\nnota", longa)
        self.assertIn(longa.strip(), prompts[0])


class TestSynthesisReliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "GROQ_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        with patch.object(S, "load_config", return_value={"llm": {
                "provider": "hermes_ssh", "hermes_ssh_host": "vps-fixture",
                "hermes_models": MODELOS, "fallback_provider": "groq", "api_key": "fixture"}}):
            self.s = MeetingSummarizer()
        self.s.hermes.sleep = lambda _: None
        self.s.hermes.clock = itertools.count().__next__
        self.meta = {"title": "Fixture", "recorded_at": "2026-09-10T10:00:00-03:00"}
        self.valid = '{"facts": [], "decisions": [], "action_items": [], "people_notes": []}'

    def test_invalid_gold_never_becomes_successful_empty_extraction(self):
        invalid = [None, "", "   ", "Tente novamente", "[]", "{}", '{"error": "failed"}',
                   '{"facts": null}', '{"facts": {}}', '{"decisions": "erro"}',
                   '{"facts": [7]}', '{"facts": [{}]}', '{"facts": [{"subject": []}]}',
                   '{"action_items": [{"task": "x", "assignee": false}]}']
        for reply in invalid:
            with self.subTest(reply=reply), patch.object(self.s, "_call_llm", return_value=reply) as call:
                with self.assertRaisesRegex(S.LlmInvalidResponse, "Extração de fatos inválida"):
                    self.s.generate_gold(self.meta, "Notas sintéticas", "Transcrição sintética")
                self.assertEqual(call.call_count, 2)

    def test_legitimate_empty_gold_is_successful_and_does_not_retry(self):
        with patch.object(self.s, "_call_llm", return_value=self.valid) as call:
            self.assertEqual(self.s.generate_gold(self.meta, "Notas", "Conversa informal"), json.loads(self.valid))
        self.assertEqual(call.call_count, 1)

    def test_oversize_gold_remains_pending(self):
        with patch.object(self.s, "_call_llm", side_effect=S.LlmTooLarge()):
            with self.assertRaisesRegex(S.LlmUnavailable, "limite"):
                self.s.generate_gold(self.meta, "Notas", "Transcrição")

    def test_empty_silver_requires_explicit_pending_with_configured_provider(self):
        for reply in ("", " \n", None, []):
            with self.subTest(reply=reply), patch.object(self.s, "_call_llm", return_value=reply):
                with self.assertRaisesRegex(S.LlmInvalidResponse, "Resumo vazio"):
                    self.s.generate_silver(self.meta, "Transcrição preservada")

    def test_template_still_allowed_without_provider_or_transcript(self):
        self.s.provider, self.s.api_key = "groq", ""
        with patch.object(self.s, "_call_llm", return_value=""):
            self.assertIn("Sem resumo", self.s.generate_silver(self.meta, "Transcrição"))
            self.assertEqual(self.s.generate_gold(self.meta, "Notas", "Transcrição"), json.loads(self.valid))
        self.s.provider = "hermes_ssh"
        with patch.object(self.s, "_call_llm") as call:
            self.assertIn("Sem resumo", self.s.generate_silver(self.meta, ""))
            self.assertEqual(self.s.generate_gold(self.meta, "Notas", ""), json.loads(self.valid))
            call.assert_not_called()

    def test_gold_request_identity_survives_pending_metadata_and_key_order(self):
        calls = []
        with patch.object(self.s, "_call_llm", side_effect=lambda sp, up, json_mode: calls.append(up) or self.valid):
            self.s.generate_gold(self.meta, "Notas", "Transcrição")
            pending = dict(reversed(list(self.meta.items())))
            pending.update(processing_status="pending", summary_status="pending", summary_error="em andamento",
                           summary_provider="hermes:fixture", zinom={"status": "pending"})
            self.s.generate_gold(pending, "Notas", "Transcrição")
        self.assertEqual(calls[0], calls[1])

    def test_cached_invalid_gold_repairs_once_and_reuses_both_jobs(self):
        original = "Falha na extração."
        vps = FakeVps({"anthropic/claude-opus-5": lambda prompt: (
            "done", self.valid if "[castanha-output-repair-v1]" in prompt else original)})
        with patch.object(S.subprocess, "run", vps), patch.object(self.s, "_call_groq") as groq:
            for _ in range(3):
                self.assertEqual(self.s.generate_gold(self.meta, "Notas", "Transcrição"), json.loads(self.valid))
            groq.assert_not_called()
        self.assertEqual(len(vps.jobs), 2)
        self.assertEqual([job["outcome"][1] for job in vps.jobs.values()], [original, self.valid])
        self.assertEqual(sum(argv[0] == "scp" for argv in vps.argv), 2)
        self.assertEqual(sum("nohup flock" in argv[-1] for argv in vps.argv), 2)
        self.assertFalse(any("rm -f" in argv[-1] for argv in vps.argv))

    def test_invalid_repair_is_bounded_across_retries_without_groq_or_model_switch(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("done", '{"facts": null}')})
        with patch.object(S.subprocess, "run", vps), patch.object(self.s, "_call_groq") as groq:
            for _ in range(3):
                with self.assertRaisesRegex(S.LlmInvalidResponse, "após reparo; revisão necessária"):
                    self.s.generate_gold(self.meta, "Notas", "Transcrição")
            groq.assert_not_called()
        self.assertEqual(len(vps.jobs), 2)
        self.assertEqual(sum(argv[0] == "scp" for argv in vps.argv), 2)
        self.assertFalse(any("openai-codex" in argv[-1] for argv in vps.argv))

    def test_session_only_cached_silver_repairs_without_losing_original(self):
        vps = FakeVps({"anthropic/claude-opus-5": lambda prompt: (
            "done", "# Notas sintéticas" if "[castanha-output-repair-v1]" in prompt else "session_id: fixture")})
        with patch.object(S.subprocess, "run", vps):
            for _ in range(2):
                silver = self.s.generate_silver(self.meta, "Transcrição preservada")
                self.assertIn("# Notas sintéticas", silver)
                self.assertIn("Transcrição preservada", silver)
                self.assertNotIn("Sem resumo", silver)
        self.assertEqual(len(vps.jobs), 2)
        self.assertEqual(next(iter(vps.jobs.values()))["outcome"][1], "session_id: fixture")

    def test_repair_running_yields_local_queue_without_restarting_worker(self):
        vps = FakeVps({"anthropic/claude-opus-5": ("done", "Invalid response")})
        with patch.object(S.subprocess, "run", vps):
            # Persist only the original failed extraction, then simulate a slow repair.
            original_run = self.s.hermes._run
            def run(provider, model, prompt):
                if "[castanha-output-repair-v1]" in prompt:
                    vps.polls = 1000
                    vps.outcomes["anthropic/claude-opus-5"] = ("done", self.valid)
                return original_run(provider, model, prompt)
            with patch.object(self.s.hermes, "_run", side_effect=run):
                with self.assertRaises(S.HermesInProgress):
                    self.s.generate_gold(self.meta, "Notas", "Transcrição")
            self.assertEqual(len(vps.jobs), 2)
            repair = list(vps.jobs.values())[1]
            self.assertIn("timeout --kill-after=30s 900s hermes", repair["launch"])
            self.assertLess(repair["polls"], 25)
            repair["stage"] = "DONE"
            self.assertEqual(self.s.generate_gold(self.meta, "Notas", "Transcrição"), json.loads(self.valid))
        self.assertEqual(sum("nohup flock" in argv[-1] for argv in vps.argv), 2)
        self.assertEqual(sum(argv[0] == "scp" for argv in vps.argv), 2)

    def _engine_failure_and_recovery(self, failed_stage):
        # Exercita captura/checkpoint/fila reais, só provedores são sintéticos.
        from tests import test_durable_jobs
        from castanha.sync import sync_meeting
        case = test_durable_jobs.TestDurableJobs()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.engine.summarizer.api_key = "fixture"
        original = case.source.read_bytes()
        def invalid(system_prompt, user_prompt, json_mode=False):
            if failed_stage == "silver":
                return ""
            return "Falha na extração" if system_prompt == S.GOLD_SYSTEM_PROMPT else "# Notas sintéticas"
        with patch("castanha.engine.get_transcriber") as provider, \
             patch.object(case.engine.summarizer, "_call_llm", side_effect=invalid), \
             patch.object(case.engine.zinom, "ingest_meeting") as ingest:
            provider.return_value.transcribe.return_value = case.transcription()
            result = case.engine.stop_recording()
            ingest.assert_not_called()
        self.assertEqual(result["status"], "partial")
        slug = result["result"]["slug"]
        bronze = case.engine.storage.bronze_dir / slug
        self.assertEqual(result["result"]["summary_status"], "pending")
        self.assertFalse((case.engine.storage.gold_dir / f"{slug}.json").exists())
        self.assertEqual((bronze / "transcript_raw.txt").read_text(), "Decisão preservada")
        self.assertEqual({json.loads(p.read_text())["stage"] for p in (bronze / ".jobs").glob("*.json")}, {"transcribed"})
        self.assertIn(original, [p.read_bytes() for p in bronze.glob("*.ogg")])
        with patch("castanha.engine.get_transcriber") as provider, \
             patch.object(MeetingSummarizer, "_call_llm", side_effect=lambda sp, up, json_mode=False:
                          self.valid if json_mode else "# Notas sintéticas"):
            sync_meeting(slug, case.engine.storage)
            provider.assert_not_called()
        meta = case.engine.storage._read_bronze_metadata(slug)
        self.assertEqual(meta["processing_status"], "complete")
        self.assertNotIn("summary_status", meta)
        self.assertEqual({json.loads(p.read_text())["stage"] for p in (bronze / ".jobs").glob("*.json")}, {"done"})
        self.assertEqual(json.loads((case.engine.storage.gold_dir / f"{slug}.json").read_text()), json.loads(self.valid))
        self.assertIn(original, [p.read_bytes() for p in bronze.glob("*.ogg")])

    def test_engine_invalid_gold_preserves_bronze_and_recovers_without_asr(self):
        self._engine_failure_and_recovery("gold")

    def test_engine_empty_silver_preserves_bronze_and_recovers_without_asr(self):
        self._engine_failure_and_recovery("silver")


class TestGroqDurableFallback(unittest.TestCase):
    def setUp(self):
        TestSynthesisReliability.setUp(self)

    def test_fallback_stays_selected_and_191k_transcript_never_reaches_groq_whole(self):
        transcript = ("Discussão sintética com a origem e o tempo preservados.\n" * 4000)
        self.assertGreater(len(transcript), 191000)
        requests = []
        def http(request, timeout=None):
            payload = json.loads(request.data)
            requests.append(payload)
            system = payload["messages"][0]["content"]
            return _resposta(self.valid if system == S.GOLD_SYSTEM_PROMPT else "# Notas\nDecisão sintética preservada.")
        with patch.object(self.s.hermes, "complete", side_effect=S.LlmUnavailable("Quota indisponível")) as hermes, \
             patch.object(S.urllib.request, "urlopen", side_effect=http):
            silver = self.s.generate_silver(self.meta, transcript)
            gold = self.s.generate_gold(self.meta, silver, transcript)
        self.assertEqual(hermes.call_count, 1)
        self.assertEqual(self.s.last_provider, "groq")
        self.assertEqual(self.s._provider_in_use(), "groq")
        self.assertIn(transcript, silver)
        self.assertEqual(gold, json.loads(self.valid))
        self.assertTrue(requests)
        self.assertTrue(all(p["max_completion_tokens"] == (4096 if p["messages"][0]["content"] == S.GOLD_SYSTEM_PROMPT else 2048)
                            for p in requests))
        self.assertTrue(all(sum(len(m["content"]) for m in p["messages"]) <= S.GROQ_INPUT_CHARS for p in requests))
        self.assertFalse(any(transcript in p["messages"][1]["content"] for p in requests))

    def test_gold_covers_all_notes_exactly_and_merges_without_rewriting_evidence(self):
        self.s._effective_provider = "groq"
        notes = "".join(f"Nota {i}: orçamento aprovado para o projeto sintético.  \n" for i in range(900))
        evidence = "orçamento aprovado para o projeto sintético."
        fact = {"subject": "Projeto sintético", "predicate": "tem", "object": "orçamento aprovado", "evidencia": evidence}
        parts = []
        def http(request, timeout=None):
            payload = json.loads(request.data)
            user = payload["messages"][1]["content"]
            self.assertNotIn("TRANSCRICAO-NAO-RETRANSMITIR", user)
            parts.append(user.split("\nNotas Silver:\n", 1)[1][:-1])
            self.assertLessEqual(sum(len(m["content"]) for m in payload["messages"]), S.GROQ_INPUT_CHARS)
            return _resposta(json.dumps({"facts": [fact]}))
        silver = notes + "\n## 📝 Transcrição Bruta\n" + "TRANSCRICAO-NAO-RETRANSMITIR" * 10000
        with patch.object(S.urllib.request, "urlopen", side_effect=http):
            result = self.s.generate_gold(self.meta, silver, "transcrição")
        self.assertGreater(len(parts), 2)
        self.assertEqual("".join(parts), notes)
        self.assertEqual(result["facts"], [fact])
        self.assertEqual(result["facts"][0]["evidencia"], evidence)

    def test_successful_parts_survive_daily_quota_and_new_instance(self):
        self.s._effective_provider = "groq"
        transcript = "".join(f"Item sintético {i}: decisão preservada no canal original.\n" for i in range(900))
        prompts = []
        def failing(request, timeout=None):
            prompt = json.loads(request.data)["messages"][1]["content"]
            prompts.append(prompt)
            if len(prompts) == 2:
                raise _http_error(429, "Rate limit reached on tokens per day (TPD)")
            return _resposta("# Parcial\nDecisão preservada.")
        with patch.object(S.urllib.request, "urlopen", side_effect=failing), patch.object(S.time, "sleep") as sleep:
            with self.assertRaisesRegex(S.LlmUnavailable, "Cota diária"):
                self.s.generate_silver(self.meta, transcript)
            sleep.assert_not_called()
        cache = Path(self.temp.name) / "castanha/llm/groq"
        self.assertEqual(len(list(cache.glob("*.json"))), 1)
        with patch.object(S, "load_config", return_value={"llm": {"provider": "groq", "api_key": "fixture"}}):
            resumed = MeetingSummarizer()
        def success(request, timeout=None):
            prompts.append(json.loads(request.data)["messages"][1]["content"])
            return _resposta("# Parcial\nDecisão preservada.")
        with patch.object(S.urllib.request, "urlopen", side_effect=success):
            silver = resumed.generate_silver(self.meta, transcript)
        self.assertEqual(prompts.count(prompts[0]), 1, "parte concluída deve vir do checkpoint")
        self.assertIn(transcript, silver)
        self.assertEqual(resumed.last_provider, "groq")
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        for path in cache.glob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('"fixture"', path.read_text())
            self.assertNotIn('"Authorization"', path.read_text())

    def test_cache_is_scoped_to_credential_without_storing_the_secret(self):
        self.s._effective_provider = "groq"
        self.s.api_key = "credential-account-one"
        with patch.object(S.urllib.request, "urlopen", side_effect=lambda *a, **k: _resposta("Resposta um")) as http:
            self.assertEqual(self.s._call_llm("sistema", "fonte"), "Resposta um")
            self.assertEqual(self.s._call_llm("sistema", "fonte"), "Resposta um")
            self.assertEqual(http.call_count, 1)
        self.s.api_key = "credential-account-two"
        with patch.object(S.urllib.request, "urlopen", side_effect=lambda *a, **k: _resposta("Resposta dois")) as http:
            self.assertEqual(self.s._call_llm("sistema", "fonte"), "Resposta dois")
            self.assertEqual(http.call_count, 1)
        files = list((Path(self.temp.name) / "castanha/llm/groq").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all("credential-account-" not in path.read_text() for path in files))

    def test_invalid_gold_is_not_cached_and_stays_pending(self):
        self.s._effective_provider = "groq"
        with patch.object(S.urllib.request, "urlopen", side_effect=lambda *a, **k: _resposta("Erro de extração")):
            with self.assertRaises(S.LlmInvalidResponse):
                self.s.generate_gold(self.meta, "Notas", "Transcrição")
        self.assertEqual(list((Path(self.temp.name) / "castanha/llm/groq").glob("*.json")), [])

    def test_corrupt_checkpoint_is_preserved_without_network(self):
        self.s._effective_provider = "groq"
        with patch.object(S.urllib.request, "urlopen", return_value=_resposta("Notas concluídas")):
            self.s._call_llm("sistema", "fonte")
        cache_file = next((Path(self.temp.name) / "castanha/llm/groq").glob("*.json"))
        cache_file.write_text("corrompido")
        with patch.object(S.urllib.request, "urlopen") as http:
            with self.assertRaisesRegex(S.LlmUnavailable, "Checkpoint"):
                self.s._call_llm("sistema", "fonte")
            http.assert_not_called()
        self.assertEqual(cache_file.read_text(), "corrompido")

    def test_preflight_rejects_oversize_without_http_or_checkpoint(self):
        with patch.object(S.urllib.request, "urlopen") as http:
            with self.assertRaises(S.LlmTooLarge):
                self.s._call_groq("sistema", "x" * (S.GROQ_INPUT_CHARS + 1))
            http.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "castanha/llm/groq").exists())

    def test_exact_split_preserves_whitespace_unicode_and_tail(self):
        text = ("  decisão\tcom ação.\n\nTexto sem alteração 🙂  " * 900) + "fim"
        parts = S._split_exact(text, 137)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 137 for part in parts))

    def test_length_finish_reason_never_caches_partial_silver_or_gold(self):
        for system, output in ((S.SILVER_SYSTEM_PROMPT, "Resumo incompleto"), (S.GOLD_SYSTEM_PROMPT, self.valid)):
            with self.subTest(system=system), patch.object(S.urllib.request, "urlopen",
                    return_value=_resposta(output, finish_reason="length")):
                with self.assertRaisesRegex(S.LlmTooLarge, "truncada"):
                    self.s._call_groq(system, "fonte")
        self.assertEqual(list((Path(self.temp.name) / "castanha/llm/groq").glob("*.json")), [])

    def test_gold_adapts_after_real_413_and_preserves_all_note_bytes(self):
        self.s._effective_provider = "groq"
        notes = "".join(f"Nota sintética {i}: decisão importante.\n" for i in range(180))
        accepted, attempts = [], []
        def http(request, timeout=None):
            payload = json.loads(request.data)
            part = payload["messages"][1]["content"].split("\nNotas Silver:\n", 1)[1][:-1]
            attempts.append(len(part))
            if len(part) > 2500:
                raise _http_error(413, "Limit 8000 Requested 9000")
            accepted.append(part)
            return _resposta(self.valid)
        with patch.object(S.urllib.request, "urlopen", side_effect=http):
            self.assertEqual(self.s.generate_gold(self.meta, notes, "Transcrição"), json.loads(self.valid))
        self.assertGreater(max(attempts), 2500)
        self.assertEqual("".join(accepted), notes)

    def test_permanent_gold_413_is_bounded_and_explicitly_pending(self):
        self.s._effective_provider = "groq"
        with patch.object(S.urllib.request, "urlopen", side_effect=_http_error(413)) as http:
            with self.assertRaisesRegex(S.LlmUnavailable, "síntese pendente"):
                self.s.generate_gold(self.meta, "Nota. " * 2000, "Transcrição")
        self.assertLessEqual(http.call_count, 4)

    def test_gold_oss_uses_low_reasoning_more_output_and_compact_context(self):
        self.s._effective_provider = "groq"
        self.meta.update(origin_id="fixture-origin-not-a-fact", processing_status="pending",
                         calendar_event={"attendees": [{"name": "Fixture guest"}]})
        requests = []
        def http(request, timeout=None):
            payload = json.loads(request.data)
            requests.append(payload)
            return _resposta(self.valid)
        notes = "".join(f"Nota {i}: contexto sintético preservado.\n" for i in range(400))
        with patch.object(S.urllib.request, "urlopen", side_effect=http):
            self.s.generate_gold(self.meta, notes, "Transcrição")
        self.assertGreater(len(requests), 1)
        for payload in requests:
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["max_completion_tokens"], 4096)
            self.assertLessEqual(sum(len(m["content"]) for m in payload["messages"]), 8000)
            prompt = payload["messages"][1]["content"]
            header = prompt.split("\nNotas Silver:\n", 1)[0].split("Metadados da Reunião:\n", 1)[1]
            self.assertEqual(json.loads(header), {"title": self.meta["title"], "recorded_at": self.meta["recorded_at"]})

    def test_silver_payload_reuses_pre_gold_budget_cache_identity(self):
        import hashlib
        from castanha.secure_io import write_private_json
        self.s._effective_provider = "groq"
        # Payload publicado antes desta correção, incluindo o limite original.
        payload = {"model": self.s.model, "messages": [
            {"role": "system", "content": S.PARTIAL_SYSTEM_PROMPT},
            {"role": "user", "content": "Parte sintética preservada"}],
            "temperature": 0.2, "max_completion_tokens": 2048}
        identity = hashlib.sha256(json.dumps({"version": 1, "provider": "groq",
            "url": "https://api.groq.com/openai/v1/chat/completions",
            "credential_scope": hashlib.sha256(self.s.api_key.encode()).hexdigest(), "request": payload},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = Path(self.temp.name) / "castanha/llm/groq" / f"{identity}.json"
        write_private_json(path, {"identity": identity, "content": "Resumo anterior válido"})
        before = path.read_bytes()
        with patch.object(S.urllib.request, "urlopen") as http:
            self.assertEqual(self.s._call_llm(S.PARTIAL_SYSTEM_PROMPT, "Parte sintética preservada"), "Resumo anterior válido")
            http.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_gold_budget_does_not_send_oss_parameter_to_other_models(self):
        self.s.model = "fixture/non-oss-model"
        with patch.object(S.urllib.request, "urlopen", return_value=_resposta(self.valid)) as http:
            self.s._call_groq(S.GOLD_SYSTEM_PROMPT, "Notas", json_mode=True)
        payload = json.loads(http.call_args.args[0].data)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_completion_tokens"], 2048)


if __name__ == "__main__":
    unittest.main()
