import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deploy_ui", Path(__file__).resolve().parents[1] / "scripts/deploy-ui.py")
deploy_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_ui)


class UiDeploymentTests(unittest.TestCase):
    def test_live_panel_command_blocks_reload_but_detached_daemon_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, name, parent, command in [(20, "quickshell", 1, b"quickshell"),
                                               (21, "python3", 20, b"python3\0-m\0castanha.retry")]:
                p = root / str(pid)
                p.mkdir()
                (p / "cmdline").write_bytes(command)
                (p / "stat").write_text(f"{pid} ({name}) S {parent} 0 0")
            with self.assertRaises(RuntimeError):
                deploy_ui.guard_shell_jobs(root)
            (root / "21" / "stat").write_text("21 (python3) S 1 0 0")
            deploy_ui.guard_shell_jobs(root)

    def test_backend_and_configuration_are_rejected(self):
        for path in ("castanha/engine.py", "bin/castanha", "config.example.json", "manifest.json", "setup"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                deploy_ui.validate_paths(["Panel.qml", path])
        deploy_ui.validate_paths(["Panel.qml", "DeliveryStatus.js", "i18n.js", "tests/test_ui_deploy.py"])

    def test_old_panel_marker_cannot_pass_health(self):
        with patch.object(deploy_ui, "run", return_value="old-panel"), \
             patch.object(deploy_ui, "guard_shell_jobs"), patch.object(deploy_ui.time, "sleep"):
            with self.assertRaises(deploy_ui.subprocess.CalledProcessError):
                deploy_ui.reload_panel()

    def exercise(self, fail=False, rollback_blocked=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            previous, candidate = "a" * 40, "b" * 40
            guards = []

            def guard():
                guards.append(True)
                calls.append(("guard",))
                if rollback_blocked and len(guards) >= 4:
                    raise PermissionError("fixture: processo ilegível")

            def run(*args):
                calls.append(args)
                if args == ("omarchy-shell", "castanha-view", "health"):
                    return "uploaded-audio-pending-v1"
                if args[0] == "git":
                    command = args[3]
                    if command == "rev-parse": return previous
                    if command == "diff": return "Panel.qml\nDeliveryStatus.js"
                return ""

            with patch.object(deploy_ui, "run", side_effect=run), \
                 patch.object(deploy_ui, "guard_shell_jobs", side_effect=guard), \
                 patch.object(deploy_ui, "get_state_dir", return_value=root), \
                 patch.object(deploy_ui, "snapshot", side_effect=[{"pids": {123: "capture-start"}, "audio_path": "/fixture/audio.ogg", "audio_bytes": 100},
                       {"pids": {123: "different-start" if fail else "capture-start"}, "audio_path": "/fixture/audio.ogg", "audio_bytes": 200},
                       {"pids": {123: "capture-start"}, "audio_path": "/fixture/audio.ogg", "audio_bytes": 250}]):
                if fail:
                    with self.assertRaises(PermissionError if rollback_blocked else RuntimeError):
                        deploy_ui.deploy(root, candidate)
                else:
                    deploy_ui.deploy(root, candidate)
            journal = json.loads((root / "local-ui-release.json").read_text())
            self.assertEqual(journal["phase"], "rollback_failed" if rollback_blocked else "rolled_back" if fail else "healthy")
            self.assertLess(calls.index(("guard",)), next(i for i, c in enumerate(calls) if "merge" in c))
            self.assertFalse(any(c[0] in ("kill", "pkill", "systemctl") for c in calls))
            resets = [c for c in calls if "reset" in c]
            self.assertEqual(len(resets), 1 if fail and not rollback_blocked else 0)
            if fail and not rollback_blocked: self.assertEqual(resets[0][-2:], ("--keep", previous))

    def test_active_capture_survives_ui_update(self):
        self.exercise()

    def test_failed_health_rolls_back_only_code(self):
        self.exercise(fail=True)

    def test_blocked_rollback_is_recorded_without_claiming_success(self):
        self.exercise(fail=True, rollback_blocked=True)
