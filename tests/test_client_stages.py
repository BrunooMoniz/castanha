"""Real CLI projections and shared QML logic, with invented data and no capture."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ClientStagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.meetings = self.root / "meetings"
        self.bronze = self.meetings / "bronze" / "fixture"
        self.bronze.mkdir(parents=True)
        self.state = self.root / "state/castanha/state.json"
        self.state.parent.mkdir(parents=True)
        self.config = self.root / "config/castanha/config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"storage": {"base_dir": str(self.meetings),
            **{layer + "_dir": str(self.meetings / layer) for layer in ("bronze", "silver", "gold")}},
            "zinom": {"enabled": False, "token": ""}, "calendar": {"feeds": []}}))
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_STATE_HOME": str(self.root / "state"), "CASTANHA_LANG": "pt"}
        self.metadata = {"slug": "fixture", "title": "Reunião simulada", "audio_status": "ok",
            "transcription_provider": "real-fixture", "transcription_pending": False,
            "summary_status": "pending", "summary_error": "Cota diária esgotada; aguardando renovação",
            "processing_status": "pending", "zinom": {"status": "pending"},
            "recordings": [{"filename": "audio.ogg", "transcribed": True,
                            "transcription_provider": "real-fixture"}]}
        (self.bronze / "metadata.json").write_text(json.dumps(self.metadata))
        (self.bronze / "audio.ogg").write_bytes(b"synthetic audio, never capture")
        (self.bronze / "transcript_raw.txt").write_text("Texto estritamente simulado.")
        self.state.write_text(json.dumps({"status": "idle", "last_result": {
            "slug": "fixture", "transcription_pending": True, "zinom": {"status": "error"}}}))

    def cli(self, *args, bootstrap=None):
        command = [sys.executable, str(ROOT / "bin/castanha"), *args]
        if bootstrap:
            command = [sys.executable, "-c", bootstrap, *command[1:]]
        result = subprocess.run(command, env=self.env, cwd=ROOT, capture_output=True,
                                text=True, timeout=20)
        return result, json.loads(result.stdout)

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.bronze.iterdir() if p.is_file()}

    def test_notes_list_detail_and_status_agree_without_changing_evidence(self):
        before, state = self.hashes(), self.state.read_bytes()
        for args in (("notes", "--json"), ("notes", "fixture", "--json"), ("status", "--json")):
            result, payload = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            note = payload.get("last_result") or (payload["notes"][0] if "notes" in payload else payload)
            self.assertFalse(note["transcription_pending"])
            self.assertEqual(note["transcription_status"], "complete")
            self.assertEqual(note["summary_status"], "pending")
            self.assertEqual(note["zinom"]["status"], "pending")
            self.assertTrue(note["can_retry"])
            self.assertEqual(note["retry_stage"], "summary")
        self.assertEqual(before, self.hashes())
        self.assertEqual(state, self.state.read_bytes())

    def test_partial_transcript_does_not_hide_untranscribed_recording(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["recordings"].append({"filename": "missing.ogg", "transcribed": False})
        (self.bronze / "metadata.json").write_text(json.dumps(metadata))
        result, note = self.cli("notes", "fixture", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(note["transcription_status"], "pending")
        self.assertEqual(note["retry_stage"], "transcription")

    def test_summary_retry_never_invokes_asr_or_delivery_when_summary_unavailable(self):
        before = {name: (self.bronze / name).read_bytes()
                  for name in ("audio.ogg", "transcript_raw.txt")}
        bootstrap = '''
import runpy, sys
from unittest.mock import patch
from castanha.summarizer import LlmUnavailable
sys.argv = sys.argv[1:]
with patch("castanha.engine.get_transcriber", side_effect=AssertionError("ASR forbidden")), \
     patch("castanha.engine.transcribe_dual", side_effect=AssertionError("ASR forbidden")), \
     patch("castanha.engine.notify"), \
     patch("castanha.summarizer.MeetingSummarizer.generate_silver", side_effect=LlmUnavailable("Cota diária esgotada")) as summary, \
     patch("castanha.zinom_adapter.ZinomAdapter.ingest_meeting", side_effect=AssertionError("Delivery forbidden")):
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    finally:
        summary.assert_called_once()
'''
        result, payload = self.cli("retry", "fixture", "--json", bootstrap=bootstrap)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["results"][0]["status"], "partial")
        self.assertEqual(payload["results"][0]["result"]["summary_status"], "pending")
        for name, content in before.items():
            self.assertEqual((self.bronze / name).read_bytes(), content)

    def test_agenda_failure_preserves_previous_then_online_refresh_clears_warning(self):
        previous = {"uid": "fixture", "title": "Evento simulado", "all_day": False}
        self.state.write_text(json.dumps({"status": "recording", "capture_slug": "untouched",
            "next_meeting": previous, "upcoming_meetings": [previous]}))
        bootstrap = '''
import runpy, sys
from unittest.mock import patch
sys.argv = sys.argv[1:]
with patch("castanha.agenda.collect_upcoming", return_value=[]), \
     patch("castanha.agenda.agenda_warning", return_value="Agenda indisponível: sem conexão com o Zinom"):
    runpy.run_path(sys.argv[0], run_name="__main__")
'''
        result, payload = self.cli("agenda", "refresh", "--json", bootstrap=bootstrap)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(payload["status"], "error")
        state = json.loads(self.state.read_bytes())
        self.assertEqual(state["next_meeting"], previous)
        self.assertEqual(state["upcoming_meetings"], [previous])
        self.assertEqual(state["capture_slug"], "untouched")
        bootstrap = bootstrap.replace('return_value="Agenda indisponível: sem conexão com o Zinom"', 'return_value=None')
        result, payload = self.cli("agenda", "refresh", "--json", bootstrap=bootstrap)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "ok")
        state = json.loads(self.state.read_bytes())
        self.assertEqual(state["upcoming_meetings"], [])
        self.assertIsNone(state["agenda_error"])
        self.assertEqual(state["status"], "recording")

    def test_shared_qml_functions_in_javascript_runtime(self):
        result = subprocess.run(["node", str(ROOT / "tests/client_stages.js")],
                                capture_output=True, text=True, timeout=10, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
