"""A cota é lida sem consultar provedores nem transportar credenciais."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from castanha.hermes_quota import quota_state, remote_command
from castanha.summarizer import HermesSshLlm, LlmUnavailable, _split_exact


class TestQuota(unittest.TestCase):
    def test_minimum_chunk_budget_always_advances(self):
        self.assertEqual(_split_exact("ação", 1), ["a", "ç", "ã", "o"])

    def state(self, rows, now=100):
        return quota_state({"credential_pool": {"anthropic": rows}}, "anthropic", now)

    def test_all_credentials_and_reset(self):
        exhausted = {"last_status": "exhausted", "last_error_reset_at": 200}
        self.assertTrue(self.state([exhausted])["blocked"])
        self.assertFalse(self.state([exhausted], 200)["blocked"])
        self.assertFalse(self.state([exhausted, {"last_status": "ok"}])["blocked"])
        self.assertTrue(self.state([exhausted, {"last_status": "dead"}])["blocked"])
        self.assertFalse(self.state([])["blocked"])

    def test_unknown_reset_is_not_a_provider_probe(self):
        self.assertEqual(self.state([{"last_status": "exhausted"}]),
                         {"blocked": True, "reset_at": None})

    def test_reset_formats(self):
        for reset in [1789440349, 1789440349000, "2026-09-15T02:45:49Z"]:
            state = self.state([{"last_status": "exhausted", "last_error_reset_at": reset}])
            self.assertEqual(state["reset_at"], 1789440349)

    def test_remote_script_only_emits_status_and_never_changes_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory)/"auth.json"
            raw = json.dumps({"credential_pool": {"anthropic": [{"api_key": "SECRET",
                "last_status": "exhausted", "last_error_reset_at": 9789440349}]}})
            auth.write_text(raw)
            proc = subprocess.run(remote_command("anthropic"), shell=True, capture_output=True,
                                  text=True, env={**os.environ, "HERMES_HOME": directory})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout), {"blocked": True, "reset_at": 9789440349})
            self.assertNotIn("SECRET", proc.stdout + proc.stderr)
            self.assertEqual(auth.read_text(), raw)
            auth.write_text("broken")
            proc = subprocess.run(remote_command("anthropic"), shell=True, capture_output=True,
                                  text=True, env={**os.environ, "HERMES_HOME": directory})
            self.assertEqual(json.loads(proc.stdout), {"unavailable": True})

    def test_exhausted_does_not_launch_upload_or_delete_and_done_reuses_cache(self):
        llm = HermesSshLlm("fixture", [{"provider": "anthropic", "model": "model"}])
        with patch.object(llm, "_state", return_value="FAILED"), \
                patch.object(llm, "_ssh", return_value='{"blocked":true}') as ssh, \
                patch.object(llm, "_launch") as launch, patch.object(llm, "_upload") as upload:
            with self.assertRaises(LlmUnavailable): llm.complete("s", "u")
            launch.assert_not_called()
            upload.assert_not_called()
            self.assertEqual(ssh.call_count, 1)
            self.assertNotIn("rm -f", ssh.call_args.args[0])
        with patch.object(llm, "_state", return_value="DONE"), \
                patch.object(llm, "_ssh", return_value="cached") as ssh:
            self.assertEqual(llm.complete("s", "u"), "cached")
            self.assertTrue(ssh.call_args.args[0].startswith("cat "))


if __name__ == "__main__":
    unittest.main()
