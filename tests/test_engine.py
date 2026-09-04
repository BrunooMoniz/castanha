import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.engine import CastanhaEngine

class TestEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.patch_cfg = patch("castanha.config.get_config_dir", return_value=self.temp_dir / "config")
        self.patch_state = patch("castanha.config.get_state_dir", return_value=self.temp_dir / "state")
        self.patch_cfg.start()
        self.patch_state.start()

    def tearDown(self):
        self.patch_cfg.stop()
        self.patch_state.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_engine_lifecycle(self):
        engine = CastanhaEngine()
        engine.storage.base_dir = self.temp_dir / "meetings"
        engine.storage.bronze_dir = self.temp_dir / "meetings" / "bronze"
        engine.storage.silver_dir = self.temp_dir / "meetings" / "silver"
        engine.storage.gold_dir = self.temp_dir / "meetings" / "gold"
        for d in [engine.storage.bronze_dir, engine.storage.silver_dir, engine.storage.gold_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 1. Start recording in mic_only mode for fast test
        res_start = engine.start_recording(mode="mic_only", title="Reunião de Teste")
        self.assertEqual(res_start["status"], "recording")
        self.assertIsNotNone(res_start["pid"])

        # Deixa gravar por 1 segundo
        time.sleep(1.2)

        # 2. Status
        status = engine.get_status()
        self.assertEqual(status["status"], "recording")
        self.assertEqual(status["mode"], "mic_only")

        # 3. Stop recording
        res_stop = engine.stop_recording()
        self.assertEqual(res_stop["status"], "success")
        result = res_stop["result"]
        self.assertTrue(Path(result["bronze_dir"]).exists())
        self.assertTrue(Path(result["silver_file"]).exists())
        self.assertTrue(Path(result["gold_file"]).exists())

        # 4. Status final
        final_status = engine.get_status()
        self.assertEqual(final_status["status"], "idle")

if __name__ == "__main__":
    unittest.main()
