import inspect
import json
from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from castanha.deployment import (CAPTURE_OUTPUT_PREFIX, DeploymentError, LocalPlatform,
                                ProcessIdentity, assert_idle, assert_quiescent, capture_processes,
                                deploy, process_identity, recover, replace_link)
from castanha.engine import CastanhaEngine


def spawn_fake_capture(directory):
    """Fake do ffmpeg da captura: vive com o caminho de saída no argv."""
    script = directory / "fake_ffmpeg.py"
    script.write_text("import sys, time\ntime.sleep(60)\n")
    saida = directory / f"{CAPTURE_OUTPUT_PREFIX}fixture.ogg"
    return subprocess.Popen([sys.executable, str(script), "-f", "pulse", str(saida)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Reproduz a ordem real de castanha.engine.start_recording: lê o estado, gasta
# tempo fora de qualquer trava (no produto é o pactl de is_default_source_muted),
# sobe o ffmpeg e só então publica o PID. Com --lock ele toma a mesma trava do
# instalador, que é a mudança mínima proposta para engine.py.
START_FALSO = '''
import fcntl, json, os, subprocess, sys, time
from pathlib import Path

estado_dir, barreira = Path(sys.argv[1]), Path(sys.argv[2])


def nascer():
    if json.loads((estado_dir / "state.json").read_text()).get("status") != "idle":
        (barreira / "recusado-pelo-estado").write_text("1")
        return
    (barreira / "leu-ocioso").write_text("1")
    while not (barreira / "seguir").exists():
        time.sleep(0.01)
    saida = barreira / "castanha_rec_contraprova.ogg"
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(saida)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    temporario = estado_dir / "state.json.tmp"
    temporario.write_text(json.dumps({"status": "recording", "pid": filho.pid}))
    os.replace(temporario, estado_dir / "state.json")
    (barreira / "gravando").write_text(str(filho.pid))


if "--lock" in sys.argv:
    with (estado_dir / ".deployment.lock").open("a") as trava:
        try:
            fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            (barreira / "recusado-pela-trava").write_text("1")
            sys.exit(0)
        nascer()
else:
    nascer()
'''


class TestDeployment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "old"
        self.new = self.base / "new"
        for root in (self.old, self.new):
            (root / "bin").mkdir(parents=True)
            (root / "bin/castanha").write_text("fixture")
        self.state = self.base / "state"
        self.state.mkdir()
        (self.state / "state.json").write_text('{"status":"idle","pid":null}')
        self.cli = self.base / "cli"
        self.plugin = self.base / "plugin"
        self.cli.symlink_to(self.old / "bin/castanha")
        self.plugin.symlink_to(self.old)
        self.old_daemon = ProcessIdentity(100, 1, "boot", str(self.old), "/fixture/python", 1000)
        self.new_daemon = ProcessIdentity(101, 2, "boot", str(self.new), "/fixture/python", 1000)
        self.platform = Mock()
        self.platform.revision.return_value = "old-sha"
        self.platform.daemon.return_value = self.old_daemon
        self.platform.start.side_effect = [self.new_daemon, self.old_daemon]
        # A varredura real de /proc é exercitada nos testes de captura; aqui
        # ela fica determinística para não depender de outra sessão da VPS.
        self.scan_patch = patch("castanha.deployment.capture_processes", return_value=[])
        self.scan = self.scan_patch.start()
        self.scan_ativa = True
        self.addCleanup(self.usar_varredura_real)

    def usar_varredura_real(self):
        if self.scan_ativa:
            self.scan_patch.stop()
            self.scan_ativa = False

    def kill_later(self, process):
        def limpar():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(limpar)
        return process

    def matar_pid(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def esperar(self, marca, prazo=10):
        limite = time.monotonic() + prazo
        while not marca.exists():
            self.assertLess(time.monotonic(), limite, f"{marca.name} não apareceu")
            time.sleep(0.01)

    def esperar_em_proc(self, pid, prazo=10):
        limite = time.monotonic() + prazo
        while pid not in capture_processes():
            self.assertLess(time.monotonic(), limite, "Varredura não enxergou a captura")
            time.sleep(0.02)

    def spawn_start_falso(self, barreira, com_lock=False):
        script = self.base / "start_falso.py"
        script.write_text(START_FALSO)
        argumentos = [sys.executable, str(script), str(self.state), str(barreira)]
        return self.kill_later(subprocess.Popen(argumentos + (["--lock"] if com_lock else []),
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def daemon_fixture(self):
        """Daemon Castanha de verdade em diretório fake, nunca o daemon real."""
        package = self.new / "castanha"
        if not package.exists():
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "daemon.py").write_text("import time\ntime.sleep(30)\n")
        process = self.kill_later(subprocess.Popen(
            [sys.executable, "-m", "castanha.daemon"], cwd=self.new,
            env={**os.environ, "PYTHONPATH": str(self.new)}))
        limite = time.monotonic() + 5
        while True:
            try:
                return process, process_identity(process.pid)
            except DeploymentError:
                self.assertLess(time.monotonic(), limite, "Daemon fake não subiu")
                time.sleep(0.01)

    def run_deploy(self, **kwargs):
        return deploy(self.new, "fixture-sha", cli_link=self.cli, plugin_link=self.plugin,
                      state_dir=self.state, platform=self.platform, apply=True, **kwargs)

    def test_success_switches_both_links_and_preserves_original(self):
        result = self.run_deploy()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.cli.resolve(), self.new / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.new)
        self.assertTrue((self.old / "bin/castanha").exists())
        self.assertEqual(json.loads((self.state / "deployment.json").read_text())["phase"], "completed")

    def test_capture_active_refuses_before_stop_or_links(self):
        for state in ({"status": "recording"}, {"status": "paused"},
                      {"status": "processing"}, {"status": "idle", "processing_pid": 12}):
            with self.subTest(state=state):
                (self.state / "state.json").write_text(json.dumps(state))
                with self.assertRaises(DeploymentError):
                    self.run_deploy()
        self.platform.stop.assert_not_called()
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_pid_reused_after_preflight_refuses_to_signal(self):
        self.platform.daemon.side_effect = [self.old_daemon, self.new_daemon]
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        self.platform.stop.assert_not_called()

    def test_health_failure_rolls_back_both_links_and_daemon(self):
        self.platform.health.side_effect = [DeploymentError("fixture"), None]
        result = self.run_deploy()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.assertEqual([c.args[0] for c in self.platform.stop.call_args_list],
                         [self.old_daemon, self.new_daemon])
        self.assertEqual(self.platform.start.call_args.args[0], self.old)

    def test_partial_link_swap_rolls_back_without_losing_originals(self):
        calls = []

        def fail_second(path, target):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("fixture second link")
            replace_link(path, target)

        self.platform.start.side_effect = [self.old_daemon]
        with patch("castanha.deployment.replace_link", side_effect=fail_second):
            self.assertEqual(self.run_deploy()["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_failed_start_rolls_back_and_failed_rollback_is_not_success(self):
        self.platform.start.side_effect = [DeploymentError("new failed"), DeploymentError("old failed")]
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        journal = json.loads((self.state / "deployment.json").read_text())
        self.assertEqual(journal["phase"], "rollback_failed")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_interrupted_journal_refuses_overlapping_deployment(self):
        (self.state / "deployment.json").write_text('{"phase":"swapping"}')
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        self.platform.validate.assert_not_called()

    def test_corrupt_state_does_not_default_to_idle(self):
        (self.state / "state.json").write_text("corrupt")
        with self.assertRaises(DeploymentError):
            assert_idle(self.state)

    def test_non_link_installation_is_preserved(self):
        self.cli.unlink()
        self.cli.write_text("user-owned wrapper")
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        self.assertEqual(self.cli.read_text(), "user-owned wrapper")

    def test_dry_run_does_not_create_journal_or_stop_daemon(self):
        result = deploy(self.new, "fixture-sha", cli_link=self.cli, plugin_link=self.plugin,
                        state_dir=self.state, platform=self.platform)
        self.assertEqual(result["status"], "preflight_ok")
        self.assertFalse((self.state / "deployment.json").exists())
        self.platform.stop.assert_not_called()

    def test_recovery_restores_partial_links_after_abrupt_exit(self):
        def crash_on_second(path, target):
            if path == self.plugin:
                raise SystemExit("simulated process death")
            replace_link(path, target)
        with patch("castanha.deployment.replace_link", side_effect=crash_on_second):
            with self.assertRaises(SystemExit):
                self.run_deploy()
        self.assertEqual(self.cli.resolve(), self.new / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.platform.daemon.return_value = None
        self.platform.start.side_effect = [self.old_daemon]
        result = recover(cli_link=self.cli, plugin_link=self.plugin,
                         state_dir=self.state, platform=self.platform)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_recovery_does_not_overwrite_third_party_link(self):
        self.platform.health.side_effect = [DeploymentError("fixture"), None]
        self.run_deploy()
        plan = json.loads((self.state / "deployment.json").read_text())
        plan["phase"] = "checking"
        (self.state / "deployment.json").write_text(json.dumps(plan))
        other = self.base / "user-version"
        replace_link(self.plugin, other)
        with self.assertRaises(DeploymentError):
            recover(cli_link=self.cli, plugin_link=self.plugin,
                    state_dir=self.state, platform=self.platform)
        self.assertEqual(self.plugin.readlink(), other)

    def test_real_pidfd_only_stops_verified_owned_fixture(self):
        process, identity = self.daemon_fixture()
        platform = LocalPlatform(self.state)
        with self.assertRaises(DeploymentError):
            platform.stop(replace(identity, start_ticks=identity.start_ticks + 1))
        self.assertIsNone(process.poll(), "Identidade divergente não pode receber sinal")
        platform.stop(identity)
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)

    def test_identity_refuses_wrong_boot_cwd_exe_and_foreign_pid(self):
        process, identity = self.daemon_fixture()
        platform = LocalPlatform(self.state)
        for campo, divergente in (("boot_id", replace(identity, boot_id="outro-boot")),
                                  ("cwd", replace(identity, cwd=str(self.old))),
                                  ("exe", replace(identity, exe="/usr/bin/python-de-outro")),
                                  ("uid", replace(identity, uid=identity.uid + 1))):
            with self.subTest(campo=campo):
                with self.assertRaises(DeploymentError):
                    platform.stop(divergente)
        self.assertIsNone(process.poll(), "Nenhuma identidade divergente pode sinalizar")
        with self.assertRaises(DeploymentError):
            process_identity(os.getpid())  # o próprio teste não é daemon Castanha
        (self.state / "daemon.pid").write_text(str(process.pid))
        with self.assertRaises(DeploymentError):
            platform.daemon(self.old)  # pidfile vivo, cwd de outro checkout
        self.assertEqual(platform.daemon(self.new).pid, process.pid)

    def test_capture_process_is_visible_before_the_state_declares_it(self):
        """A janela do TOCTOU: ffmpeg vivo com state.json ainda ocioso."""
        self.usar_varredura_real()
        captura = self.kill_later(spawn_fake_capture(self.base))
        self.esperar_em_proc(captura.pid)
        assert_idle(self.state)  # o estado declarado continua mentindo ocioso
        with self.assertRaises(DeploymentError):
            assert_quiescent(self.state)
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        self.platform.stop.assert_not_called()
        self.assertEqual(self.plugin.resolve(), self.old)
        self.assertEqual(capture_processes(proc=self.base), [],
                         "Diretório sem processos não pode gerar falso positivo")

    def test_capture_born_between_the_two_links_rolls_the_pair_back(self):
        self.usar_varredura_real()
        self.platform.start.side_effect = [self.old_daemon]
        nascidas = []

        def nascer_no_plugin(path, target):
            if path == self.plugin and not nascidas:
                nascidas.append(self.kill_later(spawn_fake_capture(self.base)))
                self.esperar_em_proc(nascidas[0].pid)
            replace_link(path, target)

        with patch("castanha.deployment.replace_link", side_effect=nascer_no_plugin):
            resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertTrue(resultado["capture_running"], "A reversão precisa relatar a captura viva")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.assertEqual(self.platform.start.call_args.args[0], self.old)

    def test_state_written_after_stop_cancels_the_swap(self):
        """Escrita que mantém 'idle' passa por assert_idle; a digital pega."""
        def escrever_estado(path, target):
            replace_link(path, target)
            if path == self.cli:
                temporario = self.state / "state.json.tmp"
                temporario.write_text('{"status":"idle","pid":null,"next_meeting":"outra"}')
                os.replace(temporario, self.state / "state.json")

        self.platform.start.side_effect = [self.old_daemon]
        with patch("castanha.deployment.replace_link", side_effect=escrever_estado):
            resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_capture_declared_after_the_swap_still_gets_the_links_back(self):
        """Reverter com gravação viva é obrigatório: par meio trocado é pior."""
        def declarar_gravacao(path, target):
            replace_link(path, target)
            if path == self.plugin:
                temporario = self.state / "state.json.tmp"
                temporario.write_text('{"status":"recording","pid":4242}')
                os.replace(temporario, self.state / "state.json")

        self.platform.start.side_effect = [self.old_daemon]
        with patch("castanha.deployment.replace_link", side_effect=declarar_gravacao):
            resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.assertEqual(json.loads((self.state / "deployment.json").read_text())["phase"],
                         "rolled_back")

    def test_capture_declared_during_health_check_rolls_back(self):
        """Último ponto da janela: nascer durante o smoke também reverte."""
        chamadas = []

        def declarar_no_health(root, identidade):
            chamadas.append(root)
            if len(chamadas) == 1:
                temporario = self.state / "state.json.tmp"
                temporario.write_text('{"status":"recording","pid":4242}')
                os.replace(temporario, self.state / "state.json")

        self.platform.health.side_effect = declarar_no_health
        resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_recovery_restores_links_even_with_a_capture_running(self):
        def morrer_no_plugin(path, target):
            if path == self.plugin:
                raise SystemExit("morte simulada")
            replace_link(path, target)

        with patch("castanha.deployment.replace_link", side_effect=morrer_no_plugin):
            with self.assertRaises(SystemExit):
                self.run_deploy()
        (self.state / "state.json").write_text('{"status":"recording","pid":4242}')
        self.platform.daemon.return_value = None
        self.platform.start.side_effect = [self.old_daemon]
        resultado = recover(cli_link=self.cli, plugin_link=self.plugin,
                            state_dir=self.state, platform=self.platform)
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_link_pair_is_never_left_half_swapped(self):
        chamadas, falhar_em = {"n": 0}, {"link": 0}

        def talvez_falhar(path, target):
            chamadas["n"] += 1
            if chamadas["n"] == falhar_em["link"]:
                raise OSError("falha injetada no link")
            replace_link(path, target)

        def reiniciar():
            for caminho, alvo in ((self.cli, self.old / "bin/castanha"), (self.plugin, self.old)):
                caminho.unlink()
                caminho.symlink_to(alvo)
            (self.state / "deployment.json").unlink(missing_ok=True)
            (self.state / "state.json").write_text('{"status":"idle","pid":null}')
            self.platform.reset_mock(side_effect=True)
            self.platform.revision.return_value = "old-sha"
            self.platform.daemon.return_value = self.old_daemon
            self.platform.start.side_effect = [self.old_daemon, self.old_daemon]
            chamadas["n"], falhar_em["link"] = 0, 0

        with patch("castanha.deployment.replace_link", side_effect=talvez_falhar):
            for cenario in ("stop", "primeiro link", "segundo link", "start", "health"):
                with self.subTest(cenario=cenario):
                    reiniciar()
                    falhar_em["link"] = {"primeiro link": 1, "segundo link": 2}.get(cenario, 0)
                    if cenario == "stop":
                        self.platform.stop.side_effect = DeploymentError("stop falhou")
                    if cenario == "start":
                        self.platform.start.side_effect = [DeploymentError("start falhou"),
                                                           self.old_daemon]
                    if cenario == "health":
                        self.platform.health.side_effect = [DeploymentError("health falhou"), None]
                    try:
                        self.run_deploy()
                    except DeploymentError:
                        pass  # falha honesta também precisa deixar o par coerente
                    self.assertEqual(self.cli.resolve().parent.parent, self.plugin.resolve(),
                                     f"par meio trocado em {cenario}")
                    self.assertEqual(self.plugin.resolve(), self.old)

    def test_capture_decided_before_the_install_is_the_residual_hole(self):
        """Contraprova: decisão -> Popen não é observável, e o instalador conclui.

        É a única janela que sobra e ela só fecha em castanha/engine.py, fora
        deste ownership. Ver docs/INSTALACAO-REVERSIVEL.md.
        """
        self.usar_varredura_real()
        self.platform.start.side_effect = [self.new_daemon]
        barreira = self.base / "barreira"
        barreira.mkdir()
        captura = self.spawn_start_falso(barreira)
        self.esperar(barreira / "leu-ocioso")
        resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "ok", "Nada observável: a instalação conclui")
        self.assertEqual(self.plugin.resolve(), self.new)
        (barreira / "seguir").write_text("1")
        self.esperar(barreira / "gravando")
        captura.wait(timeout=10)
        filho = int((barreira / "gravando").read_text())
        self.addCleanup(self.matar_pid, filho)
        self.assertEqual(json.loads((self.state / "state.json").read_text())["status"], "recording")
        self.assertIn(filho, capture_processes(), "A gravação nasceu sobre a instalação nova")

    def test_capture_holding_the_shared_lock_makes_the_install_refuse_honestly(self):
        """Remédio, ordem 1: captura com a trava, instalação recusa sem traceback."""
        self.usar_varredura_real()
        barreira = self.base / "barreira"
        barreira.mkdir()
        captura = self.spawn_start_falso(barreira, com_lock=True)
        self.esperar(barreira / "leu-ocioso")
        with self.assertRaises(DeploymentError) as erro:
            self.run_deploy()
        self.assertIn("andamento", str(erro.exception))
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.platform.stop.assert_not_called()
        (barreira / "seguir").write_text("1")
        self.esperar(barreira / "gravando")
        captura.wait(timeout=10)
        self.addCleanup(self.matar_pid, int((barreira / "gravando").read_text()))

    def test_install_holding_the_shared_lock_would_refuse_a_lock_aware_capture(self):
        """Remédio, ordem 2: com a trava tomada no start, a captura não nasce."""
        barreira = self.base / "barreira"
        barreira.mkdir()

        def tentar_gravar(identidade):
            self.spawn_start_falso(barreira, com_lock=True).wait(timeout=10)

        self.platform.stop.side_effect = tentar_gravar
        self.platform.start.side_effect = [self.new_daemon]
        self.assertEqual(self.run_deploy()["status"], "ok")
        self.assertTrue((barreira / "recusado-pela-trava").exists(),
                        "A captura precisa esbarrar na trava do instalador")
        self.assertFalse((barreira / "gravando").exists())
        self.assertFalse((barreira / "leu-ocioso").exists())

    def test_detector_stays_coupled_to_the_capture_output_name(self):
        self.assertIn(CAPTURE_OUTPUT_PREFIX, inspect.getsource(CastanhaEngine.start_recording),
                      "Mudou o nome do arquivo de captura: a varredura de /proc fica cega")


if __name__ == "__main__":
    unittest.main()
