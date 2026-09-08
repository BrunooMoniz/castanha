"""A atualização preserva a versão anterior quando a saúde falha."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("local_release", Path(__file__).resolve().parents[1] / "scripts/deploy-local.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class LocalReleaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "version").write_text("old")
        self.git("add", "version")
        self.git("commit", "-qm", "old")
        self.old = self.git("rev-parse", "HEAD")
        (self.repo / "version").write_text("new")
        self.git("commit", "-qam", "new")
        self.new = self.git("rev-parse", "HEAD")
        self.git("reset", "--hard", self.old)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE).strip()

    def install(self, health):
        original = release.run
        def command(*args):
            return "" if args[0] == "systemctl" else original(*args)
        with patch.object(release, "run", side_effect=command), \
             patch.object(release, "health", side_effect=health), \
             patch.object(release, "get_state_dir", return_value=self.root), \
             patch.object(release, "assert_idle"), patch.object(release.time, "sleep"):
            release.deploy(self.repo, self.new)

    def test_success_updates_the_single_checkout(self):
        self.install([None, None])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.new)
        self.assertEqual((self.repo / "version").read_text(), "new")

    def test_failed_health_restores_code_and_keeps_candidate_reachable(self):
        with self.assertRaisesRegex(RuntimeError, "daemon failed"):
            self.install([None, RuntimeError("daemon failed"), None])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertEqual((self.repo / "version").read_text(), "old")
        self.assertEqual(self.git("rev-parse", self.new), self.new)
        self.assertIn(self.old, (self.root / "local-release.json").read_text())

    def test_dirty_checkout_is_preserved(self):
        (self.repo / "version").write_text("work in progress")
        with self.assertRaisesRegex(AssertionError, "alterações locais"):
            self.install([])
        self.assertEqual((self.repo / "version").read_text(), "work in progress")
