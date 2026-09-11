"""Fluxo autoral em diretórios reais isolados, sem ASR, áudio ou conta externa."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha.annotations import (AnnotationConflict, AnnotationError, CHECKPOINT, MAX_BYTES,
    MANUAL_GUIDANCE, create_annotations, get_annotations, save_annotations,
    regenerate_annotations, regeneration_pending, synthesis_metadata)
from castanha.storage import MeetingStorage
from castanha.summarizer import MeetingSummarizer, LlmUnavailable, LlmTooLarge
from castanha.sync import pending_candidates, sync_meeting

GOLD = {"facts": [], "decisions": [], "action_items": [], "people_notes": []}
TEXT = "Segundo minhas anotações, precisamos revisar o orçamento de setembro."


class TestAnnotations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = self.root / "meetings"
        config = self.root / "config/castanha"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({
            "storage": {"base_dir": str(base), "bronze_dir": str(base / "bronze"),
                        "silver_dir": str(base / "silver"), "gold_dir": str(base / "gold")},
            "llm": {"provider": "groq", "api_key": ""},
            "zinom": {"enabled": False, "token": "", "bronze_ingest_enabled": False},
            "calendar": {"enabled": False},
        }))
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"),
                                     "XDG_STATE_HOME": str(self.root / "state")})
        env.start(); self.addCleanup(env.stop)
        network = patch("urllib.request.urlopen", side_effect=AssertionError("No real network"))
        network.start(); self.addCleanup(network.stop)
        self.storage = MeetingStorage(base)
        self.slug = create_annotations("Planejamento com café", self.storage)["slug"]
        self.bronze = self.storage.bronze_dir / self.slug
        self.summarizer = Mock()
        self.summarizer._requires_summary.return_value = True
        self.summarizer.last_provider = "fixture"
        self.summarizer.generate_silver.return_value = "# Resumo das anotações manuais\n" + TEXT
        self.summarizer.generate_gold.return_value = GOLD
        self.adapter = Mock()
        self.adapter.ingest_meeting.return_value = {"status": "ok", "remember": {"id": "fixture"}, "note_status": "ok"}

    def save(self, text=TEXT):
        return save_annotations(self.slug, text, self.storage)

    def run_summary(self, **kwargs):
        return regenerate_annotations(self.slug, self.storage, summarizer=self.summarizer,
                                      adapter=self.adapter, **kwargs)

    def recorded(self):
        meta = json.loads((self.bronze / "metadata.json").read_text())
        meta.update(source="recording", mode="dual", audio_status="ok", transcription_provider="fixture",
                    recordings=[{"filename": "audio.ogg", "transcribed": True}])
        (self.bronze / "metadata.json").write_text(json.dumps(meta))
        (self.bronze / "audio.ogg").write_bytes(b"fixture original audio")
        (self.bronze / "transcript_raw.txt").write_text("Fala original que não contém a anotação manual.")
        (self.bronze / "transcript_segments.json").write_text('{"recordings": []}')
        return meta

    def test_manual_create_safe_distinct_and_library_projection(self):
        other = create_annotations("Planejamento com café", self.storage)
        self.assertNotEqual(other["slug"], self.slug)
        self.assertFalse((self.bronze / "transcript_raw.txt").exists())
        self.assertEqual(self.storage.get_meeting(self.slug)["source"], "manual")
        self.assertTrue(all(x["source"] == "manual" for x in self.storage.list_recent_meetings()))
        self.assertEqual(get_annotations(self.slug, self.storage)["revision"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(pending_candidates(self.storage), [])
        for title in ("", "\ninvalid", "x" * 257):
            with self.assertRaises(AnnotationError): create_annotations(title, self.storage)

    def test_utf8_atomic_and_optimistic_conflict(self):
        old = get_annotations(self.slug, self.storage)
        saved = save_annotations(self.slug, "# Olá\n\nação 😊", self.storage, expected_sha=old["revision"])
        self.assertEqual(saved["text"], "# Olá\n\nação 😊")
        with self.assertRaises(AnnotationConflict):
            save_annotations(self.slug, "lost update", self.storage, expected_sha=old["revision"])
        self.assertEqual(get_annotations(self.slug, self.storage)["text"], saved["text"])
        self.assertEqual((self.bronze / "annotations.md").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.bronze.glob(".annotations-pending-*")), [])

    def test_atomic_failure_preserves_author_text(self):
        self.save()
        with patch("castanha.annotations.os.replace", side_effect=OSError("fixture full disk")):
            with self.assertRaises(OSError): self.save("replacement")
        self.assertEqual(get_annotations(self.slug, self.storage)["text"], TEXT)
        self.assertFalse(list(self.bronze.glob(".annotations-pending-*")))

    def test_slug_notes_metadata_and_checkpoint_symlink_fifo_guards(self):
        sentinel = self.root / "sentinel"; sentinel.write_text("private original")
        for slug in ("../outside", ".", "a/b", "a\\b", "\x00"):
            with self.assertRaises((AnnotationError, OSError)): get_annotations(slug, self.storage)
        link = self.storage.bronze_dir / "link"; link.symlink_to(self.bronze, target_is_directory=True)
        with self.assertRaises(OSError): get_annotations("link", self.storage)
        for filename in ("annotations.md", "metadata.json", CHECKPOINT):
            path = self.bronze / filename
            original = path.read_bytes() if path.exists() else None
            if path.exists(): path.unlink()
            path.symlink_to(sentinel)
            with self.assertRaises((AnnotationError, OSError)): self.save("replacement")
            path.unlink(); os.mkfifo(path)
            with self.assertRaises(AnnotationError): get_annotations(self.slug, self.storage)
            if filename == CHECKPOINT: self.assertTrue(regeneration_pending(self.bronze))
            path.unlink()
            if original is not None: path.write_bytes(original)
        self.assertEqual(sentinel.read_text(), "private original")

    def test_invalid_utf8_oversize_and_nul_preserved(self):
        self.save()
        for text in ("é" * MAX_BYTES, "x\x00y"):
            with self.assertRaises(AnnotationError): self.save(text)
        (self.bronze / "annotations.md").write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeError): get_annotations(self.slug, self.storage)
        self.assertEqual((self.bronze / "annotations.md").read_bytes(), b"\xff\xfe")

    def test_manual_summary_only_no_asr_no_ingest(self):
        self.save()
        with patch("castanha.engine.get_transcriber", side_effect=AssertionError("no ASR")):
            result = self.run_summary()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["delivery"]["status"], "local_only")
        self.adapter.ingest_meeting.assert_not_called()
        self.assertEqual(self.summarizer.generate_silver.call_args.args[1], "")
        self.assertEqual(self.summarizer.generate_gold.call_args.args[0]["manual_annotations"], TEXT)
        self.assertFalse((self.bronze / "transcript_raw.txt").exists())
        self.assertEqual(get_annotations(self.slug, self.storage)["text"], TEXT)
        self.assertEqual(pending_candidates(self.storage), [])

    def test_pending_refuses_real_asr_or_capture(self):
        self.save()
        for field, value in (("transcription_pending", True), ("recordings", [{"transcribed": False}])):
            meta = json.loads((self.bronze / "metadata.json").read_text())
            old = meta.get(field); meta[field] = value
            (self.bronze / "metadata.json").write_text(json.dumps(meta))
            with self.assertRaises(AnnotationError): self.run_summary()
            meta[field] = old; (self.bronze / "metadata.json").write_text(json.dumps(meta))
        (self.bronze / ".jobs").mkdir()
        (self.bronze / ".jobs/job.json").write_text('{"stage":"pending"}')
        with self.assertRaises(AnnotationError): self.run_summary()
        self.summarizer.generate_silver.assert_not_called()
        self.assertFalse((self.bronze / CHECKPOINT).exists())

    def test_frozen_legacy_summary_stays_local_and_metadata_unchanged(self):
        self.recorded(); self.save()
        (self.bronze / ".legacy-recovery").mkdir()
        original = (self.bronze / "metadata.json").read_bytes()
        result = self.run_summary()
        self.assertEqual(result["delivery"]["status"], "local_only")
        self.adapter.ingest_meeting.assert_not_called()
        self.assertEqual((self.bronze / "metadata.json").read_bytes(), original)
        self.assertTrue((self.storage.silver_dir / (self.slug + ".md")).exists())

    def test_recorded_originals_and_metadata_context_preserved(self):
        self.recorded(); self.save()
        files = ["audio.ogg", "transcript_raw.txt", "transcript_segments.json", "annotations.md"]
        original = {f: (self.bronze / f).read_bytes() for f in files}
        result = self.run_summary()
        self.assertEqual(result["status"], "ok")
        self.assertEqual({f: (self.bronze / f).read_bytes() for f in files}, original)
        self.assertNotIn("manual_annotations", json.loads((self.bronze / "metadata.json").read_text()))
        self.adapter.ingest_meeting.assert_called_once()
        self.assertEqual(self.summarizer.generate_gold.call_args.args[2], original["transcript_raw.txt"].decode())

    def test_quota_old_derivatives_preserved_resume_frozen_notes(self):
        self.save()
        silver = self.storage.silver_dir / (self.slug + ".md"); silver.write_text("old silver")
        gold = self.storage.gold_dir / (self.slug + ".json"); gold.write_text('{"old":true}')
        self.summarizer.generate_gold.side_effect = LlmUnavailable("quota fixture")
        self.assertEqual(self.run_summary()["status"], "pending")
        self.assertEqual(silver.read_text(), "old silver")
        self.assertEqual(gold.read_text(), '{"old":true}')
        self.save("New author text while previous summary is pending")
        self.summarizer.generate_gold.side_effect = None
        with patch("castanha.summarizer.MeetingSummarizer", return_value=self.summarizer):
            self.assertEqual(pending_candidates(self.storage), [("", self.slug)])
            result = sync_meeting(self.slug, self.storage)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["notes_changed_since_request"])
        self.assertEqual(self.summarizer.generate_silver.call_args.args[0]["manual_annotations"], TEXT)
        self.assertNotEqual(get_annotations(self.slug, self.storage)["text"], TEXT)
        self.assertEqual(pending_candidates(self.storage), [])

    def test_previous_pair_saved_before_synthesis_and_restorable(self):
        self.save()
        silver = self.storage.silver_dir / (self.slug + ".md")
        gold = self.storage.gold_dir / (self.slug + ".json")
        before = {"silver": "# Resumo anterior\n\nAção com café 😊\n", "gold": '{ "facts": [] }\n'}
        silver.write_text(before["silver"]); gold.write_text(before["gold"])
        def generate(metadata, transcript):
            checkpoint = json.loads((self.bronze / CHECKPOINT).read_text())
            self.assertEqual(checkpoint["previous_outputs"], before)
            self.assertEqual(silver.read_text(), before["silver"])
            self.assertEqual(gold.read_text(), before["gold"])
            return "novo resumo"
        self.summarizer.generate_silver.side_effect = generate
        self.run_summary()
        checkpoint = json.loads((self.bronze / CHECKPOINT).read_text())
        from castanha.durability import atomic_write
        atomic_write(silver, checkpoint["previous_outputs"]["silver"])
        atomic_write(gold, checkpoint["previous_outputs"]["gold"])
        self.assertEqual(silver.read_bytes(), before["silver"].encode())
        self.assertEqual(gold.read_bytes(), before["gold"].encode())

    def test_first_summary_backup_records_absent_outputs_explicitly(self):
        self.save(); self.run_summary()
        checkpoint = json.loads((self.bronze / CHECKPOINT).read_text())
        self.assertEqual(checkpoint["previous_outputs"], {"silver": None, "gold": None})

    def test_ready_checkpoint_resumes_pair_without_llm(self):
        self.save()
        from castanha.annotations import _write
        def fail_gold(fd, name, text):
            if name.endswith(".json") and name != CHECKPOINT:
                raise OSError("fixture disk full")
            return _write(fd, name, text)
        with patch("castanha.annotations._write", side_effect=fail_gold):
            with self.assertRaises(OSError): self.run_summary()
        self.assertEqual(json.loads((self.bronze / CHECKPOINT).read_text())["status"], "ready")
        self.summarizer.reset_mock()
        self.run_summary(resume=True)
        self.summarizer.generate_silver.assert_not_called()
        self.summarizer.generate_gold.assert_not_called()

    def test_remote_pending_keeps_retry_and_does_not_redo_summary(self):
        self.recorded(); self.save()
        self.adapter.ingest_meeting.return_value = {"status": "pending", "reason": "fixture"}
        result = self.run_summary()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["summary_status"], "complete")
        self.assertIn("entrega pendente", result["message"])
        self.summarizer.reset_mock()
        self.adapter.ingest_meeting.return_value = {"status": "ok"}
        self.assertEqual(self.run_summary(resume=True)["status"], "ok")
        self.summarizer.generate_silver.assert_not_called()

    def test_native_success_clears_old_pending_summary_projection(self):
        metadata = self.recorded(); self.save()
        metadata.update(summary_status="pending", processing_status="pending", summary_error="old failure")
        (self.bronze / "metadata.json").write_text(json.dumps(metadata))
        result = self.run_summary()
        current = self.storage.get_meeting(self.slug)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(current["summary_status"], "complete")
        self.assertEqual(current["processing_status"], "complete")
        self.assertEqual(current["summary_error"], "")
        self.assertEqual(self.adapter.ingest_meeting.call_args.args[0]["processing_status"], "complete")

    def test_changed_transcript_blocks_resume(self):
        self.recorded(); self.save()
        self.summarizer.generate_silver.side_effect = LlmUnavailable("fixture")
        self.run_summary()
        (self.bronze / "transcript_raw.txt").write_text("unexpected replacement")
        with self.assertRaises(AnnotationError): self.run_summary(resume=True)

    def test_unconfigured_llm_is_pending_never_template_success(self):
        self.save()
        result = regenerate_annotations(self.slug, self.storage, summarizer=MeetingSummarizer())
        self.assertEqual(result["status"], "pending")
        self.assertFalse((self.storage.silver_dir / (self.slug + ".md")).exists())

    def test_engine_context_copy_does_not_mutate_metadata(self):
        metadata = self.recorded(); self.save()
        copied = synthesis_metadata(metadata, self.storage, self.slug)
        self.assertEqual(copied["manual_annotations"], TEXT)
        self.assertNotIn("manual_annotations", metadata)

    def test_cli_create_get_save_conflict_and_invalid_utf8(self):
        cli = str(Path(__file__).resolve().parents[1] / "bin/castanha")
        def run(*args, data=None):
            result = subprocess.run([cli, "annotations", *args, "--json"], input=data,
                capture_output=True, timeout=10)
            return result.returncode, json.loads(result.stdout)
        code, created = run("create", "--title", "CLI manual")
        self.assertEqual(code, 0)
        slug = created["slug"]
        code, saved = run("save", slug, "--stdin", "--expected-revision", created["revision"], data=TEXT.encode())
        self.assertEqual(code, 0)
        self.assertEqual(run("get", slug)[1]["text"], TEXT)
        code, conflict = run("save", slug, "--stdin", "--expected-revision", created["revision"], data=b"wrong")
        self.assertEqual((code, conflict["status"]), (1, "conflict"))
        self.assertEqual(run("save", slug, "--stdin", data=b"\xff")[0], 1)
        self.assertEqual(run("get", slug)[1]["text"], TEXT)

    def test_save_does_not_wait_for_recording_processing_lock(self):
        from castanha.durability import meeting_lock
        cli = str(Path(__file__).resolve().parents[1] / "bin/castanha")
        with meeting_lock(self.bronze):
            result = subprocess.run([cli, "annotations", "save", self.slug, "--stdin", "--json"],
                                    input=TEXT.encode(), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["text"], TEXT)

    def test_initial_durable_pipeline_really_passes_manual_context(self):
        from tests.test_durable_jobs import TestDurableJobs
        case = TestDurableJobs()
        case.setUp()
        try:
            slug = case.crash()
            save_annotations(slug, TEXT, case.engine.storage)
            with patch("castanha.engine.get_transcriber") as provider, \
                 patch.object(case.engine.summarizer, "generate_silver", return_value="fixture silver") as silver, \
                 patch.object(case.engine.summarizer, "generate_gold", return_value=GOLD) as gold:
                provider.return_value.transcribe.return_value = case.transcription()
                case.engine.process_pending(slug)
            self.assertEqual(silver.call_args.args[0]["manual_annotations"], TEXT)
            self.assertEqual(gold.call_args.args[0]["manual_annotations"], TEXT)
            self.assertEqual(silver.call_args.args[1], "Decisão preservada")
            self.assertNotIn(TEXT, case.engine.storage.read_transcript(slug))
            self.assertNotIn("manual_annotations", case.engine.storage._read_bronze_metadata(slug))
        finally:
            case.doCleanups()

    def test_real_summarizer_manual_only_and_recorded_hermes_groq_prompts(self):
        for provider in ("hermes_ssh", "groq"):
            for transcript in ("", "Fala original preservada."):
                summarizer = MeetingSummarizer()
                summarizer.provider = provider
                summarizer.last_provider = provider
                summarizer.api_key = "fixture"
                metadata = {"title": "Fixture", "manual_annotations": TEXT, "source": "manual"}
                def llm(system, prompt, json_mode=False):
                    self.assertIn(MANUAL_GUIDANCE, system)
                    self.assertIn(TEXT, prompt)
                    return json.dumps(GOLD) if json_mode else "## 📌 Resumo Executivo\nSegundo as anotações manuais, revisar orçamento."
                with patch.object(summarizer, "_call_llm", side_effect=llm) as call:
                    silver = summarizer.generate_silver(metadata, transcript)
                    summarizer.generate_gold(metadata, silver, transcript)
                self.assertIn("Anotações manuais do usuário", silver)
                self.assertIn(TEXT, silver)
                if not transcript:
                    self.assertIn("Não houve gravação ou transcrição", silver)
                    self.assertNotIn("## 📝 Transcrição", silver)
                else:
                    self.assertIn(transcript, silver)
                    self.assertLess(silver.index(TEXT), silver.index("## 📝 Transcrição"))
                self.assertGreaterEqual(call.call_count, 2)

    def test_chunked_silver_receives_manual_context_and_preserves_raw(self):
        summarizer = MeetingSummarizer(); summarizer.provider = "groq"; summarizer.api_key = "fixture"
        calls = []
        def llm(system, prompt, **kwargs):
            calls.append(prompt)
            self.assertIn(TEXT, prompt)
            self.assertIn(MANUAL_GUIDANCE, system)
            if len(calls) == 1: raise LlmTooLarge(8000, 10000)
            return "## 📌 Resumo Executivo\nSegundo as anotações manuais, revisar orçamento."
        with patch.object(summarizer, "_call_llm", side_effect=llm):
            silver = summarizer.generate_silver({"manual_annotations": TEXT}, "fala " * 2200)
        self.assertGreater(len(calls), 2)
        self.assertIn("fala " * 2200, silver)


if __name__ == "__main__": unittest.main()
