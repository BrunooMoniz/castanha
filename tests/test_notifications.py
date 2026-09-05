import contextlib
import io
import unittest
from unittest.mock import patch

from castanha.engine import notify


class TestNotifications(unittest.TestCase):
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
