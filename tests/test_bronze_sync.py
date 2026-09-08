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
        self.default_state = "completed"

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
        state = self.states.get(key, self.default_state)
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

    def test_bridge_refusal_reason_reaches_the_panel_in_product_language(self):
        """O painel mostrava só "BronzeIngestError"; o motivo real da ponte é o que ajuda."""
        meta = self.metadata()
        meta["recordings"] = [{"id": "legado.ogg", "filename": "legado.ogg", "path": str(self.bronze / "legado.ogg"),
                               "recorded_at": "2026-09-05T10:00:00", "transcribed": True,
                               "transcription_provider": "groq", "audio_status": "ok"}]
        for p in (self.bronze / ".jobs").glob("*.json"):
            p.unlink()
        write_json(self.bronze / "metadata.json", meta)
        result = sync_meeting("fixture", self.storage)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(result["errors"][0].startswith("Ponte Bronze: "))
        self.assertNotIn("BronzeIngestError", result["errors"][0])
        self.assertNotIn(str(self.bronze), result["errors"][0])
        self.assertEqual(self.client.calls, [])

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

    def test_automatic_drain_delivers_new_origin_after_terminal_origin(self):
        from castanha.retry import drain_queue
        self.assertEqual(sync_meeting('fixture', self.storage)['status'], 'ok')
        key = next(iter(self.client.requests))
        self.client.states[key] = 'tombstoned'
        self.assertEqual(sync_meeting('fixture', self.storage)['status'], 'tombstoned')
        before = len(self.client.calls)
        self.save_job(self.bronze, 'independent-b', 'Gravação B independente')
        metadata = self.metadata()
        metadata['recordings'].append({'id':'b.ogg', 'job_id':'independent-b', 'transcription_provider':'groq'})
        metadata['memory_recording_ids'].append('b.ogg')
        write_json(self.bronze / 'metadata.json', metadata)
        results = drain_queue(self.storage, now=100)
        self.assertEqual(len(results), 1)
        self.assertEqual([r['status'] for r in results[0]['source']['revisions']], ['tombstoned', 'ok'])
        self.assertEqual(len(self.client.calls), before + 1)
        self.assertNotEqual(self.client.calls[-1][1]['idempotency_key'], key)
        self.assertEqual(drain_queue(self.storage, now=1000), [])
        self.assertEqual(len(self.client.calls), before + 1)

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

    def terminal_fixture(self, slug="fixture", *, state="tombstoned", crash=False):
        self.assertEqual(sync_meeting(slug, self.storage)["status"], "ok")
        key = list(self.client.requests)[-1]
        self.client.states[key] = state
        if crash:
            class MetadataCrash(BaseException):
                pass
            with patch.object(self.storage, "record_zinom_result", side_effect=MetadataCrash):
                with self.assertRaises(MetadataCrash):
                    sync_meeting(slug, self.storage)
        else:
            self.assertEqual(sync_meeting(slug, self.storage)["status"], state)
        bronze = self.storage.bronze_dir / slug
        paths = [p for p in (bronze / ".brain-ingest").glob("*.json")
                 if p.name not in ("destination.json", "terminal-evidence.json")]
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_terminal_checkpoint_corruption_blocks_changed_text_even_after_crash(self):
        mutations = {
            "origin": lambda cp: cp["request"]["envelope"].update(source_id="castanha:corrupt"),
            "missing_origin": lambda cp: cp["request"]["envelope"].pop("source_id"),
            "provenance": lambda cp: cp["request"]["envelope"]["proveniencia"].update(origem_id="wrong"),
            "key": lambda cp: cp["request"].update(idempotency_key="wrong"),
            "receipt_origin": lambda cp: cp["result"]["ingestion"].update(source_id="wrong"),
            "remote_identity": lambda cp: cp["remote_identity"].update(job_id=999),
        }
        for crash in (False, True):
            for label, mutate in mutations.items():
                with self.subTest(crash=crash, field=label):
                    slug = f"corrupt-{crash}-{label}"
                    bronze = self.meeting(slug)
                    checkpoint = self.terminal_fixture(slug, crash=crash)
                    saved = json.loads(checkpoint.read_bytes())
                    mutate(saved)
                    write_json(checkpoint, saved)
                    evidence = checkpoint.read_bytes()
                    self.save_job(bronze, "native-" + slug, "Alteração depois da exclusão")
                    calls = len(self.client.calls)
                    names = set((bronze / ".brain-ingest").iterdir())
                    for _ in range(3):
                        self.assertEqual(sync_meeting(slug, self.storage)["status"], "error")
                        self.assertIn(slug, [s for _, s in pending_candidates(self.storage)])
                    self.assertEqual(len(self.client.calls), calls)
                    self.assertEqual(checkpoint.read_bytes(), evidence)
                    self.assertEqual(set((bronze / ".brain-ingest").iterdir()), names)

    def test_independent_terminal_registry_detects_loss_and_conflict_without_metadata(self):
        for damage in ("checkpoint_deleted", "checkpoint_renamed", "registry_deleted",
                       "registry_corrupt", "registry_conflict", "terminal_downgraded"):
            with self.subTest(damage=damage):
                bronze = self.meeting(damage)
                checkpoint = self.terminal_fixture(damage)
                registry = bronze / ".brain-ingest/terminal-evidence.json"
                self.assertTrue(registry.exists())
                meta = json.loads((bronze / "metadata.json").read_bytes())
                meta.pop("zinom")
                write_json(bronze / "metadata.json", meta)
                if damage == "checkpoint_deleted":
                    checkpoint.unlink()
                elif damage == "checkpoint_renamed":
                    checkpoint.rename(checkpoint.with_name("wrong.json"))
                elif damage == "registry_deleted":
                    registry.unlink()
                elif damage == "registry_corrupt":
                    registry.write_text("corrupt")
                elif damage == "registry_conflict":
                    ledger = json.loads(registry.read_bytes())
                    ledger[checkpoint.name]["source_id"] = "wrong"
                    write_json(registry, ledger)
                else:
                    cp = json.loads(checkpoint.read_bytes())
                    cp.update(status="prepared")
                    cp.pop("result")
                    cp.pop("remote_identity")
                    write_json(checkpoint, cp)
                self.save_job(bronze, "native-" + damage, "Texto modificado")
                calls = len(self.client.calls)
                for _ in range(3):
                    self.assertEqual(sync_meeting(damage, self.storage)["status"], "error")
                self.assertEqual(len(self.client.calls), calls)

    def test_changed_terminal_text_creates_no_revision_and_b_stays_sendable(self):
        for state in ("tombstoned", "superseded"):
            with self.subTest(state=state):
                slug = "stable-" + state
                bronze = self.meeting(slug)
                checkpoint = self.terminal_fixture(slug, state=state)
                before = checkpoint.read_bytes()
                registry = bronze / ".brain-ingest/terminal-evidence.json"
                terminal_evidence = registry.read_bytes()
                self.save_job(bronze, "native-" + slug, "A mudou após estado terminal")
                meta = json.loads((bronze / "metadata.json").read_bytes())
                native_b = "b-" + state
                self.save_job(bronze, native_b, "B independente")
                meta["recordings"].append({"id": "b.ogg", "job_id": native_b,
                                            "transcription_provider": "groq"})
                meta["memory_recording_ids"].append("b.ogg")
                write_json(bronze / "metadata.json", meta)
                calls = len(self.client.calls)
                for _ in range(3):
                    result = sync_meeting(slug, self.storage)
                    self.assertEqual([r["status"] for r in result["source"]["revisions"]], [state, "ok"])
                new_calls = self.client.calls[calls:]
                self.assertEqual(sum("envelope" in args for _, args in new_calls), 1)
                self.assertTrue(all(args["idempotency_key"] != json.loads(before)["request"]["idempotency_key"]
                                    for _, args in new_calls))
                self.assertEqual(checkpoint.read_bytes(), before)
                self.assertEqual(registry.read_bytes(), terminal_evidence)
                self.assertNotIn(slug, [s for _, s in pending_candidates(self.storage)])
                checkpoints = [p for p in (bronze / ".brain-ingest").glob("*.json")
                               if p.name not in ("destination.json", "terminal-evidence.json")]
                self.assertEqual(len(checkpoints), 2)

    def test_completed_destination_changes_stay_in_inventory_without_new_identity(self):
        for field, changed in (("endpoint", "http://changed.invalid/mcp"),
                               ("token", "rotated-fixture-only"), ("workspace", "different-workspace"),
                               ("account_id", "different-account")):
            with self.subTest(field=field):
                slug = "destination-" + field
                bronze = self.meeting(slug)
                self.assertEqual(sync_meeting(slug, self.storage)["status"], "ok")
                before = {p.name: p.read_bytes() for p in (bronze / ".brain-ingest").glob("*.json")}
                old = self.config["zinom"].get(field)
                self.config["zinom"][field] = changed
                if field in ("endpoint", "token"):
                    setattr(self.client, field, changed)
                calls = len(self.client.calls)
                for _ in range(3):
                    self.assertIn(slug, [s for _, s in pending_candidates(self.storage)])
                    self.assertEqual(sync_meeting(slug, self.storage)["status"], "error")
                self.assertEqual(len(self.client.calls), calls)
                self.assertEqual({p.name: p.read_bytes() for p in (bronze / ".brain-ingest").glob("*.json")}, before)
                self.config["zinom"][field] = old
                if field in ("endpoint", "token"):
                    setattr(self.client, field, old)
                self.assertEqual(sync_meeting(slug, self.storage)["status"], "ok")
                self.assertEqual(list(self.client.calls[-1][1]), ["idempotency_key"])
                self.assertNotIn(slug, [s for _, s in pending_candidates(self.storage)])

    def test_lost_destination_is_not_refrozen_or_sent_to_legacy(self):
        self.terminal_fixture()
        (self.bronze / ".brain-ingest/destination.json").unlink()
        calls = len(self.client.calls)
        self.config["zinom"]["bronze_ingest_enabled"] = False
        meta = self.metadata()
        meta.pop("zinom")
        write_json(self.bronze / "metadata.json", meta)
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "pending")
        self.config["zinom"]["bronze_ingest_enabled"] = True
        self.assertIn("fixture", [s for _, s in pending_candidates(self.storage)])
        self.assertEqual(sync_meeting("fixture", self.storage)["status"], "error")
        self.assertFalse((self.bronze / ".brain-ingest/destination.json").exists())
        self.assertEqual(len(self.client.calls), calls)

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
