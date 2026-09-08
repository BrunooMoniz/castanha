"""Prova o layout com o Button real do shell do Omarchy."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")


class PluginLayoutTest(unittest.TestCase):
    def test_panel_wires_meter_only_while_recording(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn('enabled: root.isRecording && !!root.micSource', panel)
        self.assertIn(
            'enabled: root.isRecording && root.mode === "dual" && !!root.systemSink',
            panel,
        )
        self.assertIn(
            'AudioMeterLogic.combinedPeak(\n'
            '    micPeakMonitor.peak, systemPeakMonitor.peak, mode, micMuted)',
            panel,
        )
        self.assertIn(
            'if (isRecording) return glyph + "  " + formatTime(elapsedSeconds) + "  " + audioMeter.text',
            panel,
        )
        self.assertIn(
            'if (isPaused) return glyph + "  " + formatTime(elapsedSeconds)',
            panel,
        )
        self.assertIn('onModeChanged: audioMeter.reset()', panel)
        self.assertIn('onMicSourceChanged: audioMeter.reset()', panel)
        self.assertIn('onSystemSinkChanged: audioMeter.reset()', panel)

    def test_audio_meter_compiles_with_real_pipewire_monitor(self):
        if not shutil.which("quickshell"):
            self.skipTest("quickshell não instalado")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
            (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
            (config / "AudioMeter.qml").symlink_to(ROOT / "AudioMeter.qml")
            (config / "AudioMeter.js").symlink_to(ROOT / "AudioMeter.js")
            shutil.copy2(ROOT / "tests/fixtures/audio_meter_shell.qml", config / "shell.qml")

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("WAYLAND_DISPLAY", None)
            result = subprocess.run(
                ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CASTANHA_AUDIO_METER_OK", result.stdout)
        self.assertNotIn("CASTANHA_AUDIO_METER_FAIL", result.stdout)

    def test_real_omarchy_buttons_stay_inside_panel(self):
        if not shutil.which("quickshell"):
            self.skipTest("quickshell não instalado")
        if not (OMARCHY_SHELL / "Ui").is_dir() or not (OMARCHY_SHELL / "Commons").is_dir():
            self.skipTest("componentes do shell do Omarchy indisponíveis")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
            (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
            (config / "PanelActionFlow.qml").symlink_to(ROOT / "PanelActionFlow.qml")
            (config / "i18n.js").symlink_to(ROOT / "i18n.js")
            shutil.copy2(ROOT / "tests/fixtures/real_button_layout_shell.qml", config / "shell.qml")

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("WAYLAND_DISPLAY", None)
            result = subprocess.run(
                ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CASTANHA_REAL_LAYOUT_OK", result.stdout)
        self.assertNotIn("CASTANHA_REAL_LAYOUT_FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
