"""Testes para o módulo de transcrição e chunking de áudio longo."""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from castanha.transcription import (
    GroqTranscriber,
    Utterance,
    chunk_audio,
)

class TestTranscription(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        # O transcritor registra consumo no budget.json do diretório de estado:
        # sem isto a suíte escrevia no orçamento real do Bruno.
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp_dir / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sine_audio(self, filename: str, duration_sec: int) -> Path:
        p = self.temp_dir / filename
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration_sec}",
             "-c:a", "libopus", "-b:a", "64k", str(p)],
            capture_output=True, check=True
        )
        return p

    def test_chunk_audio_small_file_no_split(self):
        # Arquivo de 2 segundos não deve ser dividido
        audio_file = self._create_sine_audio("short.ogg", duration_sec=2)
        chunks = chunk_audio(audio_file, chunk_sec=600)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], audio_file)
        self.assertEqual(chunks[0][1], 0.0)

    def test_chunk_audio_long_file_splits_correctly(self):
        # 7 s com fatias de 2 s: 0-2, 2-4 e 4-7 (o resto de 1 s se funde à última)
        audio_file = self._create_sine_audio("long.ogg", duration_sec=7)
        chunks = chunk_audio(audio_file, chunk_sec=2)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0][1], 0.0)
        self.assertEqual(chunks[1][1], 2.0)
        self.assertEqual(chunks[2][1], 4.0)
        for cf, _ in chunks:
            self.assertTrue(cf.exists())
            self.assertGreater(cf.stat().st_size, 0)
        if chunks[0][0] != audio_file:
            shutil.rmtree(chunks[0][0].parent, ignore_errors=True)

    @patch.object(GroqTranscriber, "_request_groq")
    def test_groq_transcriber_aggregates_chunks_with_offsets(self, mock_request):
        audio_file = self._create_sine_audio("split_test.ogg", duration_sec=4)

        # Simula resposta para cada chunk de 2s
        def fake_groq(file_path):
            name = file_path.name
            if "000" in name:
                return {
                    "text": "Primeiro bloco de fala.",
                    "segments": [{"text": "Primeiro bloco de fala.", "start": 0.2, "end": 1.8}],
                    "duration": 2.0
                }
            elif "001" in name:
                return {
                    "text": "Segundo bloco de fala.",
                    "segments": [{"text": "Segundo bloco de fala.", "start": 0.1, "end": 1.9}],
                    "duration": 2.0
                }
            return {"text": "", "segments": [], "duration": 0.0}

        mock_request.side_effect = fake_groq

        transcriber = GroqTranscriber(api_key="gsk_test")
        with patch("castanha.transcription.DEFAULT_CHUNK_DURATION_SEC", 2), \
             patch("castanha.transcription.GROQ_MAX_FILE_BYTES", 100):  # Força chunking
            res = transcriber.transcribe(audio_file)

        self.assertEqual(res.provider, "groq")
        self.assertIn("Primeiro bloco de fala. Segundo bloco de fala.", res.text)
        self.assertEqual(len(res.utterances), 2)
        # O segundo utterance deve ter o offset somado (2.0 + 0.1 = 2.1)
        self.assertEqual(res.utterances[0].start, 0.2)
        self.assertEqual(res.utterances[0].end, 1.8)
        self.assertEqual(res.utterances[1].start, 2.1)
        self.assertEqual(res.utterances[1].end, 3.9)

    def test_arquivo_pequeno_vai_inteiro_sem_fatiar(self):
        """Abaixo do teto da Groq o áudio vai inteiro: cada emenda custa contexto."""
        audio_file = self._create_sine_audio("curto.ogg", duration_sec=3)
        with patch.object(GroqTranscriber, "_request_groq", return_value={"text": "oi", "segments": [], "duration": 3.0}) as req, \
             patch("castanha.transcription.chunk_audio") as fatiar, \
             patch("castanha.transcription.DEFAULT_CHUNK_DURATION_SEC", 1):
            res = GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        fatiar.assert_not_called()
        self.assertEqual(req.call_count, 1)
        self.assertEqual(res.text, "oi")

    def test_429_tenta_de_novo_e_honra_retry_after(self):
        import urllib.error
        from email.message import Message
        audio_file = self._create_sine_audio("limite.ogg", duration_sec=2)
        headers = Message()
        headers["Retry-After"] = "7"
        erro = urllib.error.HTTPError("https://api.groq.com", 429, "Too Many Requests", headers, io.BytesIO(b""))
        respostas = [erro, {"text": "depois do limite", "segments": [], "duration": 2.0}]

        def fake(_path):
            r = respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(GroqTranscriber, "_request_groq", side_effect=fake) as req, \
             patch("time.sleep") as dorme:
            res = GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertEqual(res.text, "depois do limite")
        self.assertEqual(req.call_count, 2)
        esperas = [c.args[0] for c in dorme.call_args_list]
        self.assertIn(7.0, esperas, "o Retry-After de 7 s tinha que ser respeitado")

    def test_erro_definitivo_sobe_sem_fallback_interno(self):
        """413 e afins não se repetem, e a Groq NÃO chama a VPS por conta própria."""
        import urllib.error
        audio_file = self._create_sine_audio("recusado.ogg", duration_sec=2)
        erro = urllib.error.HTTPError("https://api.groq.com", 413, "Payload Too Large", None, io.BytesIO(b"too large"))
        with patch.object(GroqTranscriber, "_request_groq", side_effect=erro) as req, \
             patch("castanha.transcription.VpsSshTranscriber") as vps, \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertIn("413", str(ctx.exception))
        self.assertEqual(req.call_count, 1)
        vps.assert_not_called()

    def test_rede_caida_desiste_depois_de_tres_tentativas(self):
        import urllib.error
        audio_file = self._create_sine_audio("offline.ogg", duration_sec=2)
        with patch.object(GroqTranscriber, "_request_groq", side_effect=urllib.error.URLError("sem rede")) as req, \
             patch("castanha.transcription.VpsSshTranscriber") as vps, \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertEqual(req.call_count, 3)
        vps.assert_not_called()

    def test_fatias_temporarias_somem_mesmo_quando_falha(self):
        import urllib.error
        audio_file = self._create_sine_audio("fatiado.ogg", duration_sec=4)
        criadas = []
        real_chunk = chunk_audio

        def espiao(path, chunk_sec):
            out = real_chunk(path, chunk_sec=chunk_sec)
            criadas.extend(c for c, _ in out)
            return out

        with patch("castanha.transcription.chunk_audio", side_effect=espiao), \
             patch("castanha.transcription.GROQ_MAX_FILE_BYTES", 100), \
             patch("castanha.transcription.DEFAULT_CHUNK_DURATION_SEC", 2), \
             patch.object(GroqTranscriber, "_request_groq", side_effect=urllib.error.HTTPError("u", 400, "Bad", None, io.BytesIO(b""))):
            with self.assertRaises(RuntimeError):
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertGreater(len(criadas), 1)
        for c in criadas:
            self.assertFalse(c.exists(), f"fatia temporária ficou para trás: {c}")


    def test_resto_pequeno_nao_vira_fatia_sozinha(self):
        """12 s em fatias de 10: o resto de 2 s iria sozinho e a Groq recusa fatia vazia."""
        audio_file = self._create_sine_audio("resto.ogg", duration_sec=12)
        chunks = chunk_audio(audio_file, chunk_sec=10)
        self.assertEqual(len(chunks), 1)
        from castanha.audio import probe_duration_seconds
        self.assertAlmostEqual(probe_duration_seconds(chunks[0][0]) or 0, 12.0, delta=0.5)
        shutil.rmtree(chunks[0][0].parent, ignore_errors=True)

    def test_diretorio_temporario_some_se_o_ffmpeg_falhar(self):
        import tempfile as _tf
        audio_file = self._create_sine_audio("quebra.ogg", duration_sec=12)
        criados = []
        real_mkdtemp = _tf.mkdtemp

        def espiao(**kw):
            d = real_mkdtemp(**kw)
            criados.append(Path(d))
            return d

        real_run = subprocess.run

        def so_ffmpeg_quebra(cmd, **kw):
            if cmd and cmd[0] == "ffmpeg":
                raise subprocess.CalledProcessError(1, "ffmpeg")
            return real_run(cmd, **kw)

        with patch("tempfile.mkdtemp", side_effect=espiao), \
             patch("castanha.transcription.GROQ_MAX_FILE_BYTES", 100), \
             patch("castanha.transcription.subprocess.run", side_effect=so_ffmpeg_quebra):
            with self.assertRaises(Exception):
                chunk_audio(audio_file, chunk_sec=2)
        self.assertEqual(len(criados), 1)
        self.assertFalse(criados[0].exists(), "o mkdtemp ficou para trás")

    def test_espera_somada_tem_teto(self):
        """Retry-After de horas não pode prender o stop: acima do teto somado, desiste."""
        import urllib.error
        from email.message import Message
        audio_file = self._create_sine_audio("lento.ogg", duration_sec=2)
        h = Message(); h["Retry-After"] = "3600"
        erro = urllib.error.HTTPError("u", 429, "Too Many", h, io.BytesIO(b""))
        with patch.object(GroqTranscriber, "_request_groq", side_effect=erro) as req, \
             patch("time.sleep") as dorme:
            with self.assertRaises(RuntimeError) as ctx:
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertIn("esperar", str(ctx.exception))
        self.assertEqual(req.call_count, 1, "pedido de uma hora: não dorme 15 min à toa")
        self.assertEqual(dorme.call_count, 0)

    def test_varios_429_somados_tem_teto(self):
        import urllib.error
        from email.message import Message
        from castanha.transcription import GROQ_MAX_TOTAL_WAIT_SEC
        audio_file = self._create_sine_audio("lento2.ogg", duration_sec=2)
        h = Message(); h["Retry-After"] = "700"
        erro = urllib.error.HTTPError("u", 429, "Too Many", h, io.BytesIO(b""))
        with patch.object(GroqTranscriber, "_request_groq", side_effect=erro), \
             patch("time.sleep") as dorme:
            with self.assertRaises(RuntimeError):
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        self.assertLessEqual(sum(c.args[0] for c in dorme.call_args_list), GROQ_MAX_TOTAL_WAIT_SEC)

    def test_orcamento_conta_as_fatias_boas_mesmo_se_uma_falhar(self):
        import urllib.error
        audio_file = self._create_sine_audio("meio.ogg", duration_sec=4)
        respostas = [{"text": "a", "segments": [], "duration": 2.0},
                     urllib.error.HTTPError("u", 400, "Bad", None, io.BytesIO(b""))]

        def fake(_p):
            r = respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(GroqTranscriber, "_request_groq", side_effect=fake), \
             patch("castanha.transcription.GROQ_MAX_FILE_BYTES", 100), \
             patch("castanha.transcription.DEFAULT_CHUNK_DURATION_SEC", 2), \
             patch("castanha.budget.BudgetManager.record_usage") as registra:
            with self.assertRaises(RuntimeError):
                GroqTranscriber(api_key="gsk_test").transcribe(audio_file)
        registra.assert_called_once_with(2.0)


