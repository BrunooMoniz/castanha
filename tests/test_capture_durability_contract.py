"""O original precisa sobreviver mesmo se a transcrição morrer imediatamente."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.audio import ChannelLevels
from castanha.engine import CastanhaEngine


class SimulatedProcessDeath(BaseException):
    pass


class TestCaptureDurabilityContract(unittest.TestCase):
    def test_original_is_durable_before_processing_can_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config" / "castanha"
            config.mkdir(parents=True)
            meetings = root / "meetings"
            (config / "config.json").write_text(json.dumps({
                "storage": {
                    "base_dir": str(meetings),
                    "bronze_dir": str(meetings / "bronze"),
                    "silver_dir": str(meetings / "silver"),
                    "gold_dir": str(meetings / "gold"),
                },
                "zinom": {"enabled": False, "token": ""},
                "llm": {"api_key": ""},
                "transcription": {"groq_api_key": ""},
            }), encoding="utf-8")
            source = root / "volatile-audio.ogg"
            original = b"fixture-original-audio-that-must-survive"
            source.write_bytes(original)
            with patch.dict("os.environ", {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
            }), patch("castanha.engine.notify"), \
                 patch("castanha.engine.measure_channel_levels", return_value=[
                     ChannelLevels(0, "microfone", -30, -10, False),
                 ]), patch("castanha.engine.probe_duration_seconds", return_value=30), \
                 patch("castanha.engine.get_transcriber") as transcriber:
                engine = CastanhaEngine()
                engine.state_mgr.write({
                    "status": "recording", "pid": None,
                    "audio_path": str(source), "mode": "mic-only",
                    "current_meeting": {"title": "Aceite sintético"},
                })
                transcriber.return_value.transcribe.side_effect = SimulatedProcessDeath
                try:
                    engine.stop_recording()
                except SimulatedProcessDeath:
                    pass
                durable = [
                    path for path in meetings.rglob("*")
                    if path.is_file() and path.suffix in {".ogg", ".opus", ".wav"}
                    and path.read_bytes() == original
                ]
                self.assertTrue(durable, "O processamento começou sem preservar o original")


if __name__ == "__main__":
    unittest.main()
