import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from castanha.bronze_ingest import BronzeIngestError
from castanha.durability import file_sha256, write_json
from castanha.legacy_recovery import prepare_legacy_manifest, submit_legacy_recovery, legacy_request
from tests.test_bronze_sync import FakeMcp


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bronze = Path(self.temp.name)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.2", "-c:a", "libopus",
                        str(self.bronze / "audio.ogg")], check=True, capture_output=True)
        write_json(self.bronze / "metadata.json", {"title": "Fixture legada", "slug": "fixture",
            "recorded_at": "2026-09-05T10:30:00", "transcription_provider": "groq",
            "recordings": [{"id": "audio.ogg", "job_id": None}], "audio_status": "ok"})
        (self.bronze / "transcript_raw.txt").write_bytes(("Decisão sintética 🌰\r\n" * 4000).encode())
        self.client = FakeMcp()

    def submit(self, **kwargs):
        return submit_legacy_recovery(self.bronze, self.client, workspace="fixture-workspace", **kwargs)

    def upload_path(self):
        return next(path for path in (self.bronze / ".legacy-recovery/uploads").glob("*.json")
                    if path.name != "terminal-evidence.json")

    def sync(self):
        from castanha.sync import sync_meeting
        from castanha.storage import MeetingStorage
        storage = MeetingStorage(self.bronze / "fixture-storage")
        storage.bronze_dir = self.bronze.parent
        cfg = {"zinom": {"enabled": True, "bronze_ingest_enabled": True,
            "endpoint": self.client.endpoint, "token": self.client.token,
            "workspace": "fixture-workspace"}}
        with patch("castanha.sync.load_config", return_value=cfg), patch(
                "castanha.zinom_adapter.load_config", return_value=cfg), patch(
                "castanha.zinom_adapter.ZinomMcpClient", return_value=self.client):
            return sync_meeting(self.bronze.name, storage)

    def test_sync_resumes_only_explicitly_attempted_recovery_without_changing_metadata(self):
        prepare_legacy_manifest(self.bronze)
        before = (self.bronze / "metadata.json").read_bytes()
        self.client.lose_response = True
        self.submit()
        self.assertEqual(self.sync()["status"], "ok")
        self.assertEqual((self.bronze / "metadata.json").read_bytes(), before)
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(list(self.client.calls[-1][1]), ["idempotency_key"])

    def test_sync_does_not_auto_authorize_prepared_manifest(self):
        prepare_legacy_manifest(self.bronze)
        before = (self.bronze / "metadata.json").read_bytes()
        self.assertEqual(self.sync()["status"], "pending")
        self.assertEqual(self.client.calls, [])
        self.assertEqual((self.bronze / "metadata.json").read_bytes(), before)

    def test_sync_does_not_replace_lost_manifest_or_destination(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        (self.bronze / ".legacy-recovery/manifest.json").unlink()
        before = (self.bronze / "metadata.json").read_bytes()
        calls = len(self.client.calls)
        self.assertEqual(self.sync()["status"], "error")
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual((self.bronze / "metadata.json").read_bytes(), before)

    def test_retry_inventory_tracks_attempts_and_completion(self):
        from castanha.legacy_recovery import legacy_recovery_pending
        prepare_legacy_manifest(self.bronze)
        self.assertFalse(legacy_recovery_pending(self.bronze))
        self.client.lose_response = True
        self.submit()
        self.assertTrue(legacy_recovery_pending(self.bronze))
        self.sync()
        self.assertFalse(legacy_recovery_pending(self.bronze))

    def test_resume_respects_disabled_gate(self):
        from castanha.legacy_recovery import resume_legacy_recovery
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        calls = len(self.client.calls)
        self.assertEqual(resume_legacy_recovery(self.bronze, {})["status"], "pending")
        self.assertEqual(len(self.client.calls), calls)

    def test_sync_destination_change_never_sends(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        calls = len(self.client.calls)
        self.client.token = "changed-fixture-only"
        self.assertEqual(self.sync()["status"], "error")
        self.assertEqual(len(self.client.calls), calls)

    def test_inventory_missing_checkpoint_remains_pending_and_sync_fails_closed(self):
        from castanha.legacy_recovery import legacy_recovery_pending
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        self.upload_path().unlink()
        self.assertTrue(legacy_recovery_pending(self.bronze))
        self.assertEqual(self.sync()["status"], "error")
        self.assertEqual(len(self.client.calls), 1)

    def test_corrupt_recovery_does_not_block_independent_queue_entries(self):
        import shutil
        from castanha.storage import MeetingStorage
        from castanha.sync import pending_candidates
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        write_json(self.upload_path(), {})
        with tempfile.TemporaryDirectory() as root:
            storage = MeetingStorage(Path(root))
            shutil.copytree(self.bronze, storage.bronze_dir / "legacy")
            other = storage.bronze_dir / "independent"
            other.mkdir()
            write_json(other / "metadata.json", {"zinom": {"status": "pending"}})
            with patch("castanha.sync.load_config", return_value={}), patch(
                    "castanha.sync.reconcile_finished_capture", return_value=None):
                self.assertEqual({slug for _, slug in pending_candidates(storage)},
                                 {"legacy", "independent"})

    def test_manifest_keeps_originals_and_native_id_unchanged(self):
        paths = [self.bronze / p for p in ("audio.ogg", "transcript_raw.txt", "metadata.json")]
        before = [file_sha256(p) for p in paths]
        manifest = prepare_legacy_manifest(self.bronze)
        self.assertEqual(prepare_legacy_manifest(self.bronze), manifest)
        self.assertEqual([file_sha256(p) for p in paths], before)
        self.assertIsNone(manifest["source"]["historical_job_id"])
        self.assertEqual(manifest["source"]["legacy_provider_global_unverified"], "groq")
        self.assertEqual(manifest["source"]["audio"]["mime"], "audio/ogg")
        self.assertEqual(self.client.calls, [])

    def test_concurrent_prepare_creates_only_one_identity(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: prepare_legacy_manifest(self.bronze), range(2)))
        self.assertEqual(results[0]["migration_id"], results[1]["migration_id"])

    def test_integral_projection_and_audio_lineage(self):
        manifest = prepare_legacy_manifest(self.bronze)
        self.assertEqual(self.submit()["status"], "ok")
        request = next(iter(self.client.requests.values()))
        e = request["envelope"]
        self.assertEqual(e["texto"].encode(), (self.bronze / "transcript_raw.txt").read_bytes())
        self.assertEqual(e["source_id"], "castanha:" + hashlib.sha256(
            ("migration:" + manifest["migration_id"]).encode()).hexdigest())
        self.assertEqual(e["proveniencia"]["origem_id"], e["source_id"])
        self.assertIn(manifest["source"]["audio"]["sha256"], e["proveniencia"]["referencia"])
        self.assertEqual(e["fidelidade"], "projecao")
        self.assertEqual(e["timestamp"]["valor"], "2026-09-05")
        self.assertEqual(request["facts"], [])

    def test_lost_ack_uses_key_only(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.assertEqual(self.submit()["status"], "error")
        self.assertEqual(self.submit()["status"], "ok")
        self.assertEqual(list(self.client.calls[1][1]), ["idempotency_key"])
        self.assertEqual(len(self.client.requests), 1)

    def test_terminal_never_republishes(self):
        prepare_legacy_manifest(self.bronze)
        self.submit()
        key = next(iter(self.client.requests))
        self.client.states[key] = "tombstoned"
        self.assertEqual(self.submit()["status"], "tombstoned")
        calls = len(self.client.calls)
        self.assertEqual(self.submit()["status"], "tombstoned")
        self.assertEqual(len(self.client.calls), calls)

    def test_changed_audio_blocks_all_network(self):
        prepare_legacy_manifest(self.bronze)
        with (self.bronze / "audio.ogg").open("ab") as out:
            out.write(b"changed")
        with self.assertRaises(BronzeIngestError):
            self.submit()
        self.assertEqual(self.client.calls, [])

    def test_changed_text_blocks_all_network(self):
        prepare_legacy_manifest(self.bronze)
        (self.bronze / "transcript_raw.txt").write_text("Changed")
        with self.assertRaises(BronzeIngestError):
            self.submit()
        self.assertEqual(self.client.calls, [])

    def test_no_workspace_no_network(self):
        prepare_legacy_manifest(self.bronze)
        with self.assertRaises(BronzeIngestError):
            submit_legacy_recovery(self.bronze, self.client, workspace=None)
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.bronze / ".legacy-recovery/uploads").exists())

    def test_changed_destination_blocks_retry(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        with self.assertRaises(BronzeIngestError):
            submit_legacy_recovery(self.bronze, self.client, workspace="different")
        self.assertEqual(len(self.client.calls), 1)

    def test_no_manifest_no_send(self):
        with self.assertRaises(FileNotFoundError):
            self.submit()
        self.assertEqual(self.client.calls, [])

    def test_manifest_loss_after_attempt_never_creates_new_identity(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        (self.bronze / ".legacy-recovery/manifest.json").unlink()
        with self.assertRaises(BronzeIngestError):
            prepare_legacy_manifest(self.bronze)
        self.assertEqual(len(self.client.calls), 1)

    def test_destination_loss_never_redirects_lost_ack(self):
        prepare_legacy_manifest(self.bronze)
        self.client.lose_response = True
        self.submit()
        (self.bronze / ".legacy-recovery/destination.json").unlink()
        replacement = FakeMcp()
        replacement.endpoint = "http://another-fixture.invalid/mcp"
        replacement.token = "different-fixture"
        with self.assertRaises(BronzeIngestError):
            submit_legacy_recovery(self.bronze, replacement, workspace="fixture-workspace")
        self.assertEqual(replacement.calls, [])

    def test_manifest_identity_change_and_checkpoint_loss_cannot_republish(self):
        prepare_legacy_manifest(self.bronze)
        self.submit()
        path = self.bronze / ".legacy-recovery/manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["migration_id"] = "00000000-0000-4000-8000-000000000001"
        write_json(path, manifest)
        self.upload_path().unlink()
        with self.assertRaises(BronzeIngestError):
            self.submit()
        self.assertEqual(len(self.client.calls), 1)

    def test_lost_terminal_checkpoint_never_sends_payload_again(self):
        prepare_legacy_manifest(self.bronze)
        self.submit()
        self.client.states[next(iter(self.client.requests))] = "tombstoned"
        self.assertEqual(self.submit()["status"], "tombstoned")
        self.upload_path().unlink()
        calls = len(self.client.calls)
        for _ in range(2):
            with self.assertRaises(BronzeIngestError):
                self.submit()
        self.assertEqual(len(self.client.calls), calls)

    def test_terminal_ledger_loss_blocks_without_republishing(self):
        prepare_legacy_manifest(self.bronze)
        self.submit()
        self.client.states[next(iter(self.client.requests))] = "tombstoned"
        self.assertEqual(self.submit()["status"], "tombstoned")
        ledger = self.bronze / ".legacy-recovery/uploads/terminal-evidence.json"
        self.assertTrue(ledger.exists())
        ledger.unlink()
        calls = len(self.client.calls)
        with self.assertRaises(BronzeIngestError):
            self.submit()
        self.assertEqual(len(self.client.calls), calls)

    def test_terminal_origin_corruption_blocks_before_network(self):
        prepare_legacy_manifest(self.bronze)
        self.submit()
        self.client.states[next(iter(self.client.requests))] = "tombstoned"
        self.submit()
        path = self.upload_path()
        saved = json.loads(path.read_bytes())
        saved["request"]["envelope"]["source_id"] = "castanha:" + "0" * 64
        write_json(path, saved)
        calls = len(self.client.calls)
        with self.assertRaises(BronzeIngestError):
            self.submit()
        self.assertEqual(len(self.client.calls), calls)

    def test_mock_multiple_recordings_and_existing_delivery_rejected(self):
        path = self.bronze / "metadata.json"
        original = json.loads(path.read_bytes())
        for delta in ({"transcription_provider": "mock"},
                      {"recordings": [{"id": "audio.ogg"}, {"id": "second.ogg"}]},
                      {"zinom": {"remember_id": "existing"}},
                      {"zinom": {"status": "tombstoned"}}):
            write_json(path, {**original, **delta})
            with self.assertRaises(BronzeIngestError):
                prepare_legacy_manifest(self.bronze)
        self.assertEqual(self.client.calls, [])

    def test_invalid_audio_is_not_accepted_by_extension(self):
        (self.bronze / "audio.ogg").write_text("not audio")
        with self.assertRaises(subprocess.CalledProcessError):
            prepare_legacy_manifest(self.bronze)


if __name__ == "__main__":
    unittest.main()