class TestEscolhaDoTranscritor(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        cfg_dir = self.temp / "config" / "castanha"
        cfg_dir.mkdir(parents=True)
        self.cfg_file = cfg_dir / "config.json"
        self.env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.temp / "config"),
                                             "XDG_STATE_HOME": str(self.temp / "state"),
                                             "GROQ_API_KEY": "", "CASTANHA_MOCK_TRANSCRIBER": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _cfg(self, **transcription):
        self.cfg_file.write_text(json.dumps({"transcription": transcription}), encoding="utf-8")

    def test_sem_groq_e_sem_vps_e_falha_declarada_nao_mock(self):
        """Offline, o mock inventava uma reunião e mandava fato falso para o Zinom."""
        from castanha.transcription import get_transcriber
        self._cfg(groq_api_key="", vps_ssh_host="host-inexistente")
        audio = self.temp / "fixture.ogg"
        audio.write_bytes(b"synthetic audio")
        with patch("castanha.transcription.subprocess.run", return_value=subprocess.CompletedProcess([], 255)):
            with self.assertRaises(RuntimeError) as ctx:
                get_transcriber(60).transcribe(audio)
        self.assertIn("SSH falhou", str(ctx.exception))
        self.assertEqual(audio.read_bytes(), b"synthetic audio")

    def test_mock_so_quando_pedido(self):
        from castanha.transcription import MockTranscriber, get_transcriber
        self._cfg(provider="mock", groq_api_key="")
        self.assertIsInstance(get_transcriber(60), MockTranscriber)


