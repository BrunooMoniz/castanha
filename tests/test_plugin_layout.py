"""Prova o layout com o Button real do shell do Omarchy."""
import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")


def _command_output(command, timeout=5):
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return subprocess.CompletedProcess(command, 124, output + "command timed out")


def _tone_command(sink_name):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=997:sample_rate=48000:duration=2",
        "-f", "pulse", "-device", sink_name, "castanha-peak-test",
    ]


def _castanha_is_idle():
    executable = shutil.which("castanha")
    if not executable:
        return False
    result = _command_output([executable, "status", "--json"])
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout).get("status") == "idle"
    except json.JSONDecodeError:
        return False


@contextmanager
def _isolated_null_sink(name):
    default_sink = _command_output(["pactl", "get-default-sink"])
    if default_sink.returncode != 0:
        raise RuntimeError(default_sink.stdout)

    module_ids = set()
    cleaned = False

    def discover_module_ids():
        modules = _command_output(["pactl", "list", "modules", "short"])
        if modules.returncode != 0:
            return None
        expected_argument = f"sink_name={name}"
        found = []
        for line in modules.stdout.splitlines():
            fields = line.split()
            if (len(fields) >= 3 and fields[1] == "module-null-sink"
                    and expected_argument in fields[2:]):
                found.append(fields[0])
        return found

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return True
        for _ in range(3):
            discovered = discover_module_ids()
            if discovered is None:
                time.sleep(0.1)
                continue
            targets = module_ids.union(discovered)
            if not targets:
                cleaned = True
                return True
            for target in targets:
                _command_output(["pactl", "unload-module", target])
            remaining = discover_module_ids()
            if remaining == []:
                cleaned = True
                return True
            time.sleep(0.1)
        return False

    previous_handlers = {}

    def cleanup_on_signal(signum, frame):
        cleanup()
        previous = previous_handlers[signum]
        if callable(previous):
            previous(signum, frame)
        raise SystemExit(128 + signum)

    atexit.register(cleanup)
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, cleanup_on_signal)
    try:
        loaded = _command_output([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={name}",
            "sink_properties=device.description=CastanhaPeakTest",
            "rate=48000",
            "channels=2",
        ])
        if loaded.returncode != 0:
            raise RuntimeError(loaded.stdout)
        module_id = loaded.stdout.strip()
        if not module_id:
            raise RuntimeError("pactl não retornou o id do módulo PipeWire")
        module_ids.add(module_id)
        yield default_sink.stdout.strip()
    finally:
        cleanup_succeeded = cleanup()
        if cleanup_succeeded:
            atexit.unregister(cleanup)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if not cleanup_succeeded:
            raise RuntimeError(f"não foi possível descarregar o sink PipeWire {name}")


