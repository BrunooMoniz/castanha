"""Retomada automática isolada, sem credenciais, rede ou áudio pessoal."""
import json
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha.storage import MeetingStorage
from castanha.sync import sync_pending


class TestRetryQueue(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = MeetingStorage(base_dir=Path(self.temp.name))

    def meeting(self, slug, **metadata):
        directory = self.storage.bronze_dir / slug
        directory.mkdir()
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return directory

    def test_manual_queue_continues_after_one_corrupt_meeting(self):
        self.meeting("a")
        self.meeting("b")
        with patch("castanha.sync.sync_meeting", side_effect=[ValueError("fixture"), {"status": "ok"}]):
            result = sync_pending(storage=self.storage)
        self.assertEqual([r["status"] for r in result], ["error", "ok"])

    def test_retry_deadline_survives_another_process(self):
        from castanha.retry import drain_queue
        self.meeting("a")
        with patch("castanha.retry.sync_meeting", return_value={"status": "error"}) as sync:
            drain_queue(self.storage, now=100)
            drain_queue(self.storage, now=101)
            self.assertEqual(sync.call_count, 1)
            drain_queue(self.storage, now=161)
            self.assertEqual(sync.call_count, 2)

    def test_crash_keeps_retry_lease_on_disk(self):
        from castanha.retry import drain_queue
        self.meeting("a")
        with patch("castanha.retry.sync_meeting", side_effect=SystemExit("simulated crash")):
            with self.assertRaises(SystemExit):
                drain_queue(self.storage, now=100)
        with patch("castanha.retry.sync_meeting", return_value={"status": "ok"}) as sync:
            drain_queue(self.storage, now=101)
            sync.assert_not_called()
            drain_queue(self.storage, now=161)
            sync.assert_called_once()

    def test_bad_meeting_does_not_starve_later_meetings(self):
        from castanha.retry import drain_queue
        self.meeting("a")
        self.meeting("b")
        with patch("castanha.retry.sync_meeting", side_effect=ValueError("fixture")) as sync:
            drain_queue(self.storage, now=100, limit=1)
            drain_queue(self.storage, now=101, limit=1)
        self.assertEqual([c.args[0] for c in sync.call_args_list], ["a", "b"])

    def test_tombstone_wins_over_unfinished_job(self):
        from castanha.retry import drain_queue
        directory = self.meeting("a", zinom={"status": "tombstoned"})
        (directory / ".jobs").mkdir()
        (directory / ".jobs" / "job.json").write_text('{"stage":"pending"}')
        with patch("castanha.retry.sync_meeting") as sync:
            drain_queue(self.storage, now=100)
            sync.assert_not_called()

    def test_facts_waiting_for_server_do_not_republish_delivered_note(self):
        from castanha.retry import drain_queue
        self.meeting("a", zinom={"status": "pending", "remember_id": "conversation:fixture",
                                 "facts_status": "pending_lineage"})
        with patch("castanha.retry.sync_meeting") as sync:
            drain_queue(self.storage, now=100)
            sync.assert_not_called()

    def test_corrupt_retry_receipt_preserves_content_and_other_work(self):
        from castanha.retry import drain_queue
        directory = self.meeting("a")
        receipt = directory / ".sync-retry.json"
        receipt.write_text("not json", encoding="utf-8")
        self.meeting("b")
        with patch("castanha.retry.sync_meeting", return_value={"status": "ok"}) as sync:
            result = drain_queue(self.storage, now=100)
        self.assertEqual(receipt.read_text(), "not json")
        self.assertEqual([c.args[0] for c in sync.call_args_list], ["b"])
        self.assertEqual(result[0]["status"], "error")

    def test_scheduler_does_not_wait_or_launch_overlapping_workers(self):
        from castanha.retry import RetryScheduler
        child = Mock()
        child.poll.return_value = None
        with patch("castanha.retry.subprocess.Popen", return_value=child) as launch:
            scheduler = RetryScheduler()
            scheduler.tick(now=100, capture_status="recording")
            launch.assert_not_called()
            scheduler.tick(now=100, capture_status="idle")
            scheduler.tick(now=200, capture_status="idle")
            launch.assert_called_once()
            child.wait.assert_not_called()
            child.poll.return_value = 0
            scheduler.tick(now=201, capture_status="idle")
            self.assertEqual(launch.call_count, 2)

    def test_second_worker_cannot_drain_while_first_holds_lock(self):
        from castanha.retry import drain_queue
        self.meeting("a")
        with (self.storage.bronze_dir / ".sync-run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("castanha.retry.sync_meeting") as sync:
                self.assertEqual(drain_queue(self.storage, now=100), [])
                sync.assert_not_called()

    def test_bad_metadata_does_not_prevent_retry_of_other_meeting(self):
        from castanha.retry import drain_queue
        directory = self.meeting("a")
        (directory / "metadata.json").write_text("[]")
        self.meeting("b")
        with patch("castanha.retry.sync_meeting", return_value={"status": "ok"}) as sync:
            result = drain_queue(self.storage, now=100)
        self.assertEqual([c.args[0] for c in sync.call_args_list], ["b"])
        self.assertEqual(result[0]["status"], "error")

    def test_disabled_scheduler_never_launches_a_process(self):
        from castanha.retry import RetryScheduler
        with patch("castanha.retry.subprocess.Popen") as launch:
            RetryScheduler(enabled=False).tick(now=100)
            launch.assert_not_called()

    def test_dead_finalizer_does_not_block_automatic_recovery(self):
        from castanha.retry import RetryScheduler
        with patch("castanha.retry.subprocess.Popen") as launch, \
             patch("castanha.retry.os.kill", side_effect=ProcessLookupError):
            RetryScheduler().tick(now=100, capture_status="processing", processing_pid=99999)
            launch.assert_called_once()

    def test_live_or_unknown_finalizer_is_not_interrupted(self):
        from castanha.retry import RetryScheduler
        with patch("castanha.retry.subprocess.Popen") as launch, patch("castanha.retry.os.kill"):
            RetryScheduler().tick(now=100, capture_status="processing", processing_pid=99999)
            RetryScheduler().tick(now=100, capture_status="processing")
            launch.assert_not_called()

    def test_manual_sync_never_publishes_invalid_metadata(self):
        from castanha.sync import sync_meeting
        directory = self.meeting("a")
        (directory / "metadata.json").write_text("[]")
        with patch("castanha.sync.ZinomAdapter") as adapter:
            self.assertEqual(sync_meeting("a", self.storage)["status"], "error")
            adapter.assert_not_called()
        self.assertEqual((directory / "metadata.json").read_text(), "[]")


if __name__ == "__main__":
    unittest.main()
