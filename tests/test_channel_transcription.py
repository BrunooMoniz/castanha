"""Fixtures sintéticas: nenhum microfone, serviço remoto ou reunião pessoal."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from castanha.channel_transcription import split_channels, transcribe_dual
from castanha.transcription import TranscriptionResult, Utterance


class ChannelTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "original.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "aevalsrc=0.1*sin(2*PI*440*t)|0.2*sin(2*PI*880*t):d=1:s=16000",
                        str(self.source)], check=True, capture_output=True)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def result(self, channel):
        start = 0.4 if channel == 0 else 0.1
        text = "fala local" if channel == 0 else "fala remota"
        return TranscriptionResult(text, [Utterance("Falante", text, start, 0.9)], "fixture", {})

    def run_transcription(self, callback):
        return transcribe_dual(self.source, self.root / "checkpoints", callback,
                               pipeline_id="fixture-v1")

    def test_real_split_preserves_exact_pcm_and_original(self):
        paths = split_channels(self.source, self.root / "derived")
        for channel, path in enumerate(paths):
            original_pcm = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(self.source), "-af",
                 f"pan=mono|c0=c{channel}", "-f", "s16le", "-"],
                capture_output=True, check=True).stdout
            derived_pcm = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-"],
                capture_output=True, check=True).stdout
            self.assertEqual(original_pcm, derived_pcm)
        self.assertEqual(self.digest, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_chronology_keeps_overlap_and_source_without_person_names(self):
        result = self.run_transcription(lambda p: self.result(int(p.stem[-1])))
        self.assertEqual([s.speaker for s in result.utterances],
                         ["Áudio do sistema", "Microfone local"])
        self.assertEqual([s.start for s in result.utterances], [0.1, 0.4])
        self.assertEqual([s.end for s in result.utterances], [0.9, 0.9])
        self.assertFalse(result.raw_response["identity_inferred"])
        self.assertIn("Áudio do sistema: fala remota", result.text)

    def test_retry_after_second_channel_failure_reuses_first_checkpoint(self):
        calls = []
        def first(path):
            channel = int(path.stem[-1])
            calls.append(channel)
            if channel == 1:
                raise RuntimeError("interrupção simulada")
            return self.result(channel)
        with self.assertRaises(RuntimeError):
            self.run_transcription(first)
        def retry(path):
            channel = int(path.stem[-1])
            calls.append(channel)
            return self.result(channel)
        self.run_transcription(retry)
        self.assertEqual(calls, [0, 1, 1])
        self.run_transcription(lambda _: self.fail("replay não deve chamar provedor"))

    def test_corrupt_checkpoint_is_preserved_without_provider_call(self):
        self.run_transcription(lambda p: self.result(int(p.stem[-1])))
        checkpoint = self.root / "checkpoints" / self.digest / "channel-0.json"
        checkpoint.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.run_transcription(lambda _: self.fail("não cobrar novamente"))
        self.assertEqual(checkpoint.read_text(), "{broken")

    def test_missing_or_invalid_timestamps_do_not_create_success_checkpoint(self):
        for segments in ([], [Utterance("Falante", "fala", float("nan"), 1)]):
            with self.assertRaises(ValueError):
                self.run_transcription(lambda _: TranscriptionResult("fala", segments, "fixture", {}))
        self.assertFalse(list((self.root / "checkpoints").rglob("*.json")))

    def test_mono_is_rejected_instead_of_claiming_two_origins(self):
        mono = self.root / "mono.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.source),
                        "-ac", "1", str(mono)], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            split_channels(mono, self.root / "derived")


if __name__ == "__main__":
    unittest.main()
