import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.audio import ChannelLevels
from castanha.engine import CastanhaEngine
from castanha.transcription import TranscriptionResult


class FakeProcess:
    def __init__(self, pid=424242):
        self.pid = pid

    def poll(self):
        return None


def _levels(mic_silent: bool, sys_silent: bool):
    return [
        ChannelLevels(0, "microfone", -91.0 if mic_silent else -34.4,
                      -91.0 if mic_silent else -14.1, mic_silent),
        ChannelLevels(1, "sistema", -91.0 if sys_silent else -48.5,
                      -91.0 if sys_silent else -10.6, sys_silent),
    ]


class TestEngine(unittest.TestCase):
    """Isolamento total: XDG apontado para um tempdir.

    Antes esta suíte patchava `castanha.config.get_state_dir`, que os módulos
    já tinham importado por nome, então ela escrevia no state.json real e
    criava reuniões falsas em ~/Notes/Meetings.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.meetings = self.temp_dir / "meetings"
        cfg_dir = self.temp_dir / "config" / "castanha"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({
            "storage": {
                "base_dir": str(self.meetings),
                "bronze_dir": str(self.meetings / "bronze"),
                "silver_dir": str(self.meetings / "silver"),
                "gold_dir": str(self.meetings / "gold"),
            },
            "transcription": {"groq_api_key": "", "vps_ssh_host": ""},
            "llm": {"api_key": ""},
            "zinom": {"enabled": False, "token": ""},
        }), encoding="utf-8")

        self.env = patch.dict("os.environ", {
            "XDG_CONFIG_HOME": str(self.temp_dir / "config"),
            "XDG_STATE_HOME": str(self.temp_dir / "state"),
        })
        self.env.start()

        self.audio_file = self.temp_dir / "gravacao.ogg"
        self.audio_file.write_bytes(b"ogg-de-mentira")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _engine(self):
        engine = CastanhaEngine()
        engine.recorder.start = lambda path, mode="dual", bitrate="64k": (
            Path(path).write_bytes(b"ogg-de-mentira"), FakeProcess()
        )[1]
        return engine

    def _stop(self, engine, levels, transcript="Olá, isto é um teste."):
        with patch("castanha.engine.measure_channel_levels", return_value=levels), \
             patch("castanha.engine.probe_duration_seconds", return_value=42.0), \
             patch("castanha.engine.get_transcriber") as get_t, \
             patch("castanha.engine.notify"), \
             patch("os.kill"):
            get_t.return_value.transcribe.return_value = TranscriptionResult(
                text=transcript, utterances=[], provider="fake", raw_response={},
            )
            res = engine.stop_recording()
            return res, get_t

    def test_ciclo_completo_com_audio_bom(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            start = engine.start_recording(mode="dual", title="Reunião de Teste")
        self.assertEqual(start["status"], "recording")

        res, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=True))
        self.assertEqual(res["status"], "success")
        result = res["result"]
        self.assertEqual(result["audio_status"], "ok")
        self.assertTrue(Path(result["bronze_dir"]).exists())
        self.assertTrue(Path(result["silver_file"]).exists())
        self.assertTrue(Path(result["gold_file"]).exists())
        self.assertEqual(engine.get_status()["status"], "idle")

        meta = json.loads((Path(result["bronze_dir"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["duration_seconds"], 42.0)  # veio do arquivo, não do cronômetro
        self.assertEqual(len(meta["audio_levels"]), 2)

    def test_mic_mudo_no_inicio_aparece_no_estado_e_no_retorno(self):
        engine = self._engine()
        with patch("castanha.engine.notify") as notify, \
             patch("castanha.engine.is_default_source_muted", return_value=True):
            start = engine.start_recording(mode="dual", title="Reunião")
        self.assertTrue(start["mic_muted"])
        self.assertTrue(engine.get_status()["mic_muted_at_start"])
        self.assertTrue(any("mudo" in str(c).lower() for c in notify.call_args_list))

    def test_mic_mudo_marca_a_reuniao(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=True):
            engine.start_recording(mode="dual", title="Reunião")
        res, _ = self._stop(engine, _levels(mic_silent=True, sys_silent=False))
        self.assertEqual(res["result"]["audio_status"], "mic_mudo")
        meta = json.loads((Path(res["result"]["bronze_dir"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["mic_muted_at_start"])
        self.assertIn("microfone", meta["audio_diagnostico"].lower())

    def test_gravacao_muda_nao_vai_para_transcricao(self):
        """O caso das 10:31: sem isto o Whisper alucina 'Thank you.' em cima do nada."""
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=True):
            engine.start_recording(mode="dual", title="Reunião")
        res, get_t = self._stop(engine, _levels(mic_silent=True, sys_silent=True))
        self.assertEqual(res["result"]["audio_status"], "sem_audio")
        get_t.assert_not_called()
        transcript = (Path(res["result"]["bronze_dir"]) / "transcript_raw.txt").read_text(encoding="utf-8")
        self.assertEqual(transcript, "")

    def test_silver_nao_inventa_decisoes_quando_nao_ha_resumo(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")
        res, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=True))
        silver = Path(res["result"]["silver_file"]).read_text(encoding="utf-8")
        self.assertNotIn("Gravação registrada com sucesso", silver)
        self.assertIn("Sem resumo", silver)
        self.assertIn("Olá, isto é um teste.", silver)

    def test_nao_escreve_fora_do_tempdir(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")
        res, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=True))
        for chave in ("bronze_dir", "silver_file", "gold_file"):
            self.assertTrue(
                str(res["result"][chave]).startswith(str(self.temp_dir)),
                f"{chave} escapou do tempdir: {res['result'][chave]}",
            )


    def test_transcricao_falha_nao_sai_como_sucesso(self):
        """Bronze salvo com transcrição quebrada é 'partial', nunca 'success'."""
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")

        with patch("castanha.engine.measure_channel_levels", return_value=_levels(False, True)), \
             patch("castanha.engine.probe_duration_seconds", return_value=42.0), \
             patch("castanha.engine.get_transcriber") as get_t, \
             patch("castanha.transcription.VpsSshTranscriber") as vps, \
             patch("castanha.engine.notify"), patch("os.kill"):
            get_t.return_value.transcribe.side_effect = RuntimeError("groq caiu")
            vps.return_value.transcribe.side_effect = RuntimeError("vps fora do ar")
            res = engine.stop_recording()

        self.assertEqual(res["status"], "partial")
        result = res["result"]
        self.assertEqual(result["transcription_provider"], "failed")
        self.assertTrue(any("transcrição falhou" in p for p in result["problemas"]))

        # A mensagem de erro não pode virar o texto da reunião.
        transcript = (Path(result["bronze_dir"]) / "transcript_raw.txt").read_text(encoding="utf-8")
        self.assertEqual(transcript, "")
        silver = Path(result["silver_file"]).read_text(encoding="utf-8")
        self.assertNotIn("groq caiu", silver)
        self.assertNotIn("vps fora do ar", silver)

    def test_fallback_para_vps_roda_uma_vez_so(self):
        """Groq caiu: a VPS é tentada UMA vez, e não duas (05/09/2026: dois uploads de 65 MB)."""
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")

        with patch("castanha.engine.measure_channel_levels", return_value=_levels(False, True)), \
             patch("castanha.engine.probe_duration_seconds", return_value=42.0), \
             patch("castanha.engine.get_transcriber") as get_t, \
             patch("castanha.transcription.VpsSshTranscriber") as vps, \
             patch("castanha.engine.notify"), patch("os.kill"):
            get_t.return_value.transcribe.side_effect = RuntimeError("groq caiu")
            vps.return_value.transcribe.side_effect = RuntimeError("vps fora do ar")
            res = engine.stop_recording()

        self.assertEqual(res["result"]["transcription_provider"], "failed")
        self.assertEqual(vps.return_value.transcribe.call_count, 1)

    def test_vps_como_primario_nao_tenta_a_vps_de_novo(self):
        """Sem orçamento para a Groq, o primário já é a VPS: falhou, acabou."""
        from castanha.transcription import VpsSshTranscriber
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")

        with patch("castanha.engine.measure_channel_levels", return_value=_levels(False, True)), \
             patch("castanha.engine.probe_duration_seconds", return_value=42.0), \
             patch("castanha.engine.get_transcriber", return_value=VpsSshTranscriber("host-teste")), \
             patch.object(VpsSshTranscriber, "transcribe", side_effect=RuntimeError("vps caiu")) as t, \
             patch("castanha.engine.notify"), patch("os.kill"):
            res = engine.stop_recording()

        self.assertEqual(res["result"]["transcription_provider"], "failed")
        self.assertEqual(t.call_count, 1)

    def test_gravacao_boa_nao_lista_problema(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião")
        res, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=True))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["result"]["problemas"], [])

    def test_engine_delete_recording(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Alinhamento")
        res, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=False))
        slug = res["result"]["slug"]

        del_res = engine.delete_recording(slug)
        self.assertEqual(del_res["status"], "ok")
        self.assertEqual(del_res["remaining_count"], 0)

        # last_result no estado reflete audio_apagado
        state = engine.get_status()
        self.assertEqual(state["last_result"]["audio_status"], "audio_apagado")

    def test_engine_append_recording_to_existing_meeting(self):
        engine = self._engine()
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", title="Reunião Longa")
        res1, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=False))
        slug = res1["result"]["slug"]

        # Inicia nova gravação vinculada ao mesmo slug
        with patch("castanha.engine.notify"), patch("castanha.engine.is_default_source_muted", return_value=False):
            engine.start_recording(mode="dual", meeting_slug=slug)
        res2, _ = self._stop(engine, _levels(mic_silent=False, sys_silent=False))

        # O slug permaneceu o mesmo e acumulou gravações
        self.assertEqual(res2["result"]["slug"], slug)
        m = engine.storage.get_meeting(slug)
        self.assertEqual(m["recordings_count"], 2)


if __name__ == "__main__":
    unittest.main()

