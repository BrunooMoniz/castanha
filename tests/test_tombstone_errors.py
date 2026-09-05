import json
import unittest
from unittest.mock import patch

from castanha.zinom_adapter import ZinomAdapter, ZinomError, ZinomMcpClient


class TestTombstoneErrors(unittest.TestCase):
    def result(self, error):
        with patch("castanha.zinom_adapter.load_config", return_value={
            "zinom": {"enabled": True, "token": "fixture", "endpoint": "https://fixture.invalid"}}), \
                patch("castanha.zinom_adapter.ZinomMcpClient") as client:
            client.return_value.call_tool.side_effect = error
            result = ZinomAdapter().ingest_meeting(
                {"title": "Fixture", "audio_status": "ok", "transcription_provider": "groq"},
                "Texto de teste", {"facts": []}, previous_remember_id="conversation:fixture")
            self.assertEqual([call.args[0] for call in client.return_value.call_tool.call_args_list],
                             ["brain_update"])
            return result

    def test_proxy_rpc_and_unknown_missing_memory_are_not_user_deletions(self):
        for error in (ZinomError("HTTP 404: Not Found"), ZinomError("Method not found"),
                      ZinomError("Memory not found"),
                      ZinomError("Missing", code="not_found", tool="brain_update"),
                      ZinomError("Deleted", code="source_tombstoned", tool="other_tool")):
            with self.subTest(message=str(error)):
                self.assertEqual(self.result(error)["status"], "error")

    def test_only_explicit_structured_deletion_from_update_is_tombstone(self):
        error = ZinomError("Deleted", code="source_tombstoned", tool="brain_update")
        self.assertEqual(self.result(error)["status"], "tombstoned")

    def test_transport_preserves_structured_tool_error_code(self):
        client = ZinomMcpClient("https://fixture.invalid", "fixture")
        body = {"content": [{"type": "text", "text": json.dumps({
            "ok": False, "error": "source_tombstoned", "message": "Deleted"})}]}
        with patch.object(client, "_rpc", return_value=body), self.assertRaises(ZinomError) as raised:
            client.call_tool("brain_update", {"id": "fixture"})
        self.assertEqual(raised.exception.code, "source_tombstoned")
        self.assertEqual(raised.exception.tool, "brain_update")
