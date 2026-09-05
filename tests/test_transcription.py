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
        # Arquivo de 5 segundos dividido com chunk_sec=2 deve gerar 3 chunks
        audio_file = self._create_sine_audio("long.ogg", duration_sec=5)
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


class TestVpsTimeout(unittest.TestCase):
    def test_timeout_acompanha_a_duracao_com_teto(self):
        from castanha.transcription import VPS_MAX_TIMEOUT_SEC, VPS_MIN_TIMEOUT_SEC, VpsSshTranscriber
        vistos = {}

        def fake_run(cmd, **kw):
            if cmd[0] == "scp":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            vistos["timeout"] = kw.get("timeout")
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with patch("castanha.transcription.subprocess.run", side_effect=fake_run), \
             patch("castanha.audio.probe_duration_seconds", return_value=7969.75), \
             patch.object(VpsSshTranscriber, "_cleanup_remote") as limpa:
            with self.assertRaises(RuntimeError) as ctx:
                VpsSshTranscriber("host-teste").transcribe(Path("/tmp/x.ogg"))
        # 2,5 x 7970 s passa de 3 h: fica no teto.
        self.assertEqual(vistos["timeout"], VPS_MAX_TIMEOUT_SEC)
        self.assertGreaterEqual(VPS_MAX_TIMEOUT_SEC, VPS_MIN_TIMEOUT_SEC)
        self.assertIn("abortada", str(ctx.exception))
        limpa.assert_called_once()

    def test_reuniao_curta_fica_no_minimo(self):
        from castanha.transcription import VPS_MIN_TIMEOUT_SEC, VpsSshTranscriber
        vistos = {}

        def fake_run(cmd, **kw):
            if cmd[0] == "scp":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            vistos["timeout"] = kw.get("timeout")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "ok", "segments": []}), "")

        with patch("castanha.transcription.subprocess.run", side_effect=fake_run), \
             patch("castanha.audio.probe_duration_seconds", return_value=300.0):
            res = VpsSshTranscriber("host-teste").transcribe(Path("/tmp/x.ogg"))
        self.assertEqual(vistos["timeout"], VPS_MIN_TIMEOUT_SEC)
        self.assertEqual(res.text, "ok")


if __name__ == "__main__":
    unittest.main()
