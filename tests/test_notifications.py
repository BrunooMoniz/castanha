import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from castanha.engine import MICROPHONE_GLYPH, notify


class TestNotifications(unittest.TestCase):
    def test_notification_uses_theme_colored_microphone_glyph(self):
        process = Mock()
        with patch("castanha.engine.subprocess.Popen", return_value=process) as launch:
            self.assertIsNone(notify("Gravação iniciada", "Capturando áudio"))
        command = launch.call_args.args[0]
        self.assertIn(f"string:omarchy-glyph:{MICROPHONE_GLYPH}", command)
        self.assertNotIn("audio-input-microphone", command)

    def test_action_notification_keeps_glyph_and_actions(self):
        completed = Mock(stdout="record\n")
        with patch("castanha.engine.subprocess.run", return_value=completed) as launch:
            self.assertEqual(notify("Reunião", "Começa agora", actions=[("record", "Gravar")]), "record")
        command = launch.call_args.args[0]
        self.assertIn(f"string:omarchy-glyph:{MICROPHONE_GLYPH}", command)
        self.assertIn("record=Gravar", command)
        self.assertNotIn("audio-input-microphone", command)

    def test_missing_desktop_notifier_does_not_abort_recording_work(self):
        output = io.StringIO()
        with patch("castanha.engine.subprocess.Popen", side_effect=FileNotFoundError("fixture")), \
                contextlib.redirect_stderr(output):
            self.assertIsNone(notify("Título privado", "Texto privado"))
        self.assertIn("FileNotFoundError", output.getvalue())
        self.assertNotIn("privado", output.getvalue())

    def test_action_notification_failure_is_reported_without_content(self):
        output = io.StringIO()
        with patch("castanha.engine.subprocess.run", side_effect=OSError("privado")), \
                contextlib.redirect_stderr(output):
            self.assertIsNone(notify("Título privado", "Texto privado", actions=[("go", "Abrir")]))
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("privado", output.getvalue())


if __name__ == "__main__":
    unittest.main()