class TestVpsTimeout(unittest.TestCase):
    """O teto do worker continua; a conexão não precisa esperar esse teto."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from tests.test_vps_channel_transport import LocalVps, TEST_CONTRACT
        self.audio = Path(self.temp.name) / "fixture.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-ar", "16000", "-ac", "1", str(self.audio)],
                       check=True, capture_output=True)
        self.original = self.audio.read_bytes()
        self.contract = TEST_CONTRACT
        self.remote = LocalVps(Path(self.temp.name) / "remote")

    def test_scp_travado_tem_teto_e_preserva_original(self):
        from castanha.transcription import VpsSshTranscriber
        self.remote.fail_transport = "scp"
        with patch("castanha.transcription.subprocess.run", side_effect=self.remote):
            with self.assertRaisesRegex(RuntimeError, "SCP indisponível"):
                VpsSshTranscriber("host-teste", self.contract).transcribe(self.audio, "mic_only")
        commands = [cmd for cmd, _ in self.remote.calls]
        self.assertEqual([kw["timeout"] for _, kw in self.remote.calls], [15, 15, 30])
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertFalse(any("pkill" in " ".join(cmd) or "rm -f" in " ".join(cmd) for cmd in commands))

    def test_erro_remoto_preserva_job_para_nova_tentativa(self):
        from castanha.transcription import VpsSshTranscriber
        commands = []

        def fake_run(cmd, **kw):
            if cmd[0] not in ("ssh", "scp"):
                return self.remote(cmd, **kw)
            commands.append(cmd)
            self.assertEqual(kw["timeout"], 30 if cmd[0] == "scp" else 15)
            if "nohup" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 1, "", "worker unavailable")
            return self.remote(cmd, **kw)

        with patch("castanha.transcription.subprocess.run", side_effect=fake_run), \
             patch("castanha.audio.probe_duration_seconds", return_value=300):
            with self.assertRaisesRegex(RuntimeError, "SSH falhou"):
                VpsSshTranscriber("host-teste", self.contract).transcribe(self.audio, "mic_only")
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(next(self.remote.root.rglob("audio.flac")).read_bytes(), self.original)
        self.assertFalse(any("pkill" in " ".join(cmd) or "rm -f" in " ".join(cmd) for cmd in commands))

    def test_timeout_acompanha_a_duracao_com_teto(self):
        from castanha.transcription import VPS_MAX_TIMEOUT_SEC, VpsSshTranscriber
        with patch("castanha.transcription.subprocess.run", side_effect=self.remote), \
             patch("castanha.audio.probe_duration_seconds", return_value=7969.75):
            with self.assertRaisesRegex(RuntimeError, "em andamento"):
                VpsSshTranscriber("host-teste", self.contract).transcribe(self.audio, "mic_only")
        self.remote.completed()
        commands = [cmd for cmd, _ in self.remote.calls]
        self.assertTrue(all(kw["timeout"] == (30 if cmd[0] == "scp" else 15)
                            for cmd, kw in self.remote.calls))
        launched = next(cmd[-1] for cmd in commands if "nohup" in cmd[-1])
        self.assertIn(f"timeout --kill-after=30s {VPS_MAX_TIMEOUT_SEC}s", launched)
        self.assertIn("flock -n", launched)

    def test_reuniao_curta_fica_no_minimo_e_resultado_e_reutilizado(self):
        from castanha.transcription import VPS_MIN_TIMEOUT_SEC, VpsSshTranscriber
        self.remote.set_reply({"text": "ok", "segments": [{"text": "ok", "start": 0, "end": 1}]})
        with patch("castanha.transcription.subprocess.run", side_effect=self.remote), \
             patch("castanha.audio.probe_duration_seconds", return_value=300):
            with self.assertRaisesRegex(RuntimeError, "em andamento"):
                VpsSshTranscriber("host-teste", self.contract).transcribe(self.audio, "mic_only")
            self.remote.completed()
            res = VpsSshTranscriber("host-teste", self.contract).transcribe(self.audio, "mic_only")
        commands = [cmd for cmd, _ in self.remote.calls]
        self.assertTrue(all(kw["timeout"] == (30 if cmd[0] == "scp" else 15)
                            for cmd, kw in self.remote.calls))
        launched = [cmd[-1] for cmd in commands if "nohup" in cmd[-1]]
        self.assertEqual(len(launched), 1)
        self.assertIn(f"timeout --kill-after=30s {VPS_MIN_TIMEOUT_SEC}s", launched[0])
        self.assertEqual(res.text, "ok")
        self.assertEqual((self.remote.root / "count").read_text().splitlines(), ["started"])
        self.assertEqual(self.audio.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()


class TestMimeDoArquivo(unittest.TestCase):
    """O tipo declarado tem que casar com o arquivo: canal separado vai em FLAC."""

    def test_mime_segue_a_extensao_e_mantem_ogg_como_padrao(self):
        from castanha.transcription import audio_mime
        self.assertEqual(audio_mime(Path("channel-0.flac")), "audio/flac")
        self.assertEqual(audio_mime(Path("capture.ogg")), "audio/ogg")
        self.assertEqual(audio_mime(Path("captura.desconhecido")), "audio/ogg")

    def test_canal_flac_nao_sobe_anunciado_como_ogg(self):
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp, True)
        caminho = temp / "channel-0.flac"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=1000:duration=1", "-c:a", "flac", str(caminho)],
                       check=True, capture_output=True)
        capturado = {}

        class Resposta:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            headers: dict = {}

            def __init__(self):
                self._corpo = io.BytesIO(
                    json.dumps({"text": "ok", "segments": [], "duration": 1}).encode("utf-8"))

            # read(amt) como no HTTPResponse real.
            def read(self, amt=None):
                return self._corpo.read(amt) if amt else self._corpo.read()

        def falso_urlopen(req, timeout=None):
            capturado["body"] = req.data
            return Resposta()

        with patch.dict("os.environ", {"XDG_STATE_HOME": str(temp / "state"),
                                       "XDG_CONFIG_HOME": str(temp / "config")}), \
             patch("castanha.transcription.urllib.request.urlopen", falso_urlopen):
            GroqTranscriber("chave-de-teste").transcribe(caminho)
        self.assertIn(b"Content-Type: audio/flac", capturado["body"])
        self.assertNotIn(b"Content-Type: audio/ogg", capturado["body"])
