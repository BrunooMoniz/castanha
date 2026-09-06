"""Primeira transição contra b97131e real, somente checkouts/processos descartáveis."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import py_compile
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha import deployment
from castanha.transition_fence import (FenceError, LEGACY_SHA, RECEIPT, TransitionFence,
                                       file_record, tree_record)

REPO = Path(__file__).resolve().parents[1]


class TestTransitionFence(unittest.TestCase):
    def test_recovery_resumes_real_restored_identity(self):
        self.crash_with_fence()
        journal = self.state / "deployment.json"
        plan = json.loads(journal.read_text())
        plan["previous_daemon"] = asdict(deployment.ProcessIdentity(
            123, 1, "previous", str(self.old), sys.executable, os.getuid()))
        journal.write_text(json.dumps(plan))
        platform = deployment.LocalPlatform(self.state)

        def start(root):
            ready_r, ready_w = os.pipe()
            child = subprocess.Popen(
                [sys.executable, "-B", "-c",
                 "import os,time,sys;os.write(int(sys.argv[1]),b'R');time.sleep(60)",
                 str(ready_w), "-m", "castanha.daemon"], cwd=root, pass_fds=(ready_w,))
            os.close(ready_w)
            def cleanup():
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            self.addCleanup(cleanup)
            try:
                self.assertTrue(select.select([ready_r], [], [], 5)[0])
                self.assertEqual(os.read(ready_r, 1), b"R")
            finally:
                os.close(ready_r)
            (self.state / "daemon.pid").write_text(str(child.pid))
            return deployment.process_identity(child.pid)

        with patch.object(platform, "start", start), patch.object(
                platform, "health", side_effect=SystemExit("interrupted rollback health")):
            with self.assertRaises(SystemExit):
                deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                   state_dir=self.state, platform=platform)
        self.assertEqual(json.loads(journal.read_text())["phase"], "rollback_checking")
        with patch.object(platform, "start") as starts, patch.object(platform, "stop") as stops, \
                patch.object(platform, "health"):
            result = deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                        state_dir=self.state, platform=platform)
            self.assertEqual(result["status"], "rolled_back")
            starts.assert_not_called()
            stops.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="castanha-f5pt-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "old"
        subprocess.run(["git", "clone", "--shared", "--quiet", "--no-checkout", str(REPO), str(self.old)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.old), "checkout", "--quiet", LEGACY_SHA],
                       check=True, capture_output=True)
        self.state = self.base / "state"
        self.state.mkdir()
        (self.state / "state.json").write_text('{"status":"idle","pid":null}')
        self.cli, self.plugin = self.base / "cli", self.base / "plugin"
        # Preservar literalmente links relativos também faz parte da reversão.
        self.cli.symlink_to("old/bin/castanha")
        self.plugin.symlink_to("old")
        self.fence = TransitionFence(self.old, self.state, self.cli, self.plugin)
        self.env = {"HOME": str(self.base), "XDG_STATE_HOME": str(self.base / "xdg"),
                    "XDG_CONFIG_HOME": str(self.base / "config"), "PATH": os.defpath,
                    "PYTHONPATH": str(self.old), "PYTHONDONTWRITEBYTECODE": "1"}
        self.new = self.base / "new"
        self.new.mkdir()
        self.platform = Mock()
        self.platform.daemon.return_value = None
        self.platform.revision.return_value = LEGACY_SHA
        self.platform.start.return_value = deployment.ProcessIdentity(123, 1, "fixture",
                                                                     str(self.new), sys.executable, os.getuid())

    def activate(self):
        record = self.fence.prepare()
        self.fence.activate(record)
        return record

    def deploy(self, **kwargs):
        return deployment.deploy(self.new, "candidate-sha", cli_link=self.cli,
                                 plugin_link=self.plugin, state_dir=self.state,
                                 platform=self.platform, initial_transition=True, apply=True, **kwargs)

    def proc_for(self, *children):
        proc = self.base / "proc"
        proc.mkdir(exist_ok=True)
        for child in children:
            (proc / str(child.pid)).symlink_to(Path("/proc") / str(child.pid), target_is_directory=True)
        return proc

    def child_at_barrier(self, *, loaded):
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        # O script CLI e o start_recording executados são os bytes de b97131e.
        # Só substituímos construtor/I/O/áudio/notificação; nenhuma gravação real.
        code = r'''
import os, sys, runpy
from pathlib import Path
from types import SimpleNamespace
root, cli, ready, go, loaded = sys.argv[1:]
ready, go = int(ready), int(go)
sys.path.insert(0, root)
def barrier():
    os.write(ready, b"R")
    os.read(go, 1)
    return False
if loaded == "yes":
    import castanha.engine as engine
    def initialize(self):
        self.config = {}
        self.state_mgr = SimpleNamespace(read=lambda: {"status": "idle"},
                                         write=lambda state: None)
        self.recorder = SimpleNamespace(start=lambda *a, **k: SimpleNamespace(pid=123))
    engine.CastanhaEngine.__init__ = initialize
    engine.is_default_source_muted = barrier
    engine.notify = lambda *a, **k: None
    sys.argv = [cli, "start"]
    runpy.run_path(cli, run_name="__main__")
else:
    with open(cli, "rb") as source:
        data = source.read()
        barrier()
        sys.argv = [cli, "start"]
        exec(compile(data, cli, "exec"), {"__name__": "__main__", "__file__": cli})
'''
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.old), str(self.cli),
                                  str(ready_write), str(go_read), "yes" if loaded else "no"],
                                 env=self.env, cwd=self.base, pass_fds=(ready_write, go_read),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(ready_write)
        os.close(go_read)
        def finish():
            try:
                os.write(go_write, b"G")
            except BrokenPipeError:
                pass
            os.close(go_write)
            child.communicate(timeout=10)
        self.addCleanup(finish)
        self.assertTrue(select.select([ready_read], [], [], 10)[0], "fixture não chegou à barreira")
        self.assertEqual(os.read(ready_read, 1), b"R")
        os.close(ready_read)
        return child, go_write

    @contextmanager
    def direct_daemon_reader(self, script):
        """Python executa daemon.py b97131e real; trava antes de qualquer I/O do daemon."""
        bootstrap = self.base / "bootstrap"
        bootstrap.mkdir(exist_ok=True)
        (bootstrap / "sitecustomize.py").write_text(
            "import os, sys\n"
            "def trace(frame, event, arg):\n"
            "    if event == 'call' and frame.f_code.co_name == 'run_daemon':\n"
            "        sys.settrace(None)\n"
            "        os.write(int(os.environ['F5PT_READY']), b'R')\n"
            "        os.read(int(os.environ['F5PT_GO']), 1)\n"
            "        raise SystemExit(0)\n"
            "    return trace\n"
            "sys.settrace(trace)\n")
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        child = subprocess.Popen([sys.executable, "-B", str(script)], cwd=self.base,
                                 env={**self.env, "PYTHONPATH": f"{bootstrap}:{self.old}",
                                      "F5PT_READY": str(ready_write), "F5PT_GO": str(go_read)},
                                 pass_fds=(ready_write, go_read), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        os.close(ready_write)
        os.close(go_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 10)[0], "daemon não abriu a fixture")
            self.assertEqual(os.read(ready_read, 1), b"R")
            proc = Path("/proc") / str(child.pid)
            self.assertEqual((proc / "cwd").resolve(), self.base)
            self.assertIn(os.fsencode(script), (proc / "cmdline").read_bytes().split(b"\0"))
            for fd in (proc / "fd").iterdir():
                target = Path(os.readlink(fd))
                self.assertFalse(self.old == target or self.old in target.parents,
                                 "prova não pode depender de FD aberto no checkout")
            yield child
        finally:
            os.close(ready_read)
            try:
                os.write(go_write, b"G")
            except BrokenPipeError:
                pass
            os.close(go_write)
            _, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr.decode())

    def crash_with_fence(self):
        activate = TransitionFence.activate
        def crash(fence, record):
            activate(fence, record)
            raise SystemExit("crash depois da cerca")
        with patch.object(TransitionFence, "activate", crash), \
                patch.object(deployment, "capture_processes", return_value=[]), \
                self.assertRaises(SystemExit):
            self.deploy()
        self.fence.assert_active()

    @contextmanager
    def live_candidate_daemon(self):
        package = self.new / "castanha"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "daemon.py").write_text(
            "import os\nos.write(int(os.environ['F5PT_READY']), b'R')\n"
            "os.read(int(os.environ['F5PT_GO']), 1)\n")
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        child = subprocess.Popen([sys.executable, "-B", "-m", "castanha.daemon"], cwd=self.new,
                                 env={**self.env, "PYTHONPATH": str(self.new),
                                      "F5PT_READY": str(ready_write), "F5PT_GO": str(go_read)},
                                 pass_fds=(ready_write, go_read))
        os.close(ready_write)
        os.close(go_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 10)[0])
            self.assertEqual(os.read(ready_read, 1), b"R")
            yield child, deployment.process_identity(child.pid)
        finally:
            os.close(ready_read)
            os.write(go_write, b"G")
            os.close(go_write)
            self.assertEqual(child.wait(timeout=10), 0)

    def test_direct_real_package_readers_with_closed_fds_block_before_release_swap(self):
        scripts = (self.old / "castanha/daemon.py", self.plugin / "castanha/daemon.py",
                   Path("old/castanha/daemon.py"), Path("plugin/castanha/daemon.py"),
                   Path("old/castanha/../castanha/daemon.py"))
        readers = TransitionFence.assert_no_readers
        for script in scripts:
            with self.subTest(script=script), self.direct_daemon_reader(script) as child:
                proc = self.proc_for(child)
                self.platform.start.reset_mock()
                with patch.object(TransitionFence, "assert_no_readers", lambda f: readers(f, proc=proc)), \
                        patch.object(deployment, "capture_processes", return_value=[]), \
                        patch.object(deployment, "replace_link", wraps=deployment.replace_link) as swaps:
                    result = self.deploy()
                self.assertEqual(result["status"], "rolled_back")
                targets = [call.args[1] for call in swaps.call_args_list]
                self.assertNotIn(self.new, targets)
                self.assertNotIn(self.new / "bin/castanha", targets)
                self.platform.start.assert_not_called()
                self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
                self.assertEqual(self.plugin.resolve(), self.old)
                self.assertEqual(json.loads((self.state / RECEIPT).read_text())["phase"], "restored")

    def test_crash_recovery_with_proven_dead_pid_restores_package_and_links(self):
        before = tree_record(self.old / "castanha")
        for zombie in (False, True):
            with self.subTest(zombie=zombie):
                self.crash_with_fence()
                child = subprocess.Popen([sys.executable, "-B", "-c", "pass"], cwd=self.base)
                try:
                    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                    if not zombie:
                        child.wait(timeout=10)
                    (self.state / "daemon.pid").write_text(str(child.pid))
                    platform = deployment.LocalPlatform(self.state)
                    # Reproduz as DUAS recusas do caminho anterior à correção.
                    for root in (self.new, self.old):
                        with self.assertRaises(deployment.DeploymentError):
                            platform.daemon(root)
                    with patch.object(platform, "stop") as stop, \
                            patch.object(platform, "start") as start, \
                            patch.object(platform, "health") as health, \
                            patch.object(deployment, "capture_processes", return_value=[]):
                        result = deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                                    state_dir=self.state, platform=platform)
                        self.assertEqual(result["status"], "rolled_back")
                        self.assertEqual(deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                                            state_dir=self.state, platform=platform)["status"],
                                         "already_rolled_back")
                        stop.assert_not_called()
                        start.assert_not_called()
                        health.assert_called_once_with(self.old, None)
                    self.assertEqual(tree_record(self.old / "castanha"), before)
                    self.assertEqual(os.readlink(self.cli), "old/bin/castanha")
                    self.assertEqual(os.readlink(self.plugin), "old")
                finally:
                    child.wait(timeout=10)

    def test_recovery_rejects_live_unreadable_or_reused_pid_without_signals(self):
        self.crash_with_fence()
        with self.live_candidate_daemon() as (child, identity):
            (self.state / "daemon.pid").write_text(str(child.pid))
            plan_path = self.state / "deployment.json"
            plan = json.loads(plan_path.read_text())
            platform = deployment.LocalPlatform(self.state)
            for failure in ("reused", "unreadable", "proc_child_missing", "pidfd_denied", "unrecorded"):
                with self.subTest(failure=failure):
                    plan["new_daemon"] = asdict(replace(identity, start_ticks=identity.start_ticks + 1)
                                                if failure == "reused" else identity)
                    if failure == "unrecorded":
                        del plan["new_daemon"]
                    plan_path.write_text(json.dumps(plan))
                    read_bytes = Path.read_bytes
                    def unreadable(path):
                        if path == Path("/proc") / str(child.pid) / "cmdline":
                            raise (PermissionError("PID vivo ilegível") if failure == "unreadable"
                                   else FileNotFoundError("filho de /proc sumiu, PID continua vivo"))
                        return read_bytes(path)
                    identity_probe = (patch.object(Path, "read_bytes", unreadable)
                                      if failure in {"unreadable", "proc_child_missing"} else
                                      patch.object(deployment.os, "pidfd_open", side_effect=PermissionError("negado"))
                                      if failure == "pidfd_denied" else
                                      patch.object(deployment, "process_identity", wraps=deployment.process_identity))
                    with identity_probe, patch.object(platform, "stop") as stop, \
                            patch.object(platform, "start") as start, \
                            patch.object(platform, "health"), \
                            patch.object(deployment.signal, "pidfd_send_signal") as signal, \
                            self.assertRaises(deployment.DeploymentError):
                        deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                           state_dir=self.state, platform=platform)
                    stop.assert_not_called()
                    start.assert_not_called()
                    signal.assert_not_called()
                    self.assertIsNone(child.poll())
                    self.fence.assert_active()
                    self.assertEqual(json.loads(plan_path.read_text())["phase"], "blocking")

    def test_preflight_real_legacy_is_read_only_and_requires_explicit_transition(self):
        before = tree_record(self.old / "castanha")
        self.fence.inspect()
        self.assertEqual(tree_record(self.old / "castanha"), before)
        self.assertFalse((self.state / RECEIPT).exists())
        with self.assertRaises(deployment.LegacyTransitionBlocked):
            deployment.LocalPlatform(self.state).validate_runtime(self.old)

    def test_all_managed_entrypoints_block_against_actual_legacy(self):
        original = tree_record(self.old / "castanha")
        self.activate()
        for command in ([str(self.cli), "start"], [str(self.plugin / "bin/castanha"), "toggle"],
                        [str(self.old / "bin/castanha"), "start"],
                        [sys.executable, "bin/castanha", "start"],
                        [sys.executable, "-m", "castanha.daemon"],
                        [sys.executable, "castanha/daemon.py"]):
            with self.subTest(command=command):
                result = subprocess.run(command, cwd=self.old, env=self.env, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.base / "xdg").exists())
                self.fence.assert_active()
        self.fence.restore()
        self.fence.restore()
        self.assertEqual(tree_record(self.old / "castanha"), original)
        self.assertEqual(os.readlink(self.cli), "old/bin/castanha")
        self.assertEqual(os.readlink(self.plugin), "old")

    def test_fence_accepts_and_preserves_valid_python_caches(self):
        py_compile.compile(str(self.old / "castanha/engine.py"), doraise=True)
        before = tree_record(self.old / "castanha")
        self.activate()
        # Sem -B também deve continuar recusando, mesmo gerando cache do stub.
        env = {k: v for k, v in self.env.items() if k != "PYTHONDONTWRITEBYTECODE"}
        result = subprocess.run([sys.executable, str(self.cli), "start"], env=env,
                                cwd=self.old, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.fence.assert_active()
        self.fence.restore()
        self.assertEqual(tree_record(self.old / "castanha"), before)

    def test_loaded_real_start_is_detected_before_any_release_swap(self):
        child, _ = self.child_at_barrier(loaded=True)
        proc = self.proc_for(child)
        original = TransitionFence.assert_no_readers
        swaps = []
        replace = deployment.replace_link
        def spy(path, target):
            swaps.append(str(target))
            replace(path, target)
        with patch.object(TransitionFence, "assert_no_readers", lambda f: original(f, proc=proc)), \
                patch.object(deployment, "capture_processes", return_value=[]), \
                patch.object(deployment, "replace_link", side_effect=spy):
            result = self.deploy()
        self.assertEqual(result["status"], "rolled_back")
        self.assertNotIn(str(self.new), swaps)
        self.assertNotIn(str(self.new / "bin/castanha"), swaps)
        self.platform.start.assert_not_called()
        self.assertEqual(os.readlink(self.cli), "old/bin/castanha")

    def test_file_opened_immediately_before_exchange_is_detected_and_then_blocked(self):
        child, go = self.child_at_barrier(loaded=False)
        self.activate()
        with self.assertRaisesRegex(FenceError, "aberto antes"):
            self.fence.assert_no_readers(proc=self.proc_for(child))
        os.write(go, b"G")
        stdout, stderr = child.communicate(timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("gravação recusada", stderr)
        self.assertNotIn("Gravação iniciada", stdout)

    def test_success_keeps_old_checkout_fenced_and_recover_restores_it(self):
        before = tree_record(self.old / "castanha")
        empty_proc = self.proc_for()
        original = TransitionFence.assert_no_readers
        with patch.object(TransitionFence, "assert_no_readers", lambda f: original(f, proc=empty_proc)), \
                patch.object(deployment, "capture_processes", return_value=[]), \
                patch.object(deployment, "settle", side_effect=AssertionError("polling não é cerca")):
            self.assertEqual(self.deploy()["status"], "ok")
            self.fence.assert_active()
            self.assertEqual(self.cli.resolve(), self.new / "bin/castanha")
            result = deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                        state_dir=self.state, platform=self.platform)
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                               state_dir=self.state, platform=self.platform)["status"],
                             "already_rolled_back")
        self.assertEqual(tree_record(self.old / "castanha"), before)
        self.assertEqual(os.readlink(self.cli), "old/bin/castanha")
        self.assertEqual(os.readlink(self.plugin), "old")

    def test_crash_at_every_phase_restores_idempotently_with_durable_receipt(self):
        phases = ("preparing", "slot_created", "stub_written", "prepared", "exchanging",
                  "exchanged", "fenced", "restoring", "restore_exchanged", "restored")
        before = tree_record(self.old / "castanha")
        code = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from castanha.transition_fence import TransitionFence
phase = sys.argv[6]
def checkpoint(current):
    if current == phase:
        os._exit(91)
fence = TransitionFence(*sys.argv[2:6], checkpoint=checkpoint)
record = fence.prepare()
fence.activate(record)
fence.restore()
'''
        for phase in phases:
            with self.subTest(phase=phase):
                child = subprocess.run([sys.executable, "-B", "-c", code, str(REPO), str(self.old),
                                        str(self.state), str(self.cli), str(self.plugin), phase],
                                       cwd=self.base, capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 91, child.stderr.decode())
                self.assertEqual((self.state / RECEIPT).stat().st_mode & 0o777, 0o600)
                self.fence.restore()
                self.fence.restore()
                self.assertEqual(tree_record(self.old / "castanha"), before)

    def test_deployment_crash_at_each_swap_and_phase_recovers_the_real_checkout(self):
        before = tree_record(self.old / "castanha")
        proc = self.proc_for()
        code = r'''
import os, sys
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
from castanha import deployment
from castanha.transition_fence import TransitionFence, LEGACY_SHA
root, state, cli, plugin, candidate, proc = map(Path, sys.argv[2:8])
phase = sys.argv[8]
platform = Mock()
platform.daemon.return_value = None
platform.revision.return_value = LEGACY_SHA
platform.start.return_value = deployment.ProcessIdentity(123, 1, "fixture", str(candidate), sys.executable, os.getuid())
write, swap, save, readers = deployment.write_json, deployment.replace_link, TransitionFence.save, TransitionFence.assert_no_readers
def crash(current):
    if current == phase:
        os._exit(92)
def journal(path, record):
    write(path, record)
    crash(record["phase"])
def exchange_link(path, target):
    swap(path, target)
    if target == candidate:
        crash("plugin_swapped")
    if target == candidate / "bin/castanha":
        crash("cli_swapped")
def fence_save(self, record, current):
    save(self, record, current)
    crash(current)
with patch.object(deployment, "capture_processes", return_value=[]), \
     patch.object(deployment, "write_json", journal), \
     patch.object(deployment, "replace_link", exchange_link), \
     patch.object(TransitionFence, "save", fence_save), \
     patch.object(TransitionFence, "assert_no_readers", lambda self: readers(self, proc=proc)):
    deployment.deploy(candidate, "new", cli_link=cli, plugin_link=plugin, state_dir=state,
                      platform=platform, initial_transition=True, apply=True)
'''
        for phase in ("prepared", "blocking", "fenced", "swapping", "plugin_swapped",
                      "checking", "releasing", "cli_swapped", "completed"):
            with self.subTest(phase=phase):
                child = subprocess.run([sys.executable, "-B", "-c", code, str(REPO), str(self.old),
                                        str(self.state), str(self.cli), str(self.plugin), str(self.new),
                                        str(proc), phase], cwd=self.base, capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 92, child.stderr.decode())
                with patch.object(deployment, "capture_processes", return_value=[]):
                    self.assertEqual(deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                                        state_dir=self.state, platform=self.platform)["status"],
                                     "rolled_back")
                    self.assertEqual(deployment.recover(cli_link=self.cli, plugin_link=self.plugin,
                                                        state_dir=self.state, platform=self.platform)["status"],
                                     "already_rolled_back")
                self.assertEqual(tree_record(self.old / "castanha"), before)
                self.assertEqual(os.readlink(self.cli), "old/bin/castanha")
                self.assertEqual(os.readlink(self.plugin), "old")

    def test_unexpected_symlink_hardlink_permissions_and_bytes_refuse_before_mutation(self):
        path = self.old / "castanha/engine.py"
        original = path.read_bytes()
        for mutation in ("symlink", "hardlink", "permission", "bytes", "extra", "cache"):
            with self.subTest(mutation=mutation):
                if mutation == "symlink":
                    path.rename(self.base / "engine")
                    path.symlink_to(self.base / "engine")
                elif mutation == "hardlink":
                    os.link(path, self.base / "engine")
                elif mutation == "permission":
                    path.chmod(0o666)
                elif mutation == "bytes":
                    path.write_bytes(original + b"\n# changed\n")
                elif mutation == "extra":
                    (self.old / "castanha/other.py").write_text("pass")
                else:
                    cache = self.old / "castanha/__pycache__"
                    cache.mkdir()
                    (cache / "engine.bad.pyc").write_bytes(b"bad")
                with self.assertRaises((FenceError, OSError)):
                    self.fence.inspect()
                self.assertFalse((self.state / RECEIPT).exists())
                if mutation == "symlink":
                    path.unlink()
                    (self.base / "engine").rename(path)
                elif mutation == "hardlink":
                    (self.base / "engine").unlink()
                elif mutation == "permission":
                    path.chmod(0o644)
                elif mutation == "bytes":
                    path.write_bytes(original)
                elif mutation == "extra":
                    (self.old / "castanha/other.py").unlink()
                else:
                    (cache / "engine.bad.pyc").unlink()
                    cache.rmdir()

    def test_unexpected_alias_and_receipt_target_are_rejected(self):
        self.cli.unlink()
        alias = self.base / "alias"
        alias.symlink_to(self.old)
        self.cli.symlink_to(alias / "bin/castanha")
        with self.assertRaises(FenceError):
            self.fence.inspect()
        self.cli.unlink()
        self.cli.symlink_to("old/bin/castanha")
        record = self.activate()
        record["slot"] = str(self.base / "unrelated")
        (self.state / RECEIPT).write_text(json.dumps(record))
        with self.assertRaises(FenceError):
            self.fence.restore()

    def test_changed_backup_and_stub_never_get_silently_restored(self):
        record = self.activate()
        target = Path(record["slot"]) / "engine.py"
        original = target.read_bytes()
        target.write_bytes(original + b"\n# foreign change\n")
        with self.assertRaises(FenceError):
            self.fence.restore()
        target.write_bytes(original)
        (self.old / "castanha/__init__.py").write_text("pass\n")
        with self.assertRaises(FenceError):
            self.fence.restore()

    def test_proc_enumeration_and_permissions_fail_closed(self):
        self.activate()
        with self.assertRaises(deployment.DeploymentError):
            self.fence.assert_no_readers(proc=self.base / "absent")
        proc = self.proc_for()
        pid = proc / "12345678"
        pid.mkdir()
        (pid / "cmdline").write_bytes(b"python\0")
        (pid / "cwd").symlink_to(self.base)
        (pid / "fd").mkdir()
        original = Path.iterdir
        def denied(path):
            if path == pid / "fd":
                raise PermissionError("proc negado")
            return original(path)
        with patch.object(Path, "iterdir", denied), self.assertRaises(deployment.DeploymentError):
            self.fence.assert_no_readers(proc=proc)

    def test_open_descriptor_alone_identifies_pre_fence_reader(self):
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        child = subprocess.Popen([sys.executable, "-B", "-c",
                                  "import os,sys; f=open(sys.argv[1]); os.write(int(sys.argv[2]),b'R'); "
                                  "os.read(int(sys.argv[3]),1)",
                                  str(self.old / "castanha/engine.py"), str(ready_write), str(go_read)],
                                 cwd=self.base, pass_fds=(ready_write, go_read))
        os.close(ready_write)
        os.close(go_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 10)[0])
            self.assertEqual(os.read(ready_read, 1), b"R")
            self.activate()
            with self.assertRaisesRegex(FenceError, "aberto antes"):
                self.fence.assert_no_readers(proc=self.proc_for(child))
        finally:
            os.close(ready_read)
            os.write(go_write, b"G")
            os.close(go_write)
            self.assertEqual(child.wait(timeout=10), 0)

    def test_common_lock_rejects_symlink_hardlink_and_unsafe_modes(self):
        from castanha.capture_gate import open_lock
        lock = self.state / deployment.LOCK_NAME
        target = self.base / "untouched"
        target.write_text("preserve")
        for mutation in ("symlink", "hardlink", "permission", "directory"):
            with self.subTest(mutation=mutation):
                if mutation == "symlink":
                    lock.symlink_to(target)
                elif mutation == "hardlink":
                    os.link(target, lock)
                elif mutation == "directory":
                    lock.mkdir()
                else:
                    lock.touch(mode=0o600)
                    lock.chmod(0o666)
                with self.assertRaises(OSError):
                    open_lock(self.state)
                self.assertEqual(target.read_text(), "preserve")
                if mutation == "directory":
                    lock.rmdir()
                else:
                    lock.unlink()

    def test_health_failure_restores_real_package_before_starting_old_daemon(self):
        before = tree_record(self.old / "castanha")
        self.platform.health.side_effect = [RuntimeError("smoke falhou"), None]
        original = TransitionFence.assert_no_readers
        proc = self.proc_for()
        with patch.object(TransitionFence, "assert_no_readers", lambda f: original(f, proc=proc)), \
                patch.object(deployment, "capture_processes", return_value=[]):
            result = self.deploy()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(tree_record(self.old / "castanha"), before)
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.platform.stop.assert_called_once_with(self.platform.start.return_value)

    def test_proc_error_after_fencing_cannot_reach_release_swap(self):
        original = TransitionFence.assert_no_readers
        with patch.object(TransitionFence, "assert_no_readers", lambda f: original(f, proc=self.base / "absent")), \
                patch.object(deployment, "capture_processes", return_value=[]):
            result = self.deploy()
        self.assertEqual(result["status"], "rolled_back")
        self.platform.start.assert_not_called()
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_cache_with_valid_header_but_foreign_code_is_rejected(self):
        source = self.old / "castanha/engine.py"
        path = Path(py_compile.compile(str(source), doraise=True))
        import marshal
        path.write_bytes(path.read_bytes()[:16] + marshal.dumps(compile("pass", str(source), "exec")))
        with self.assertRaisesRegex(FenceError, "Cache diverge"):
            self.fence.inspect()
        self.assertFalse((self.state / RECEIPT).exists())

    def test_fsync_failure_does_not_expose_candidate_and_can_recover(self):
        record = self.fence.prepare()
        original = os.fsync
        def fail_after_exchange(fd):
            if self.old.joinpath("castanha/__init__.py").read_text().startswith('"""Castanha temporariamente'):
                raise OSError("fsync indisponível")
            return original(fd)
        with patch("castanha.transition_fence.os.fsync", side_effect=fail_after_exchange), \
                self.assertRaises(OSError):
            self.fence.activate(record)
        self.fence.restore()
        self.fence.restore()
        self.assertEqual(tree_record(self.old / "castanha"), record["package"])


if __name__ == "__main__":
    unittest.main()
