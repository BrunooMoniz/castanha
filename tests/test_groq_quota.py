import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from castanha import groq_quota as Q
from castanha.summarizer import MeetingSummarizer, LlmUnavailable
from tests.test_summarizer import _resposta, _http_error


class TestGroqQuota(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": temp.name})
        env.start(); self.addCleanup(env.stop)

    def summarizer(self, key="fixture"):
        with patch("castanha.summarizer.load_config", return_value={"llm": {"provider": "groq", "api_key": key}}):
            return MeetingSummarizer()

    def test_known_quota_survives_process_and_cached_result_is_still_available(self):
        s = self.summarizer()
        with patch("urllib.request.urlopen", return_value=_resposta("Notas prontas")):
            self.assertEqual(s._call_groq("s", "cached"), "Notas prontas")
        with patch.object(Q.time, "time", return_value=100):
            Q.record_daily_quota("fixture", "60")
        with patch.object(Q.time, "time", return_value=120), patch("urllib.request.urlopen") as http:
            resumed = self.summarizer()
            self.assertEqual(resumed._call_groq("s", "cached"), "Notas prontas")
            with self.assertRaisesRegex(LlmUnavailable, "Cota diária"):
                resumed._call_groq("s", "missing")
            http.assert_not_called()
            self.assertIsNone(Q.blocked_until("other-account"))
        with patch.object(Q.time, "time", return_value=160):
            self.assertIsNone(Q.blocked_until("fixture"))

    def test_http_daily_limit_records_retry_after_without_sleep_or_more_calls(self):
        with patch.object(Q.time, "time", return_value=100), \
                patch("urllib.request.urlopen", side_effect=_http_error(429, "tokens per day (TPD)", {"Retry-After": "90"})) as http, \
                patch("castanha.summarizer.time.sleep") as sleep:
            with self.assertRaises(LlmUnavailable): self.summarizer()._call_groq("s", "u")
            self.assertEqual(Q.blocked_until("fixture"), 190)
            http.assert_called_once(); sleep.assert_not_called()

    def test_unknown_reset_is_conservative_and_does_not_store_key(self):
        with patch.object(Q.time, "time", return_value=100):
            self.assertEqual(Q.record_daily_quota("private-key"), 86500)
        for p in (self.root / "castanha/llm/quota").glob("*.json"):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-key", p.read_text())

    def test_http_date_and_corrupt_checkpoint(self):
        with patch.object(Q.time, "time", return_value=100):
            self.assertEqual(Q.record_daily_quota("fixture", "Thu, 01 Jan 1970 00:03:20 GMT"), 200)
            p=Q._path("fixture");p.write_text('{"until": "invalid"}')
            with self.assertRaises(ValueError): Q.blocked_until("fixture")


if __name__ == "__main__": unittest.main()
