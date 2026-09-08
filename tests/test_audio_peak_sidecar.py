"""Contrato do medidor auxiliar: o pico vem da captura real, não do QML."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from castanha.audio import (AudioDeviceInfo, AudioRecorder, is_safe_capture_peak,
                            read_audio_peak)
from castanha.daemon import CastanhaDaemon
from castanha.state import StateManager


class TestAudioPeakSidecar(unittest.TestCase):
    def test_le_o_ultimo_pico_e_aplica_escala_perceptual(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.peak"
            path.write_text(
                "frame:0\nlavfi.astats.Overall.Peak_level=-60.000000\n"
                "frame:1\nlavfi.astats.Overall.Peak_level=-18.063656\n",
                encoding="utf-8",
            )
            expected = (10 ** (-18.063656 / 20)) ** (1 / 3)
            self.assertAlmostEqual(read_audio_peak(path), expected, places=5)

    def test_silencio_e_zero_e_arquivo_parado_e_invalido(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.peak"
            path.write_text("lavfi.astats.Overall.Peak_level=-inf\n", encoding="utf-8")
            self.assertEqual(read_audio_peak(path), 0.0)
            old = time.time() - 10
            os.utime(path, (old, old))
            self.assertIsNone(read_audio_peak(path))

    def test_comando_mede_o_misto_dual_e_o_microfone_no_mic_only(self):
        recorder = AudioRecorder()
        recorder.devices = AudioDeviceInfo("mic", "sink", "sink.monitor")
        with tempfile.TemporaryDirectory() as directory:
            recorder.peak_path = Path(directory) / "capture.peak"
            recorder.mode = "dual"
            stats = recorder._meter_stats()
            self.assertIn("lavfi.astats.Overall.Peak_level", stats)
            self.assertIn("asetnsamples=n=12000", stats)
            recorder.mode = "mic_only"
            self.assertEqual(recorder._meter_stats(), stats)

    def test_stop_de_outra_instancia_limpa_o_artefato_persistido(self):
        audio = Path("/tmp/castanha_rec_987654321.ogg")
        peak = AudioRecorder._peak_path(audio)
        try:
            peak.write_text("lavfi.astats.Overall.Peak_level=-18\n", encoding="utf-8")
            stopper = AudioRecorder()
            stopper.peak_path = peak
            stopper.stop_meter()
            self.assertIsNone(stopper.peak_path)
        finally:
            peak.unlink(missing_ok=True)

    def test_estado_nao_pode_apagar_caminho_arbitrario(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "capture.ogg"
            arbitrary = Path(directory) / "not-delete-me.peak"
            arbitrary.write_text("preservar", encoding="utf-8")
            self.assertFalse(is_safe_capture_peak(audio, arbitrary))
            self.assertTrue(arbitrary.exists())

    def test_pico_atualiza_e_expira_enquanto_agenda_bloqueia(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_home = root / "state"
            peak = root / "capture.peak"
            peak.write_text("lavfi.astats.Overall.Peak_level=-18\n", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                daemon = CastanhaDaemon.__new__(CastanhaDaemon)
                daemon.config = {"calendar": {"enabled": True, "feeds": [{"url": "blocked"}],
                                                "zinom": {}, "poll_interval_sec": 0}}
                daemon.state_mgr = StateManager()
                daemon.running = True
                daemon.notified_meeting_uids = set()
                daemon.retry_scheduler = SimpleNamespace(tick=lambda **kwargs: None)
                daemon._agenda_thread = None
                daemon._agenda_result = None
                daemon._agenda_lock = __import__("threading").Lock()
                daemon.state_mgr.write({"status": "recording", "started_at": "2026-09-08T00:00:00",
                                        "audio_peak_path": str(peak), "audio_peak": 0.0})
                writes = []
                original_write = daemon.state_mgr.write

                def record_write(updates):
                    writes.append(dict(updates))
                    return original_write(updates)

                daemon.state_mgr.write = record_write
                release = __import__("threading").Event()
                loops = [0]

                def blocked(_config):
                    release.wait()
                    return []

                def sleep(_interval):
                    loops[0] += 1
                    if loops[0] == 1:
                        old = time.time() - 10
                        os.utime(peak, (old, old))
                    if loops[0] >= 2:
                        daemon.running = False

                with patch("castanha.daemon.collect_upcoming", side_effect=blocked), \
                     patch("castanha.daemon.signal.signal"), \
                     patch("castanha.daemon.time.sleep", side_effect=sleep):
                    daemon.run()
                release.set()
                if daemon._agenda_thread:
                    daemon._agenda_thread.join(timeout=1)
                projected = [w["audio_peak"] for w in writes if "audio_peak" in w]
                self.assertTrue(any(value > 0 for value in projected))
                self.assertEqual(projected[-1], 0.0)


if __name__ == "__main__":
    unittest.main()
