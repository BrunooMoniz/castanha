import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from castanha.bronze_ingest import (BronzeIngestError, build_transcript_request,
                                    prepare_transcript_upload, submit_transcript_upload)


class TestBronzeEnvelope(unittest.TestCase):
    def build(self, text="Transcrição de teste.", **metadata):
        return build_transcript_request("fixture-meeting", {
            "title": "Reunião sintética", "recorded_at": "2026-09-05T10:30:11",
            "transcription_provider": "groq", "audio_status": "ok", **metadata,
        }, text, captured_at="2026-09-05T17:00:00-03:00")

    def test_integral_unicode_content_and_exact_hash(self):
        text = "Decisão com ação e emoji 🌰\r\n" * 4000
        envelope = self.build(text)["envelope"]
        self.assertEqual(envelope["texto"], text)
        self.assertEqual(envelope["proveniencia"]["sha256_texto"],
                         hashlib.sha256(text.encode()).hexdigest())

    def test_retry_is_identical_but_changed_text_has_new_revision(self):
        first = self.build()
        self.assertEqual(first, self.build())
        changed = self.build("Conteúdo corrigido")
        self.assertNotEqual(first["idempotency_key"], changed["idempotency_key"])
        self.assertNotEqual(first["envelope"]["source_id"], changed["envelope"]["source_id"])
        self.assertEqual(first["envelope"]["proveniencia"]["referencia"],
                         changed["envelope"]["proveniencia"]["referencia"])

    def test_does_not_invent_account_presence_or_facts(self):
        request = self.build(calendar_event={"attendees": [{"name": "Convidado"}]})
        self.assertNotIn("account_id", request["envelope"])
        self.assertNotIn("presenca_confirmada", request["envelope"]["proveniencia"])
        self.assertEqual(request["facts"], [])
        self.assertEqual(request["envelope"]["fidelidade"], "projecao")

    def test_legacy_naive_time_preserves_date_without_inventing_timezone(self):
        self.assertEqual(self.build()["envelope"]["timestamp"]["valor"], "2026-09-05")
        self.assertEqual(self.build(recorded_at="2026-09-05T10:30:00-03:00")
                         ["envelope"]["timestamp"]["valor"], "2026-09-05T10:30:00-03:00")

    def test_rejects_untrustworthy_or_deleted_content(self):
        for metadata in ({"transcription_provider": "mock"},
                         {"transcription_provider": "failed"},
                         {"processing_status": "pending"}, {"audio_status": "sem_audio"},
                         {"zinom": {"status": "tombstoned"}}, {"zinom": ["bad"]},
                         {"recordings": [{"id": "bad", "transcription_provider": "mock"}]}):
            with self.subTest(metadata=metadata), self.assertRaises(BronzeIngestError):
                self.build(**metadata)

    def test_oversize_is_rejected_not_truncated(self):
        with self.assertRaises(BronzeIngestError):
            self.build("á" * (4 * 1024 * 1024 + 1))

    def test_capture_requires_explicit_timezone(self):
        with self.assertRaises(BronzeIngestError):
            build_transcript_request("fixture", {}, "texto", captured_at="2026-09-05T10:00:00")

    def test_missing_source_date_is_explicit(self):
        self.assertEqual(self.build(recorded_at=None)["envelope"]["timestamp"],
                         {"valor": None, "origem": "sem_data"})


class TestBronzeUpload(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.metadata = {"title": "Fixture", "transcription_provider": "groq"}
        self.path = self.prepare()

    def prepare(self, instant="2026-09-05T17:00:00-03:00", text="transcrição"):
        return prepare_transcript_upload(self.directory, "fixture", self.metadata, text,
                                         captured_at=instant)

    def response(self, status="pending", **extra):
        return {"content": [{"type": "text", "text": json.dumps({
            "ok": True, "jobId": 12, "revisionId": 34, "status": status, **extra})}]}

    def test_restart_reuses_frozen_request_despite_later_clock(self):
        original = self.path.read_bytes()
        self.assertEqual(self.path, self.prepare("2026-09-06T18:00:00-03:00"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_new_revision_keeps_previous_checkpoint(self):
        original = self.path.read_bytes()
        other = self.prepare(text="transcrição corrigida")
        self.assertNotEqual(self.path, other)
        self.assertEqual(self.path.read_bytes(), original)

    def test_lost_response_replays_exact_request_without_legacy_fallback(self):
        client = Mock()
        client.call_tool.side_effect = [TimeoutError("fixture"), self.response("completed")]
        first = submit_transcript_upload(self.path, client)
        second = submit_transcript_upload(self.prepare(), client)
        self.assertEqual(first["status"], "error")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(client.call_tool.call_args_list[0], client.call_tool.call_args_list[1])
        self.assertEqual(client.call_tool.call_args.args[0], "brain_ingest")

    def test_queue_ack_is_not_searchable_success(self):
        for state, expected in (("pending", "pending"), ("processing", "pending"),
                                ("retry", "pending"), ("failed", "error"),
                                ("tombstoned", "tombstoned"), ("completed", "ok")):
            with self.subTest(state=state):
                client = Mock()
                client.call_tool.return_value = self.response(state)
                self.assertEqual(submit_transcript_upload(self.path, client)["status"], expected)

    def test_invalid_receipt_cannot_report_success(self):
        client = Mock()
        client.call_tool.return_value = self.response("completed", jobId=True)
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")

    def test_corrupt_checkpoint_is_preserved_without_network(self):
        saved = json.loads(self.path.read_text())
        saved["request"]["envelope"]["texto"] = "alterado"
        self.path.write_text(json.dumps(saved))
        original = self.path.read_bytes()
        client = Mock()
        with self.assertRaises(BronzeIngestError):
            submit_transcript_upload(self.path, client)
        with self.assertRaises(BronzeIngestError):
            self.prepare()
        client.connect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
