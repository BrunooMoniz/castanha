"""Caller real, disco temporário e MCP sintético; nenhum dado ou serviço pessoal."""
import concurrent.futures
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from castanha.durability import write_json
from castanha.storage import MeetingStorage
from castanha.sync import pending_candidates, sync_meeting, sync_pending
from castanha.zinom_adapter import ZinomAdapter, ZinomError, ZinomMcpClient, ZinomSessionError


class FakeMcp:
    endpoint = "http://fixture.invalid/mcp"
    token = "fixture-only"

    def __init__(self):
        self.requests = {}
        self.calls = []
        self.states = {}
        self.lose_response = False
        self.bronze = None

    def connect(self):
        pass

    def call_tool(self, name, arguments):
        assert name == "brain_ingest", name
        if self.bronze:
            with (self.bronze / ".processing.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("Reunião sem lock durante a rede")
        self.calls.append((name, arguments))
        key = arguments["idempotency_key"]
        if "envelope" in arguments:
            self.requests.setdefault(key, arguments)
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("fixture")
        if key not in self.requests:
            raise ZinomError("fixture", code="unknown_idempotency_key", tool=name)
        request = self.requests[key]
        state = self.states.get(key, "completed")
        job_id = list(self.requests).index(key) + 1
        return {"content": [{"type": "text", "text": json.dumps({
            "ok": True, "jobId": job_id, "revisionId": job_id,
            "checkpoint": 2 if state == "completed" else 0, "status": state,
            "replay": "envelope" not in arguments, "attempts": 1, "lastError": None,
            "sourceType": "castanha", "sourceId": request["envelope"]["source_id"],
        })}]}


class TestBronzeCaller(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = MeetingStorage(base_dir=Path(self.temp.name))
        self.config = {"zinom": {"enabled": True, "bronze_ingest_enabled": True,
                                  "workspace": "fixture-workspace", "token": "fixture-only",
                                  "endpoint": FakeMcp.endpoint},
                       "storage": {"bronze_dir": str(self.storage.bronze_dir)}}
        for target in ("castanha.sync.load_config", "castanha.zinom_adapter.load_config"):
            patcher = patch(target, side_effect=lambda: self.config)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeMcp()
        patcher = patch("castanha.zinom_adapter.ZinomMcpClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bronze = self.meeting("fixture")

    def meeting(self, slug):
        bronze = self.storage.bronze_dir / slug
        (bronze / ".jobs").mkdir(parents=True)
        self.save_job(bronze, f"native-{slug}", "Decisão com ação 🌰\r\n" * 4000)
        write_json(bronze / "metadata.json", {
            "slug": slug, "title": "Título sintético", "audio_status": "ok",
            "processing_status": "complete", "transcription_provider": "groq",
            "memory_recording_ids": ["capture.ogg"],
            "recordings": [{"id": "capture.ogg", "job_id": f"native-{slug}",
                            "transcription_provider": "groq"}],
        })
        return bronze

    def save_job(self, bronze, native_id, text):
        write_json(bronze / ".jobs" / f"{native_id}.json", {
            "id": native_id, "stage": "done", "transcript": text,
            "provider": "groq", "recorded_at": "2026-09-05T10:30:00-03:00",
        })

    def metadata(self):
        return json.loads((self.bronze / "metadata.json").read_bytes())

    def test_integral_job_not_silver_with_durable_receipt_and_meeting_lock(self):
        self.client.bronze = self.bronze
        (self.storage.silver_dir / "fixture.md").write_text("resumo não é original")
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "ok")
        request = next(iter(self.client.requests.values()))
        job = json.loads((self.bronze / ".jobs/native-fixture.json").read_bytes())
        self.assertEqual(request["envelope"]["texto"], job["transcript"])
        self.assertEqual(request["facts"], [])
        source = self.metadata()["zinom"]["source"]
        self.assertEqual(source["transport"], "bronze")
        self.assertEqual(source["revisions"][0]["ingestion"]["state"], "completed")
        self.assertEqual(pending_candidates(self.storage), [])

    def test_lost_response_and_restart_only_lookup(self):
        self.client.lose_response = True
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(sync_pending(storage=self.storage)[0]["status"], "ok")
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(list(self.client.calls[1][1]), ["idempotency_key"])

    def test_current_revision_must_complete_even_when_old_revision_did(self):
        sync_meeting("fixture", self.storage)
        old = next(iter(self.client.requests.values()))
        self.save_job(self.bronze, "native-fixture", "Correção integral\r\n🌰")
        self.assertEqual([s for _, s in pending_candidates(self.storage)], ["fixture"])
        # O servidor mantém a revisão antiga completed; isso não conclui a nova.
        original_call = self.client.call_tool
        def pending(name, request):
            if "envelope" in request:
                self.client.states[request["idempotency_key"]] = "pending"
            return original_call(name, request)
        with patch.object(self.client, "call_tool", side_effect=pending):
            self.assertEqual(sync_pending(storage=self.storage)[0]["status"], "pending")
        new = list(self.client.requests.values())[1]
        self.assertNotEqual(old["idempotency_key"], new["idempotency_key"])
        self.assertEqual(old["envelope"]["source_id"], new["envelope"]["source_id"])
        self.assertEqual([s for _, s in pending_candidates(self.storage)], ["fixture"])

    def test_native_origin_survives_title_and_slug_changes(self):
        sync_meeting("fixture", self.storage)
        old = next(iter(self.client.requests.values()))
        meta = self.metadata()
        meta.update(slug="renamed", title="Outro título")
        moved = self.bronze.with_name("renamed")
        self.bronze.rename(moved)
        write_json(moved / "metadata.json", meta)
        self.assertEqual(sync_meeting("renamed", self.storage)["status"], "ok")
        new = list(self.client.requests.values())[1]
        self.assertEqual(old["envelope"]["source_id"], new["envelope"]["source_id"])

    def test_missing_workspace_is_recoverable_and_never_inferred(self):
        del self.config["zinom"]["workspace"]
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(self.client.calls, [])
        self.config["zinom"]["workspace"] = "authorized-fixture"
        self.assertEqual(sync_pending(storage=self.storage)[0]["status"], "ok")

    def test_changed_destination_after_lost_response_never_uploads(self):
        self.client.lose_response = True
        sync_meeting("fixture", self.storage)
        self.config["zinom"]["workspace"] = "different-workspace"
        self.assertEqual(sync_pending(storage=self.storage)[0]["status"], "error")
        self.assertEqual(len(self.client.calls), 1)

    def test_changed_endpoint_or_credential_cannot_redirect_a_lost_response(self):
        self.client.lose_response = True
        sync_meeting("fixture", self.storage)
        self.client.endpoint = "http://another-fixture.invalid/mcp"
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.client.endpoint = FakeMcp.endpoint
        self.client.token = "another-fixture-only"
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(len(self.client.calls), 1)

    def test_multiple_native_recordings_keep_distinct_origins(self):
        meta = self.metadata()
        self.save_job(self.bronze, "native-second", "Outra gravação integral\r\n")
        meta["recordings"].append({"id": "second.ogg", "job_id": "native-second",
                                   "transcription_provider": "groq"})
        meta["memory_recording_ids"].append("second.ogg")
        write_json(self.bronze / "metadata.json", meta)
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "ok")
        requests = list(self.client.requests.values())
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0]["envelope"]["source_id"], requests[1]["envelope"]["source_id"])
        self.assertEqual(pending_candidates(self.storage), [])

    def test_local_tombstone_survives_metadata_crash_and_changed_text(self):
        sync_meeting("fixture", self.storage)
        key = next(iter(self.client.requests))
        self.client.states[key] = "tombstoned"
        sync_meeting("fixture", self.storage)
        meta = self.metadata()
        meta.pop("zinom")
        write_json(self.bronze / "metadata.json", meta)
        self.save_job(self.bronze, "native-fixture", "Correção após exclusão")
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "tombstoned")
        self.assertEqual(len(self.client.calls), 2)

    def test_disabled_after_crash_never_falls_back_to_remember(self):
        self.client.lose_response = True
        sync_meeting("fixture", self.storage)
        meta = self.metadata()
        meta.pop("zinom")  # Simula morte antes de storage.record_zinom_result.
        write_json(self.bronze / "metadata.json", meta)
        self.config["zinom"]["bronze_ingest_enabled"] = False
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "pending")
        self.assertEqual(len(self.client.calls), 1)

    def test_failed_item_does_not_block_queue(self):
        (self.bronze / ".jobs/native-fixture.json").write_text("corrupt")
        self.meeting("next")
        results = sync_pending(storage=self.storage)
        self.assertEqual([r["status"] for r in results], ["error", "ok"])

    def test_terminal_states_never_publish_again(self):
        for state in ("tombstoned", "superseded"):
            with self.subTest(state=state):
                bronze = self.meeting(state)
                meta = json.loads((bronze / "metadata.json").read_bytes())
                meta["zinom"] = {"status": state, "source": {"transport": "bronze"}}
                write_json(bronze / "metadata.json", meta)
                self.assertEqual(sync_meeting(state, self.storage)["status"], state)
        self.assertEqual(self.client.calls, [])

    def test_terminal_origin_does_not_block_new_independent_recording(self):
        for state in ("tombstoned", "superseded"):
            with self.subTest(state=state):
                bronze = self.meeting("origin-" + state)
                slug = bronze.name
                self.assertEqual(sync_meeting(slug, self.storage)["status"], "ok")
                key = list(self.client.requests)[-1]
                self.client.states[key] = state
                self.assertEqual(sync_meeting(slug, self.storage)["status"], state)
                calls_before = len(self.client.calls)
                meta = json.loads((bronze / "metadata.json").read_bytes())
                native_id = "new-" + state
                self.save_job(bronze, native_id, "Nova gravação independente")
                meta["recordings"].append({"id": "new.ogg", "job_id": native_id,
                                            "transcription_provider": "groq"})
                meta["memory_recording_ids"].append("new.ogg")
                write_json(bronze / "metadata.json", meta)
                self.assertIn(slug, [s for _, s in pending_candidates(self.storage)])
                result = sync_meeting(slug, self.storage)
                self.assertEqual([r["status"] for r in result["source"]["revisions"]], [state, "ok"])
                self.assertEqual(len(self.client.calls), calls_before + 1)
                self.assertNotIn(slug, [s for _, s in pending_candidates(self.storage)])
                # Recriar o chamador não reenvia a origem terminal.
                sync_meeting(slug, MeetingStorage(base_dir=Path(self.temp.name)))
                self.assertTrue(all(c[1]["idempotency_key"] != key
                                    for c in self.client.calls[calls_before:]))

    def test_missing_terminal_evidence_blocks_upload_without_resurrection(self):
        sync_meeting("fixture", self.storage)
        key = next(iter(self.client.requests))
        self.client.states[key] = "tombstoned"
        sync_meeting("fixture", self.storage)
        receipt = self.metadata()["zinom"]["source"]["revisions"][0]
        (self.bronze / ".brain-ingest" / receipt["checkpoint"]).unlink()
        calls = len(self.client.calls)
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(len(self.client.calls), calls)

    def test_changed_deleted_text_settles_without_uploading_again(self):
        sync_meeting("fixture", self.storage)
        key = next(iter(self.client.requests))
        self.client.states[key] = "tombstoned"
        sync_meeting("fixture", self.storage)
        self.save_job(self.bronze, "native-fixture", "Texto alterado depois da exclusão")
        calls = len(self.client.calls)
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "tombstoned")
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(pending_candidates(self.storage), [])

    def test_concurrent_sync_freezes_one_request(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: sync_meeting("fixture", self.storage), range(2)))
        self.assertEqual([r["status"] for r in results], ["ok", "ok"])
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(sum("envelope" in args for _, args in self.client.calls), 1)

    def test_adapter_default_is_off_and_flag_requires_boolean(self):
        for value in (None, False, "true", 1):
            self.config["zinom"]["bronze_ingest_enabled"] = value
            self.assertFalse(ZinomAdapter().bronze_enabled)

    def test_job_without_native_identity_is_not_guessed(self):
        meta = self.metadata()
        del meta["recordings"][0]["job_id"]
        write_json(self.bronze / "metadata.json", meta)
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertEqual(self.client.calls, [])

    def test_ingest_transport_session_error_does_not_retry_payload(self):
        # Constrói a classe real, independente do patch do factory acima.
        client = ZinomMcpClient("http://fixture.invalid", "fixture-only")
        with patch.object(client, "_rpc", side_effect=ZinomSessionError("session lost")) as rpc, \
             patch.object(client, "connect") as connect:
            with self.assertRaises(ZinomSessionError):
                client.call_tool("brain_ingest", {"idempotency_key": "fixture", "envelope": {}})
            self.assertEqual(rpc.call_count, 1)
            connect.assert_not_called()
