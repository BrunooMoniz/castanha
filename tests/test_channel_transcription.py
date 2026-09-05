"""Fixtures sintéticas: nenhum microfone, serviço remoto ou reunião pessoal."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.channel_transcription import (
    LABELS,
    MONO_LABEL,
    MONO_ORIGIN,
    pcm_sha256,
    prepare_channels,
    transcribe_dual,
)
from castanha.transcription import TranscriptionResult, Utterance


class OrcamentoFalso:
    """Responde como o BudgetManager, contando quantas vezes foi consultado."""

    def __init__(self, respostas=None):
        self.respostas = list(respostas or [])
        self.consultas = []

    def can_use_groq(self, estimated_duration_sec=60.0):
        self.consultas.append(estimated_duration_sec)
        return self.respostas.pop(0) if self.respostas else True


class ChannelTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.gerar("original.wav", "0.1*sin(2*PI*440*t)|0.2*sin(2*PI*880*t)")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def gerar(self, nome, expressao):
        caminho = self.root / nome
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"aevalsrc={expressao}:d=1:s=16000", str(caminho)],
                       check=True, capture_output=True)
        return caminho

    def result(self, channel):
        start = 0.4 if channel == 0 else 0.1
        text = "fala local" if channel == 0 else "fala remota"
        return TranscriptionResult(text, [Utterance("Falante", text, start, 0.9)], "fixture", {})

    def varias(self, channel):
        """Três falas por canal, intercaladas, com o último par simultâneo."""
        origem = "local" if channel == 0 else "remota"
        base = 0.0 if channel == 0 else 0.2
        segmentos = [(f"{origem} A", base, base + 0.5), (f"{origem} B", base + 1.0, base + 1.5),
                     (f"{origem} C", 2.0, 2.5)]
        return TranscriptionResult(
            " ".join(t for t, _, _ in segmentos),
            [Utterance("Falante", t, s, e) for t, s, e in segmentos], "fixture", {})

    def run_transcription(self, callback, source=None, budget=None):
        return transcribe_dual(source or self.source, self.root / "checkpoints", callback,
                               pipeline_id="fixture-v1", budget=budget)

    def por_canal(self, funcao):
        """Adapta um callback (path, duração) que só olha o número do canal."""
        return lambda path, duration: funcao(int(path.stem[-1]))

    def checkpoints(self):
        return sorted(p.name for p in (self.root / "checkpoints").rglob("*.json"))

    def test_real_split_preserves_exact_pcm_and_original(self):
        canais = prepare_channels(self.source, self.root / "derived")
        for entrada in canais:
            original_pcm = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(self.source), "-af",
                 f"pan=mono|c0=c{entrada['channel']}", "-f", "s16le", "-"],
                capture_output=True, check=True).stdout
            derived_pcm = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(entrada["path"]), "-f", "s16le", "-"],
                capture_output=True, check=True).stdout
            self.assertEqual(original_pcm, derived_pcm)
        self.assertEqual(self.digest, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_chronology_keeps_overlap_and_source_without_person_names(self):
        result = self.run_transcription(self.por_canal(self.result))
        self.assertEqual([s.speaker for s in result.utterances],
                         ["Áudio do sistema", "Microfone local"])
        self.assertEqual([s.start for s in result.utterances], [0.1, 0.4])
        self.assertEqual([s.end for s in result.utterances], [0.9, 0.9])
        self.assertFalse(result.raw_response["identity_inferred"])
        self.assertFalse(result.raw_response["mono_fallback"])
        self.assertIn("Áudio do sistema: fala remota", result.text)

    def test_interleaved_order_keeps_every_turn_and_simultaneous_speech(self):
        result = self.run_transcription(self.por_canal(self.varias))
        self.assertEqual([s.text for s in result.utterances],
                         ["local A", "remota A", "local B", "remota B", "local C", "remota C"])
        # Fala simultânea: mesmos tempos nos dois canais, nenhuma descartada.
        simultaneas = [s for s in result.utterances if (s.start, s.end) == (2.0, 2.5)]
        self.assertEqual([s.channel for s in simultaneas], [0, 1])
        self.assertEqual([s.origin for s in simultaneas], ["microfone_local", "audio_sistema"])
        self.assertEqual(result.raw_response["utterance_count"], 6)

    def test_full_content_reaches_the_merged_text_and_hashes(self):
        result = self.run_transcription(self.por_canal(self.varias))
        self.assertEqual(len(result.text.splitlines()), len(result.utterances))
        for segmento in result.utterances:
            self.assertIn(segmento.text, result.text)
            self.assertEqual(segmento.source_sha256, self.digest)
            self.assertTrue(segmento.channel_sha256)
        canais = result.raw_response["channels"]
        self.assertEqual([c["utterance_count"] for c in canais], [3, 3])
        self.assertEqual({c["channel_sha256"] for c in canais},
                         {s.channel_sha256 for s in result.utterances})

    def test_identity_follows_the_decoded_audio_not_the_container_bytes(self):
        canais = prepare_channels(self.source, self.root / "derived")
        derivado = canais[0]["path"]
        recomprimido = self.root / "recomprimido.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(derivado), "-c:a", "flac",
                        "-compression_level", "12", str(recomprimido)],
                       check=True, capture_output=True)
        # Outro FFmpeg gera outros bytes de contêiner para o mesmo áudio.
        self.assertNotEqual(derivado.read_bytes(), recomprimido.read_bytes())
        self.assertEqual(pcm_sha256(derivado), pcm_sha256(recomprimido))
        self.run_transcription(self.por_canal(self.result))
        identidade = json.loads(
            (self.root / "checkpoints" / self.digest / "channel-0.json").read_text())["identity"]
        canal = (self.root / "checkpoints" / self.digest / "channel-0.flac")
        self.assertEqual(identidade["pcm_sha256"], pcm_sha256(canal))
        # O hash dos bytes do FLAC não entra na identidade: ele muda sem o áudio mudar.
        self.assertNotIn(hashlib.sha256(canal.read_bytes()).hexdigest(), identidade.values())

    def test_retry_after_second_channel_failure_reuses_first_checkpoint(self):
        calls = []
        def first(path, duration):
            channel = int(path.stem[-1])
            calls.append(channel)
            if channel == 1:
                raise RuntimeError("interrupção simulada")
            return self.result(channel)
        with self.assertRaises(RuntimeError):
            self.run_transcription(first)
        def retry(path, duration):
            channel = int(path.stem[-1])
            calls.append(channel)
            return self.result(channel)
        self.run_transcription(retry)
        self.assertEqual(calls, [0, 1, 1])
        self.run_transcription(lambda *_: self.fail("replay não deve chamar provedor"))

    def test_replay_repeats_the_same_content_without_calling_the_provider(self):
        primeiro = self.run_transcription(self.por_canal(self.varias))
        antes = {p.name: p.read_bytes() for p in (self.root / "checkpoints").rglob("*.json")}
        segundo = self.run_transcription(lambda *_: self.fail("replay não deve chamar provedor"))
        self.assertEqual(primeiro.text, segundo.text)
        self.assertEqual(primeiro.utterances, segundo.utterances)
        self.assertEqual(primeiro.raw_response, segundo.raw_response)
        self.assertEqual(antes, {p.name: p.read_bytes()
                                 for p in (self.root / "checkpoints").rglob("*.json")})

    def test_corrupt_checkpoint_is_preserved_without_provider_call(self):
        self.run_transcription(self.por_canal(self.result))
        checkpoint = self.root / "checkpoints" / self.digest / "channel-0.json"
        checkpoint.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.run_transcription(lambda *_: self.fail("não cobrar novamente"))
        self.assertEqual(checkpoint.read_text(), "{broken")

    def test_missing_or_invalid_timestamps_do_not_create_success_checkpoint(self):
        for segments in ([], [Utterance("Falante", "fala", float("nan"), 1)]):
            with self.assertRaises(ValueError):
                self.run_transcription(
                    lambda *_: TranscriptionResult("fala", segments, "fixture", {}))
        self.assertFalse(self.checkpoints())

    def test_mono_recording_falls_back_to_one_generic_origin(self):
        mono = self.root / "mono.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.source),
                        "-ac", "1", str(mono)], check=True, capture_output=True)
        result = self.run_transcription(self.por_canal(self.result), source=mono)
        self.assertTrue(result.raw_response["mono_fallback"])
        self.assertEqual([s.speaker for s in result.utterances], [MONO_LABEL])
        self.assertEqual([s.origin for s in result.utterances], [MONO_ORIGIN])
        # Uma origem só: nada de alegar microfone e sistema onde não há separação.
        for rotulo in LABELS:
            self.assertNotIn(rotulo, result.text)
        self.assertEqual(len(result.raw_response["channels"]), 1)

    def test_more_than_two_channels_is_refused(self):
        surround = self.root / "surround.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.source),
                        "-ac", "6", str(surround)], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            prepare_channels(surround, self.root / "derived")

    def test_silent_channel_is_registered_without_provider_and_without_invented_speech(self):
        mudo = self.gerar("mic-mudo.wav", "0|0.3*sin(2*PI*880*t)")
        chamados = []
        def callback(path, duration):
            chamados.append(int(path.stem[-1]))
            return self.result(1)
        result = self.run_transcription(callback, source=mudo)
        self.assertEqual(chamados, [1])
        self.assertEqual([s.origin for s in result.utterances], ["audio_sistema"])
        self.assertNotIn("Microfone local", result.text)
        canais = result.raw_response["channels"]
        self.assertEqual([c["silent"] for c in canais], [True, False])
        self.assertEqual([c["utterance_count"] for c in canais], [0, 1])
        self.assertEqual(canais[0]["provider"], "nenhum (canal em silêncio)")
        # Provedor real do canal com áudio, sem virar "mixed" por causa do silêncio.
        self.assertEqual(result.provider, "fixture")

    def test_channel_with_audio_and_empty_answer_fails_instead_of_losing_speech(self):
        with self.assertRaises(ValueError):
            self.run_transcription(
                lambda *_: TranscriptionResult("   ", [Utterance("Falante", "x", 0, 1)],
                                               "fixture", {}))
        self.assertFalse(self.checkpoints())

    def test_two_silent_channels_refuse_instead_of_reporting_empty_success(self):
        surdo = self.gerar("sem-audio.wav", "0|0")
        with self.assertRaises(ValueError):
            self.run_transcription(lambda *_: self.fail("silêncio não vai ao provedor"),
                                   source=surdo)
        # A recusa é estável: repetir não chama provedor nem inventa transcrição.
        with self.assertRaises(ValueError):
            self.run_transcription(lambda *_: self.fail("silêncio não vai ao provedor"),
                                   source=surdo)

    def test_mock_channel_never_completes_and_preserves_the_real_checkpoint(self):
        with self.assertRaises(ValueError):
            self.run_transcription(
                lambda *_: TranscriptionResult("simulado", [Utterance("Falante", "simulado", 0, 1)],
                                               "mock", {}))
        self.assertFalse(self.checkpoints())
        def real_e_mock(path, duration):
            if int(path.stem[-1]) == 1:
                return TranscriptionResult("simulado", [Utterance("Falante", "simulado", 0, 1)],
                                           "mock", {})
            return self.result(0)
        with self.assertRaises(ValueError):
            self.run_transcription(real_e_mock)
        self.assertEqual(self.checkpoints(), ["channel-0.json"])
        chamados = []
        def ambos_reais(path, duration):
            chamados.append(int(path.stem[-1]))
            return self.result(int(path.stem[-1]))
        result = self.run_transcription(ambos_reais)
        self.assertEqual(chamados, [1])
        self.assertEqual(len(result.utterances), 2)

    def test_speaker_name_from_the_provider_is_replaced_by_the_channel_origin(self):
        def com_nome(path, duration):
            channel = int(path.stem[-1])
            texto = "fala local" if channel == 0 else "fala remota"
            return TranscriptionResult(texto, [Utterance("Bruno Moniz", texto, 0.1 * channel, 0.9)],
                                       "fixture", {})
        result = self.run_transcription(com_nome)
        self.assertEqual({s.speaker for s in result.utterances}, set(LABELS))
        self.assertNotIn("Bruno", result.text)
        self.assertFalse(result.raw_response["identity_inferred"])

    def test_each_channel_reports_its_own_duration_to_the_caller(self):
        duracoes = []
        def callback(path, duration):
            duracoes.append(duration)
            return self.result(int(path.stem[-1]))
        result = self.run_transcription(callback)
        self.assertEqual(len(duracoes), 2)
        for medida in duracoes:
            self.assertAlmostEqual(medida, 1.0, delta=0.2)
        self.assertEqual([c["duration_seconds"] for c in result.raw_response["channels"]], duracoes)

    def test_channel_over_the_ceiling_is_refused_before_reaching_the_provider(self):
        with patch("castanha.channel_transcription.MAX_CHANNEL_SECONDS", 0.5):
            with self.assertRaises(ValueError):
                self.run_transcription(lambda *_: self.fail("teto estourado não envia áudio"))
        self.assertFalse(self.checkpoints())

    def test_budget_is_consulted_per_channel_and_refusal_keeps_the_first_one(self):
        orcamento = OrcamentoFalso([True, False])
        chamados = []
        def callback(path, duration):
            chamados.append(int(path.stem[-1]))
            return self.result(int(path.stem[-1]))
        with self.assertRaises(ValueError):
            self.run_transcription(callback, budget=orcamento)
        # Dois canais são dois envios: o orçamento é consultado uma vez por canal.
        self.assertEqual(len(orcamento.consultas), 2)
        self.assertEqual(chamados, [0])
        self.assertEqual(self.checkpoints(), ["channel-0.json"])
        # Com orçamento de volta, só o canal que faltava é cobrado.
        result = self.run_transcription(callback, budget=OrcamentoFalso())
        self.assertEqual(chamados, [0, 1])
        self.assertEqual(len(result.utterances), 2)


if __name__ == "__main__":
    unittest.main()
