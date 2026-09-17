"""Microfone sem eco: a gravação nunca depende do nó, e o nó morre com a captura."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from castanha import echo_cancel
from castanha.audio import AudioDeviceInfo, AudioRecorder


class FakeNode:
    def __init__(self):
        self.source, self.bound, self.closed = "castanha_mic_aec_fixture", None, False

    def bind(self, pid):
        self.bound = pid
        return True

    def close(self):
        self.closed = True


def _recorder():
    recorder = AudioRecorder()
    recorder.devices = AudioDeviceInfo("mic", "sink", "sink.monitor")
    return recorder


def _fake_ffmpeg(commands):
    real = subprocess.Popen

    def popen(cmd, **kwargs):
        commands.append(cmd[:])
        return real([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    return popen


def _inputs(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]


class RecorderUsesEchoCancel(unittest.TestCase):
    def _start(self, node, **kwargs):
        commands = []
        recorder = _recorder()
        with tempfile.TemporaryDirectory() as directory, \
                patch("castanha.audio.echo_cancel_node.start", return_value=node) as start, \
                patch("castanha.audio.subprocess.Popen", side_effect=_fake_ffmpeg(commands)):
            try:
                process = recorder.start(Path(directory) / "capture.ogg", **kwargs)
            finally:
                if recorder.process:
                    recorder.process.kill()
                    recorder.process.wait()
                    recorder.process.stdin.close()
        self.recorder = recorder
        return commands, start, process

    def test_dual_grava_a_fonte_sem_eco_e_amarra_o_no_a_captura(self):
        node = FakeNode()
        commands, start, process = self._start(node, mode="dual", echo_cancel=True)
        self.assertIs(self.recorder.echo, node)
        start.assert_called_once_with("mic", "sink")
        self.assertEqual(_inputs(commands[0]), [node.source, "sink.monitor"])
        self.assertEqual(node.bound, process.pid)

    def test_no_indisponivel_grava_o_microfone_cru(self):
        commands, _, _ = self._start(None, mode="dual", echo_cancel=True)
        self.assertEqual(_inputs(commands[0]), ["mic", "sink.monitor"])

    def test_presencial_e_desligado_nao_sobem_no(self):
        for kwargs in ({"mode": "mic_only", "echo_cancel": True}, {"mode": "dual", "echo_cancel": False}):
            commands, start, _ = self._start(FakeNode(), **kwargs)
            start.assert_not_called()
            self.assertEqual(_inputs(commands[0])[0], "mic")

    def test_supervisor_que_ja_saiu_nao_deixa_o_estado_dizer_sem_eco(self):
        node = FakeNode()
        node.bind = lambda pid: False
        recorder = _recorder()
        with tempfile.TemporaryDirectory() as directory, \
                patch("castanha.audio.echo_cancel_node.start", return_value=node), \
                patch("castanha.audio.subprocess.Popen", side_effect=_fake_ffmpeg([])):
            try:
                recorder.start(Path(directory) / "capture.ogg", mode="dual", echo_cancel=True)
                self.assertIsNone(recorder.echo)
            finally:
                recorder.process.kill()
                recorder.process.wait()
                recorder.process.stdin.close()

    def test_ffmpeg_que_nao_inicia_encerra_o_no(self):
        node = FakeNode()
        recorder = _recorder()
        with tempfile.TemporaryDirectory() as directory, \
                patch("castanha.audio.echo_cancel_node.start", return_value=node), \
                patch("castanha.audio.subprocess.Popen", side_effect=OSError("sem ffmpeg")):
            with self.assertRaises(OSError):
                recorder.start(Path(directory) / "capture.ogg", mode="dual", echo_cancel=True)
        self.assertTrue(node.closed)


class Supervisor(unittest.TestCase):
    """Roda o supervisor real com um `pipewire` de mentira no PATH."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        fake = Path(self.dir.name) / "pipewire"
        self.node_pid = Path(self.dir.name) / "node.pid"
        fake.write_text(f"#!/bin/sh\necho $$ > {self.node_pid}\nexec sleep 60\n")
        fake.chmod(0o755)
        self.conf = Path(self.dir.name) / "aec.conf"
        self.conf.write_text("conf")
        env = dict(os.environ, PATH=f"{self.dir.name}:{os.environ['PATH']}")
        self.supervisor = subprocess.Popen(
            [sys.executable, "-B", echo_cancel.__file__, str(self.conf)],
            stdin=subprocess.PIPE, env=env)
        self.addCleanup(self._reap)

    def _reap(self):
        self.supervisor.kill()
        self.supervisor.wait()
        if self.node_pid.exists():
            try:
                os.kill(int(self.node_pid.read_text()), 9)
            except (OSError, ValueError):
                pass

    def _wait(self, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def _node_gone(self):
        return not echo_cancel._alive(int(self.node_pid.read_text()))

    def test_no_termina_quando_a_captura_termina(self):
        capture = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(capture.kill)
        self.assertTrue(self._wait(self.node_pid.exists))
        echo_cancel.EchoCancel("fonte", self.supervisor).bind(capture.pid)
        time.sleep(1.5)
        self.assertFalse(self._node_gone(), "nó caiu com a captura ainda viva")
        capture.kill()
        capture.wait()
        self.assertTrue(self._wait(self._node_gone))
        self.assertEqual(self.supervisor.wait(timeout=5), 0)
        self.assertFalse(self.conf.exists())

    def test_quem_iniciou_morre_sem_entregar_o_pid(self):
        self.assertTrue(self._wait(self.node_pid.exists))
        self.supervisor.stdin.close()
        self.assertTrue(self._wait(self._node_gone))

    def test_close_encerra_o_no(self):
        self.assertTrue(self._wait(self.node_pid.exists))
        echo_cancel.EchoCancel("fonte", self.supervisor).close()
        self.assertTrue(self._wait(self._node_gone))


class StartSemNo(unittest.TestCase):
    """Host sem o nó (binário ausente ou nó que cai na hora): mic cru, sem lixo e sem espera."""

    def _start(self, fake_pipewire):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "run"
            runtime.mkdir()
            bindir = Path(directory) / "bin"
            bindir.mkdir()
            if fake_pipewire:
                (bindir / "pipewire").write_text(fake_pipewire)
                (bindir / "pipewire").chmod(0o755)
            env = {"XDG_RUNTIME_DIR": str(runtime), "PATH": str(bindir)}
            began = time.monotonic()
            with patch.dict(os.environ, env):
                node = echo_cancel.start("mic", "sink")
            return node, time.monotonic() - began, list(runtime.glob('castanha-aec-*'))

    def test_pipewire_ausente_nao_vaza_a_conf(self):
        node, _, leftovers = self._start(None)
        self.assertIsNone(node)
        self.assertEqual(leftovers, [])

    def test_no_que_cai_na_hora_libera_o_start_sem_esperar_o_teto(self):
        node, elapsed, leftovers = self._start("#!/bin/sh\nexit 1\n")
        self.assertIsNone(node)
        self.assertEqual(leftovers, [])
        self.assertLess(elapsed, echo_cancel.READY_TIMEOUT_SECONDS - 1)


class Conf(unittest.TestCase):
    def test_nome_de_dispositivo_que_quebraria_a_sintaxe_e_recusado(self):
        with self.assertRaises(ValueError):
            echo_cancel.render_conf("no", 'mic" } evil = {', "sink")


if __name__ == "__main__":
    unittest.main()
