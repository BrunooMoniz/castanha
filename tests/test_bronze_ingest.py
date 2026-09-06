import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock

from castanha.bronze_ingest import (BronzeIngestError, build_transcript_request,
                                    prepare_transcript_upload, submit_transcript_upload)
from castanha.zinom_adapter import ZinomError, ZinomMcpClient


class TestBronzeEnvelope(unittest.TestCase):
    def build(self, text="Transcrição de teste.", **metadata):
        return build_transcript_request("fixture-meeting", {
            "title": "Reunião sintética", "recorded_at": "2026-09-05T10:30:11",
            "transcription_provider": "groq", "audio_status": "ok", **metadata,
        }, text, captured_at="2026-09-05T17:00:00-03:00", workspace="fixture-workspace", recording_id="native-fixture")

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
        self.assertEqual(first["envelope"]["source_id"], changed["envelope"]["source_id"])
        self.assertEqual(first["envelope"]["proveniencia"]["origem_id"],
                         changed["envelope"]["proveniencia"]["origem_id"])

    def test_explicit_workspace_and_stable_origin_are_required(self):
        request = self.build()
        self.assertEqual(request["envelope"]["workspace"], "fixture-workspace")
        self.assertEqual(request["envelope"]["source_id"],
                         request["envelope"]["proveniencia"]["origem_id"])
        for workspace in (None, "", " ", " fixture", 123):
            with self.subTest(workspace=workspace), self.assertRaises(BronzeIngestError):
                build_transcript_request("fixture", {}, "texto", workspace=workspace,
                                         captured_at="2026-09-05T10:00:00-03:00")

    def test_native_id_is_required_and_independent_of_slug(self):
        with self.assertRaises(BronzeIngestError):
            build_transcript_request("fixture", {}, "texto", workspace="fixture-workspace",
                                     captured_at="2026-09-05T10:00:00-03:00")
        first = self.build()
        renamed = build_transcript_request("renamed", {}, "outro texto", workspace="fixture-workspace",
            recording_id="native-fixture", captured_at="2026-09-05T10:00:00-03:00")
        self.assertEqual(first["envelope"]["source_id"], renamed["envelope"]["source_id"])

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
            build_transcript_request("fixture", {}, "texto", workspace="fixture-workspace", recording_id="native-fixture",
                                     captured_at="2026-09-05T10:00:00")

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
                                         captured_at=instant, workspace="fixture-workspace", recording_id="native-fixture")

    def response(self, state="pending", **extra):
        return {"content": [{"type": "text", "text": json.dumps({
            "ok": True, "jobId": 12, "revisionId": 34, "status": state,
            "checkpoint": 2 if state == "completed" else 0, "replay": True, "attempts": 1, "lastError": None,
            "sourceId": json.loads(self.path.read_text())["request"]["envelope"]["source_id"],
            "sourceType": "castanha", **extra})}]}

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
        first_request = client.call_tool.call_args_list[0].args[1]
        self.assertEqual(client.call_tool.call_args_list[1].args[1],
                         {"idempotency_key": first_request["idempotency_key"]})
        self.assertEqual(client.call_tool.call_args.args[0], "brain_ingest")

    def test_queue_ack_is_not_searchable_success(self):
        for state, expected in (("pending", "pending"), ("processing", "pending"),
                                ("retry", "pending"), ("failed", "error"),
                                ("completed", "ok"), ("tombstoned", "tombstoned")):
            with self.subTest(state=state):
                client = Mock()
                client.call_tool.return_value = self.response(state)
                self.assertEqual(submit_transcript_upload(self.path, client)["status"], expected)

    def test_invalid_receipt_cannot_report_success(self):
        client = Mock()
        client.call_tool.return_value = self.response("completed", jobId=True)
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")

    def test_receipt_rejects_unsafe_ids_incomplete_ack_and_wrong_initial_origin(self):
        for extra in ({"jobId": 9007199254740992}, {"revisionId": 9007199254740992},
                      {"checkpoint": 0}, {"checkpoint": 3}, {"sourceId": "wrong"},
                      {"sourceType": "wrong"}):
            with self.subTest(extra=extra):
                self.path = self.prepare(text=json.dumps(extra))
                client = Mock()
                client.call_tool.return_value = self.response("completed", **extra)
                self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")

    def test_initial_enqueue_accepts_only_server_enqueue_fields(self):
        client = Mock()
        client.call_tool.return_value = {"content": [{"type": "text", "text": json.dumps({
            "ok": True, "jobId": 12, "revisionId": 34, "status": "pending",
            "checkpoint": 0, "replay": False})}]}
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "pending")

    def test_only_typed_ingest_tombstone_is_terminal(self):
        for code, tool, expected in (("source_tombstoned", "brain_ingest", "tombstoned"),
                                     ("not_found", "brain_ingest", "error"),
                                     ("source_tombstoned", "brain_update", "error")):
            with self.subTest(code=code, tool=tool):
                self.path = self.prepare(text=f"fixture {code} {tool}")
                client = Mock()
                client.call_tool.side_effect = ZinomError("fixture", code=code, tool=tool)
                self.assertEqual(submit_transcript_upload(self.path, client)["status"], expected)

    def test_unknown_key_reuses_frozen_envelope_without_changing_destination(self):
        client = Mock()
        client.call_tool.side_effect = [TimeoutError(),
            ZinomError("fixture", code="unknown_idempotency_key", tool="brain_ingest"),
            self.response("completed")]
        submit_transcript_upload(self.path, client)
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "ok")
        self.assertEqual(client.call_tool.call_args_list[0], client.call_tool.call_args_list[2])

    def test_lookup_must_match_origin(self):
        client = Mock()
        client.call_tool.side_effect = [self.response(), self.response("completed", sourceId="other")]
        submit_transcript_upload(self.path, client)
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")

    def test_lookup_cannot_change_job_identity_or_return_malformed_state(self):
        for extra in ({"jobId": 99}, {"revisionId": 99}, {"checkpoint": True},
                      {"checkpoint": -1}, {"attempts": True}, {"lastError": {}},
                      {"replay": False}, {"status": None}):
            self.path = self.prepare(text=json.dumps(extra))
            client = Mock()
            client.call_tool.side_effect = [self.response(), self.response("completed", **extra)]
            submit_transcript_upload(self.path, client)
            self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")

    def test_authorization_error_does_not_trigger_full_upload_fallback(self):
        client = Mock()
        client.call_tool.side_effect = [self.response(),
            ZinomError("fixture", code="workspace_forbidden", tool="brain_ingest")]
        submit_transcript_upload(self.path, client)
        self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")
        self.assertEqual(client.call_tool.call_count, 2)
        self.assertEqual(list(client.call_tool.call_args.args[1]), ["idempotency_key"])

    def test_deleted_and_superseded_revisions_are_terminal_without_resend(self):
        for state in ("tombstoned", "superseded"):
            self.path = self.prepare(text=f"fixture {state}")
            client = Mock()
            client.call_tool.return_value = self.response(state)
            self.assertEqual(submit_transcript_upload(self.path, client)["status"], state)
            self.assertEqual(submit_transcript_upload(self.path, client)["status"], state)
            self.assertEqual(client.call_tool.call_count, 1)

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

    def test_real_http_lost_response_reuses_one_server_job(self):
        accepted = {}
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Fixture sem log de cabeçalhos ou payload.

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Authorization") != "Bearer fixture-only":
                    self.send_error(401)
                    return
                method = body["method"]
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                result = {"protocolVersion": "2025-06-18", "capabilities": {}}
                if method == "tools/call":
                    calls.append(body["params"])
                    request = body["params"]["arguments"]
                    key = request["idempotency_key"]
                    if key not in accepted:
                        accepted[key] = request
                        # Servidor aceitou, mas a conexão cai antes do recibo.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    result = {"content": [{"type": "text", "text": json.dumps({
                        "ok": True, "jobId": 12, "revisionId": 34,
                        "checkpoint": 2, "replay": True, "attempts": 1, "lastError": None,
                        "sourceId": accepted[key]["envelope"]["source_id"], "sourceType": "castanha",
                        "idempotent": True, "status": "completed"})}]}
                encoded = json.dumps({"jsonrpc": "2.0", "id": body["id"],
                                      "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("mcp-session-id", "fixture-session")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
            client = ZinomMcpClient(endpoint, "fixture-only", timeout=2)
            self.assertEqual(submit_transcript_upload(self.path, client)["status"], "error")
            # Nova instância de transporte simula reinício, sem sessão em memória.
            client = ZinomMcpClient(endpoint, "fixture-only", timeout=2)
            checkpoint = self.prepare("2026-09-06T18:00:00-03:00")
            self.assertEqual(submit_transcript_upload(checkpoint, client)["status"], "ok")
            self.assertEqual(len(accepted), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["arguments"],
                             {"idempotency_key": calls[0]["arguments"]["idempotency_key"]})
            self.assertEqual(calls[0]["name"], "brain_ingest")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