class PluginLayoutTest(unittest.TestCase):
    def test_uncertain_sink_creation_is_cleaned_up_by_name(self):
        responses = iter([
            subprocess.CompletedProcess([], 0, "default-sink\n"),
            subprocess.CompletedProcess([], 124, "command timed out"),
            subprocess.CompletedProcess(
                [], 0,
                "55\tmodule-null-sink\tsink_name=castanha_peak_fixture rate=48000\n",
            ),
            subprocess.CompletedProcess([], 0, ""),
            subprocess.CompletedProcess([], 0, ""),
        ])
        commands = []

        def fake_command(command, timeout=5):
            commands.append(command)
            return next(responses)

        with mock.patch(f"{__name__}._command_output", side_effect=fake_command):
            with self.assertRaisesRegex(RuntimeError, "command timed out"):
                with _isolated_null_sink("castanha_peak_fixture"):
                    self.fail("a criação incerta não pode chegar ao corpo do contexto")

        self.assertIn(["pactl", "unload-module", "55"], commands)

    def test_tone_command_targets_the_isolated_sink(self):
        command = _tone_command("castanha_peak_fixture")
        device_index = command.index("-device")
        self.assertEqual(command[device_index + 1], "castanha_peak_fixture")
        self.assertEqual(command[-1], "castanha-peak-test")

    def test_panel_wires_meter_only_while_recording(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn('RecordingAudioMeter {', panel)
        self.assertIn('recording: root.isRecording', panel)
        self.assertIn('mode: root.mode', panel)
        self.assertIn('micSource: root.micSource', panel)
        self.assertIn('systemSink: root.systemSink', panel)
        self.assertIn('micMuted: root.micMuted', panel)
        self.assertIn(
            'if (isRecording) return glyph + "  " + formatTime(elapsedSeconds) + "  " + audioMeter.text',
            panel,
        )
        self.assertIn(
            'if (isPaused) return glyph + "  " + formatTime(elapsedSeconds)',
            panel,
        )

    def test_real_sink_peak_reaches_recording_meter_without_changing_defaults(self):
        required = ("pactl", "ffmpeg", "quickshell")
        missing = [command for command in required if not shutil.which(command)]
        if missing:
            self.skipTest("dependências PipeWire indisponíveis: " + ", ".join(missing))
        if not _castanha_is_idle():
            self.skipTest("Castanha não está idle; smoke não toca no grafo durante gravação")

        sink_name = f"castanha_peak_test_{os.getpid()}"
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "AudioMeter.qml").symlink_to(ROOT / "AudioMeter.qml")
            (config / "AudioMeter.js").symlink_to(ROOT / "AudioMeter.js")
            (config / "RecordingAudioMeter.qml").symlink_to(ROOT / "RecordingAudioMeter.qml")
            shutil.copy2(ROOT / "tests/fixtures/pipewire_peak_shell.qml", config / "shell.qml")

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment["CASTANHA_TEST_SINK"] = sink_name
            environment.pop("WAYLAND_DISPLAY", None)

            with _isolated_null_sink(sink_name) as original_default:
                shell = subprocess.Popen(
                    ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=environment,
                )
                output = ""
                try:
                    time.sleep(0.8)
                    if not _castanha_is_idle():
                        self.skipTest("Castanha deixou idle; smoke cancelado antes do tom")
                    tone = _command_output(_tone_command(sink_name), timeout=5)
                    self.assertEqual(tone.returncode, 0, tone.stdout)
                    output, _ = shell.communicate(timeout=9)
                finally:
                    if shell.poll() is None:
                        shell.terminate()
                        try:
                            shell.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            shell.kill()
                            shell.wait(timeout=2)

                self.assertEqual(shell.returncode, 0, output)
                self.assertIn("CASTANHA_PIPEWIRE_PEAK_OK", output)
                self.assertNotIn("CASTANHA_PIPEWIRE_PEAK_FAIL", output)
                current_default = _command_output(["pactl", "get-default-sink"])
                self.assertEqual(current_default.returncode, 0, current_default.stdout)
                self.assertEqual(current_default.stdout.strip(), original_default)

            sinks_after_cleanup = _command_output(["pactl", "list", "sinks", "short"])
            self.assertEqual(sinks_after_cleanup.returncode, 0, sinks_after_cleanup.stdout)
            self.assertNotIn(sink_name, sinks_after_cleanup.stdout)
            default_after_cleanup = _command_output(["pactl", "get-default-sink"])
            self.assertEqual(default_after_cleanup.returncode, 0, default_after_cleanup.stdout)
            self.assertEqual(default_after_cleanup.stdout.strip(), original_default)

    def test_audio_meter_compiles_with_real_pipewire_monitor(self):
        if not shutil.which("quickshell"):
            self.skipTest("quickshell não instalado")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
            (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
            (config / "AudioMeter.qml").symlink_to(ROOT / "AudioMeter.qml")
            (config / "AudioMeter.js").symlink_to(ROOT / "AudioMeter.js")
            (config / "RecordingAudioMeter.qml").symlink_to(ROOT / "RecordingAudioMeter.qml")
            shutil.copy2(ROOT / "tests/fixtures/audio_meter_shell.qml", config / "shell.qml")

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("WAYLAND_DISPLAY", None)
            result = subprocess.run(
                ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CASTANHA_AUDIO_METER_OK", result.stdout)
        self.assertNotIn("CASTANHA_AUDIO_METER_FAIL", result.stdout)

    def test_real_omarchy_buttons_stay_inside_panel(self):
        if not shutil.which("quickshell"):
            self.skipTest("quickshell não instalado")
        if not (OMARCHY_SHELL / "Ui").is_dir() or not (OMARCHY_SHELL / "Commons").is_dir():
            self.skipTest("componentes do shell do Omarchy indisponíveis")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            (config / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
            (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
            (config / "PanelActionFlow.qml").symlink_to(ROOT / "PanelActionFlow.qml")
            (config / "i18n.js").symlink_to(ROOT / "i18n.js")
            shutil.copy2(ROOT / "tests/fixtures/real_button_layout_shell.qml", config / "shell.qml")

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("WAYLAND_DISPLAY", None)
            result = subprocess.run(
                ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CASTANHA_REAL_LAYOUT_OK", result.stdout)
        self.assertNotIn("CASTANHA_REAL_LAYOUT_FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
