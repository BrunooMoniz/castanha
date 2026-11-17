"""Prova o layout com o Button real do shell do Omarchy."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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


class PluginLayoutTest(unittest.TestCase):
    def test_panel_wires_meter_only_while_recording(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn('RecordingAudioMeter {', panel)
        self.assertIn('recording: root.isRecording', panel)
        self.assertIn('mode: root.mode', panel)
        self.assertIn('audioPeak: root.audioPeakFresh ? Number(stateData.audio_peak) : 0', panel)
        self.assertIn('AudioMeterLogic.isFresh(audioPeakUpdatedAt, nowMs, isRecording)', panel)
        self.assertIn('micMuted: root.micMuted', panel)
        self.assertIn(
            'if (isRecording) return glyph + "  " + formatTime(elapsedSeconds) + "  " + audioMeter.text',
            panel,
        )
        self.assertIn(
            'if (isPaused) return glyph + "  " + formatTime(elapsedSeconds)',
            panel,
        )

    def test_real_capture_sidecar_reports_peak_without_changing_defaults(self):
        required = ("pactl", "ffmpeg")
        missing = [command for command in required if not shutil.which(command)]
        if missing:
            self.skipTest("dependências PipeWire indisponíveis: " + ", ".join(missing))
        if not _castanha_is_idle():
            self.skipTest("Castanha não está idle; smoke não toca no grafo durante gravação")
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-B", str(ROOT / "tests" / "qa_pipewire_capture.py")],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('"status": "pass"', result.stdout)

    def test_audio_meter_compiles_without_pipewire_peak_monitor(self):
        if not os.environ.get("WAYLAND_DISPLAY"):
            self.skipTest("componentes do shell do Omarchy exigem backend Wayland")
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

            # Wayland, não offscreen: os componentes do shell do Omarchy passaram
            # a puxar GTK, que aborta com "cannot open display" no offscreen.
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "wayland"
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
        if not os.environ.get("WAYLAND_DISPLAY"):
            self.skipTest("componentes do shell do Omarchy exigem backend Wayland")
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

            # Wayland, não offscreen: os componentes do shell do Omarchy passaram
            # a puxar GTK, que aborta com "cannot open display" no offscreen.
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "wayland"
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

    def test_panel_compiles_and_wires_rename_and_delete(self):
        """O Panel real, com os componentes reais e um acervo real de uma nota.

        KeyboardPanel é um PanelWindow: sem backend Wayland ele não carrega, e
        por isso este é o único teste de QML que não roda offscreen. O painel
        usa uma barra layer-shell isolada, sem reservar espaço nem receber
        teclado/cliques da sessão real.

        O acervo é temporário e apontado por XDG_CONFIG_HOME: a nota que o
        painel lista vem do `castanha notes --json` de verdade, e não de uma
        atribuição em recentNotes — que o próprio Process do painel sobrescreve
        quando responde.
        """
        if not shutil.which("quickshell"):
            self.skipTest("quickshell não instalado")
        if not (OMARCHY_SHELL / "Ui").is_dir() or not (OMARCHY_SHELL / "Commons").is_dir():
            self.skipTest("componentes do shell do Omarchy indisponíveis")
        if not os.environ.get("WAYLAND_DISPLAY"):
            self.skipTest("KeyboardPanel exige backend Wayland; nenhum WAYLAND_DISPLAY")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "qml"
            config.mkdir()
            (config / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
            (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
            for source in [*ROOT.glob("*.qml"), *ROOT.glob("*.js")]:
                if source.name != "Panel.qml":
                    (config / source.name).symlink_to(source)
            # O tipo QML vem do nome do arquivo, e "Panel" colidiria com o
            # Panel.qml do próprio shell, que é a base do nosso.
            (config / "CastanhaPanel.qml").symlink_to(ROOT / "Panel.qml")
            shutil.copy2(ROOT / "tests/fixtures/panel_compile_shell.qml", config / "shell.qml")

            acervo = self._acervo_de_uma_nota(Path(temporary))

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "wayland"
            environment["XDG_CONFIG_HOME"] = str(acervo["xdg_config"])
            environment["XDG_STATE_HOME"] = str(acervo["xdg_state"])
            shim_bin = Path(temporary) / "retry-shim"
            shim_bin.mkdir()
            shim = shim_bin / "castanha"
            shim.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
if len(sys.argv) > 1 and sys.argv[1] == 'retry':
    marker = pathlib.Path(os.environ['CASTANHA_TEST_RETRY_CALLS'])
    count = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(count + 1))
    responses = [({'status': 'error', 'message': 'Falha sintética de retry'}, 1),
                 ({'status': 'ok', 'results': [{'status': 'pending'}]}, 0),
                 ({'status': 'empty'}, 0),
                 ({'status': 'ok', 'message': 'Sucesso inventado'}, 1)]
    output, code = responses[count]
    print(json.dumps(output)); sys.exit(code)
os.execv(os.environ['CASTANHA_TEST_REAL_CLI'], ['castanha', *sys.argv[1:]])
""")
            shim.chmod(0o700)
            environment["CASTANHA_TEST_RETRY_CALLS"] = str(Path(temporary) / "retry-count")
            environment["CASTANHA_TEST_REAL_CLI"] = str(ROOT / "bin/castanha")
            environment["CASTANHA_TEST_AUDIO"] = str(Path(temporary) / "meetings/bronze" / acervo["slug"] / "second.wav")
            environment["PATH"] = f"{shim_bin}:{ROOT / 'bin'}:{environment.get('PATH', '')}"
            result = subprocess.run(
                ["quickshell", "--no-duplicate", "--path", str(config / "shell.qml"), "--no-color"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=60,
                check=False,
            )

            self.assertEqual((Path(temporary) / "retry-count").read_text(), "4", result.stdout)
            note_dir = acervo["xdg_config"].parent / "meetings" / "bronze" / acervo["slug"]
            self.assertFalse((note_dir / "first.wav").exists(), result.stdout)
            self.assertTrue((note_dir / "second.wav").exists(), result.stdout)
            self.assertFalse((note_dir / "transcript_raw.txt").exists())
            self.assertFalse((note_dir.parent.parent / "silver" / (acervo["slug"] + ".md")).exists())
            metadata = json.loads((note_dir / "metadata.json").read_text())
            self.assertEqual(metadata['content_status'], 'invalidated')
            archive = note_dir / '.recording-exclusions' / metadata['exclusion_id']
            operation = json.loads((archive / 'operation.json').read_text())
            import hashlib
            self.assertEqual(hashlib.sha256((archive / 'audio').read_bytes()).hexdigest(), operation['audio_sha256'])
            transcript = next(item for item in operation['files'] if item['parts'] == ['transcript_raw.txt'])
            self.assertEqual((archive / transcript['backup']).read_text(), 'Texto.')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CASTANHA_PANEL_OK", result.stdout)
        self.assertNotIn("CASTANHA_PANEL_FAIL", result.stdout)
        self.assertNotIn("Unable to assign [undefined]", result.stdout)
        self.assertNotIn("Cannot assign [undefined]", result.stdout)

    def _acervo_de_uma_nota(self, base):
        """Config + acervo com exatamente uma reunião, para o painel listar."""
        xdg_config = base / "config"
        xdg_state = base / "state"
        meetings = base / "meetings"
        bronze, silver, gold = meetings / "bronze", meetings / "silver", meetings / "gold"
        for d in (bronze, silver, gold, xdg_state):
            d.mkdir(parents=True, exist_ok=True)
        # O app inicializado já tem o diretório da trava compartilhada entre
        # captura/deploy/exclusão. Use o bootstrap real, sem contornar a trava.
        from castanha.state import StateManager
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_state)}):
            StateManager()

        cfg_dir = xdg_config / "castanha"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({
            "storage": {"base_dir": str(meetings), "bronze_dir": str(bronze),
                        "silver_dir": str(silver), "gold_dir": str(gold)},
            # Nada de rede: o painel só lista, e a agenda fica quieta.
            "calendar": {"enabled": False},
            "zinom": {"enabled": False},
        }), encoding="utf-8")

        slug = "2026-01-01_1000_reuniao-de-teste"
        (bronze / slug).mkdir(parents=True, exist_ok=True)
        (bronze / slug / "transcript_raw.txt").write_text("Texto.", encoding="utf-8")
        (bronze / slug / "metadata.json").write_text(json.dumps({
            "title": "Reunião de Teste", "recorded_at": "2026-01-01T10:00:00",
            "duration_seconds": 600, "mode": "dual", "audio_status": "ok",
        }), encoding="utf-8")
        import wave
        for name in ("first.wav", "second.wav"):
            with wave.open(str(bronze / slug / name), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * 16000)
        (silver / f"{slug}.md").write_text("# Reunião de Teste\n", encoding="utf-8")
        return {"xdg_config": xdg_config, "xdg_state": xdg_state, "slug": slug}

    def test_panel_offers_rename_and_delete_on_right_click(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        # Botão direito na linha abre o menu, e não expande a nota.
        self.assertIn("acceptedButtons: Qt.LeftButton | Qt.RightButton", panel)
        self.assertIn("if (mouse.button === Qt.RightButton) root.openContext(slug)", panel)
        # Apagar exige o segundo clique.
        self.assertIn("if (noteRow.apagarArmado) root.deleteMeeting(slug)", panel)
        self.assertIn('else root.deleteArmedSlug = slug', panel)
        # Digitar num campo não pode acionar os atalhos do painel ("x" apaga).
        self.assertIn("blocked: root.textEditing", panel)


if __name__ == "__main__":
    unittest.main()
