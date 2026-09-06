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

from castanha import deployment
from castanha.deployment import (BLOCKER_EXIT, BLOCKER_NAME, CAPTURE_OUTPUT_PREFIX, DeploymentError,
                                LegacyTransitionBlocked, LocalPlatform, ProcessIdentity, assert_idle, assert_lock_aware,
                                assert_quiescent, capture_processes, capture_running, deploy,
                                legacy_processes, process_identity, recover, replace_link, settle)
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
# sobe o ffmpeg e só então publica o PID. Roda como o CLI legado roda de
# verdade, pelo caminho que o painel e o atalho de teclado chamam, para que o
# argv carregue a mesma assinatura que o inventário procura. Com --lock ele toma
# a trava do instalador, que é a mudança mínima proposta para engine.py;
# com --desistir sai sem gravar; com --atraso=N não espera barreira, dorme.
CLI_LEGADO = '''
import fcntl, json, os, subprocess, sys, time
from pathlib import Path

estado_dir, barreira = Path(sys.argv[1]), Path(sys.argv[2])
opcoes = sys.argv[3:]


def liberar():
    atraso = next((o.split("=", 1)[1] for o in opcoes if o.startswith("--atraso=")), None)
    if atraso is not None:
        time.sleep(float(atraso))
        return
    while not (barreira / "seguir").exists():
        time.sleep(0.01)


def nascer():
    if json.loads((estado_dir / "state.json").read_text()).get("status") != "idle":
        (barreira / "recusado-pelo-estado").write_text("1")
        return
    (barreira / "leu-ocioso").write_text("1")
    liberar()
    if "--desistir" in opcoes:
        (barreira / "desistiu").write_text("1")
        return
    saida = barreira / "castanha_rec_contraprova.ogg"
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(saida)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    temporario = estado_dir / "state.json.tmp"
    temporario.write_text(json.dumps({"status": "recording", "pid": filho.pid}))
    os.replace(temporario, estado_dir / "state.json")
    (barreira / "gravando").write_text(str(filho.pid))


if "--lock" in opcoes:
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
        # A varredura real de /proc é exercitada nos testes de captura e de
        # legado; aqui ela fica determinística para não depender de outra sessão
        # da VPS, e o inventário roda com prazos curtos para o teste ser leve.
        self.scan_patches = [patch("castanha.deployment.capture_processes", return_value=[]),
                             patch("castanha.deployment.legacy_processes", return_value=[])]
        self.scan, self.scan_legado = [p.start() for p in self.scan_patches]
        self.scan_ativa = True
        self.addCleanup(self.usar_varredura_real)
        self.settle_rapido()

    def settle_rapido(self, timeout=10.0, clean_scans=2, interval=0.0):
        for nome, valor in (("SETTLE_TIMEOUT", timeout), ("SETTLE_CLEAN_SCANS", clean_scans),
                            ("SETTLE_INTERVAL", interval)):
            ajuste = patch.object(deployment, nome, valor)
            ajuste.start()
            self.addCleanup(ajuste.stop)

    def usar_varredura_real(self):
        if self.scan_ativa:
            for p in self.scan_patches:
                p.stop()
            self.scan_ativa = False

    def blocker(self):
        return self.state / BLOCKER_NAME

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

    def spawn_cli_legado(self, barreira, *opcoes, entrypoint=None, cwd=None):
        """O CLI antigo em voo, com o argv que o painel e o atalho produzem.

        O painel chama `castanha` pelo PATH, então argv carrega o caminho do
        link; um terminal chama o script do checkout. As duas assinaturas são o
        que o inventário procura, e é por elas que o legado deixa de ser
        invisível entre a decisão de gravar e o Popen do ffmpeg.
        """
        (self.old / "bin/castanha").write_text(CLI_LEGADO)
        caminho = entrypoint if entrypoint is not None else self.cli
        return self.kill_later(subprocess.Popen(
            [sys.executable, str(caminho), str(self.state), str(barreira), *opcoes],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=cwd))

    def barreira(self):
        caminho = self.base / "barreira"
        caminho.mkdir(exist_ok=True)
        return caminho

    def espiar_links(self):
        """Registra o alvo de cada troca de link, sem impedir nenhuma."""
        trocas = []

        def registrar(path, target):
            trocas.append((path, Path(target)))
            replace_link(path, target)

        remendo = patch("castanha.deployment.replace_link", side_effect=registrar)
        remendo.start()
        self.addCleanup(remendo.stop)
        return trocas

    def assert_candidato_nunca_exposto(self, trocas):
        """Nenhum link chegou a apontar para o candidato: recusa, não reversão."""
        expostos = [str(alvo) for _, alvo in trocas if str(alvo).startswith(str(self.new))]
        self.assertEqual(expostos, [], "O candidato não podia ter sido exposto")

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
        # Morte durante a transição deixa o entrypoint no bloqueador, nunca num
        # CLI novo solto com o plugin velho ao lado.
        self.assertEqual(self.cli.resolve(), self.blocker())
        self.assertEqual(self.plugin.resolve(), self.old)
        self.platform.daemon.return_value = None
        self.platform.start.side_effect = [self.old_daemon]
        result = recover(cli_link=self.cli, plugin_link=self.plugin,
                         state_dir=self.state, platform=self.platform)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_recovery_resumes_its_restarted_daemon_after_health_interruption(self):
        self.platform.health.side_effect = SystemExit("interrupted candidate health")
        with self.assertRaises(SystemExit):
            self.run_deploy()
        restored = replace(self.old_daemon, pid=102, start_ticks=3)
        self.platform.daemon.return_value = None
        self.platform.start.side_effect = [restored]
        self.platform.health.side_effect = SystemExit("interrupted rollback health")
        with self.assertRaises(SystemExit):
            recover(cli_link=self.cli, plugin_link=self.plugin,
                    state_dir=self.state, platform=self.platform)
        plan = json.loads((self.state / "deployment.json").read_text())
        self.assertEqual(plan["phase"], "rollback_checking")
        self.assertEqual(plan["restored_daemon"]["pid"], restored.pid)
        self.platform.daemon.side_effect = [DeploymentError("not candidate"), restored]
        self.platform.health.side_effect = None
        starts = self.platform.start.call_count
        stops = self.platform.stop.call_count
        result = recover(cli_link=self.cli, plugin_link=self.plugin,
                         state_dir=self.state, platform=self.platform)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.platform.start.call_count, starts)
        self.assertEqual(self.platform.stop.call_count, stops)

    def test_internal_rollback_records_identity_before_failed_health(self):
        restored = replace(self.old_daemon, pid=102, start_ticks=3)
        self.platform.start.side_effect = [self.new_daemon, restored]
        self.platform.health.side_effect = DeploymentError("unhealthy")
        with self.assertRaises(DeploymentError):
            self.run_deploy()
        plan = json.loads((self.state / "deployment.json").read_text())
        self.assertEqual(plan["phase"], "rollback_failed")
        self.assertEqual(plan["restored_daemon"]["pid"], restored.pid)
        self.platform.daemon.side_effect = [DeploymentError("not candidate"), restored]
        self.platform.health.side_effect = None
        starts = self.platform.start.call_count
        result = recover(cli_link=self.cli, plugin_link=self.plugin,
                         state_dir=self.state, platform=self.platform)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.platform.start.call_count, starts)

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
            if path == self.plugin:
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
            self.scan_legado.return_value = []
            deployment.SETTLE_TIMEOUT = 10.0
            chamadas["n"], falhar_em["link"] = 0, 0

        # Três trocas de link agora: bloqueador, plugin e a liberação do CLI novo.
        with patch("castanha.deployment.replace_link", side_effect=talvez_falhar):
            for cenario in ("bloqueador", "stop", "settle", "plugin", "start", "health",
                            "liberação do CLI"):
                with self.subTest(cenario=cenario):
                    reiniciar()
                    falhar_em["link"] = {"bloqueador": 1, "plugin": 2,
                                         "liberação do CLI": 3}.get(cenario, 0)
                    if cenario == "stop":
                        self.platform.stop.side_effect = DeploymentError("stop falhou")
                    if cenario == "settle":
                        self.scan_legado.return_value = [999999]
                        deployment.SETTLE_TIMEOUT = 0.05
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
                    self.assertNotEqual(self.cli.resolve(), self.blocker(),
                                        f"entrypoint largado no bloqueador em {cenario}")

    def test_legacy_cli_deciding_before_popen_refuses_the_install(self):
        """A janela que era residual: legado já decidiu gravar e ainda não subiu ffmpeg.

        Ele é invisível como gravação (o ffmpeg não nasceu) e invisível no
        estado (o PID só é publicado depois), mas é processo desde o exec. O
        inventário o vê, espera, e recusa quando ele não some no prazo.
        """
        self.usar_varredura_real()
        self.settle_rapido(timeout=1.0, clean_scans=2, interval=0.05)
        self.platform.start.side_effect = [self.old_daemon]
        barreira = self.barreira()
        trocas = self.espiar_links()
        legado = self.spawn_cli_legado(barreira)
        self.esperar(barreira / "leu-ocioso")
        resultado = self.run_deploy()
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)
        self.assert_candidato_nunca_exposto(trocas)
        self.assertEqual(self.platform.start.call_args.args[0], self.old,
                         "O daemon anterior precisa voltar")
        # E a gravação que ele decidiu fazer nasce sobre a instalação ANTERIOR,
        # coerente com os dois links, que é o ponto de recusar em vez de trocar.
        (barreira / "seguir").write_text("1")
        self.esperar(barreira / "gravando")
        legado.wait(timeout=10)
        filho = int((barreira / "gravando").read_text())
        self.addCleanup(self.matar_pid, filho)
        self.assertIn(filho, capture_processes())
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_legacy_cli_that_gives_up_only_delays_the_install(self):
        """"Até estado estável" é esperar, não recusar de saída."""
        self.usar_varredura_real()
        self.settle_rapido(timeout=15.0, clean_scans=2, interval=0.05)
        self.platform.start.side_effect = [self.new_daemon]
        barreira = self.barreira()
        legado = self.spawn_cli_legado(barreira, "--desistir", "--atraso=0.6",
                                       entrypoint=self.old / "bin/castanha")
        self.esperar(barreira / "leu-ocioso")
        self.assertEqual(self.run_deploy()["status"], "ok")
        legado.wait(timeout=10)
        self.assertTrue((barreira / "desistiu").exists())
        self.assertFalse((barreira / "gravando").exists())
        self.assertEqual(self.cli.resolve(), self.new / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.new)

    def test_capture_born_during_the_inventory_refuses_before_any_swap(self):
        """Nascimento durante a transição: o ffmpeg aparece no meio do inventário."""
        self.usar_varredura_real()
        self.settle_rapido(timeout=15.0, clean_scans=3, interval=0.05)
        self.platform.start.side_effect = [self.old_daemon]
        barreira = self.barreira()
        trocas = self.espiar_links()
        legado = self.spawn_cli_legado(barreira, "--atraso=0.5")
        self.esperar(barreira / "leu-ocioso")
        resultado = self.run_deploy()
        legado.wait(timeout=10)
        self.addCleanup(self.matar_pid, int((barreira / "gravando").read_text()))
        self.assertEqual(resultado["status"], "rolled_back")
        self.assertTrue(resultado["capture_running"])
        self.assert_candidato_nunca_exposto(trocas)
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_blocker_takes_the_entrypoint_before_the_stop_and_refuses_to_record(self):
        """O bloqueador entra antes do stop, que leva até 15 s com o painel ativo."""
        ordem = []

        def no_stop(identidade):
            ordem.append(("stop", os.readlink(self.cli)))

        self.platform.stop.side_effect = no_stop
        self.platform.start.side_effect = [self.new_daemon]
        self.platform.health.side_effect = lambda root, ident: ordem.append(
            ("health", os.readlink(self.cli)))
        self.assertEqual(self.run_deploy()["status"], "ok")
        self.assertEqual([passo for passo, _ in ordem], ["stop", "health"])
        for passo, alvo in ordem:
            self.assertEqual(alvo, str(self.blocker()), f"CLI exposto em {passo}")
        self.assertEqual(self.cli.resolve(), self.new / "bin/castanha",
                         "O CLI novo só aparece no fim")
        # E o bloqueador é executável de verdade: recusa honesta, não gravação.
        saida = subprocess.run([str(self.blocker())], capture_output=True, text=True, timeout=10)
        self.assertEqual(saida.returncode, BLOCKER_EXIT)
        self.assertIn("Instalação em andamento", saida.stderr)

    def test_only_a_lock_aware_candidate_may_be_released(self):
        """Liberar CLI que ignora a trava reabriria a janela que isto fecha."""
        pacote = self.new / "castanha"
        pacote.mkdir(exist_ok=True)
        (pacote / "engine.py").write_text("def start_recording():\n    pass\n")
        with self.assertRaises(DeploymentError):
            assert_lock_aware(self.new)
        (pacote / "engine.py").write_text('trava = ".deployment.lock"\n')
        with self.assertRaises(DeploymentError):
            assert_lock_aware(self.new)
        (pacote / "engine.py").unlink()
        with self.assertRaises(DeploymentError):
            assert_lock_aware(self.new)

    def test_capture_holding_the_shared_lock_makes_the_install_refuse_honestly(self):
        """Remédio, ordem 1: captura com a trava, instalação recusa sem traceback."""
        self.usar_varredura_real()
        barreira = self.barreira()
        captura = self.spawn_cli_legado(barreira, "--lock")
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
        barreira = self.barreira()

        def tentar_gravar(identidade):
            self.spawn_cli_legado(barreira, "--lock",
                                  entrypoint=self.old / "bin/castanha").wait(timeout=10)

        self.platform.stop.side_effect = tentar_gravar
        self.platform.start.side_effect = [self.new_daemon]
        self.assertEqual(self.run_deploy()["status"], "ok")
        self.assertTrue((barreira / "recusado-pela-trava").exists(),
                        "A captura precisa esbarrar na trava do instalador")
        self.assertFalse((barreira / "gravando").exists())
        self.assertFalse((barreira / "leu-ocioso").exists())

    def test_proc_that_cannot_be_enumerated_never_reports_absence(self):
        """Erro de varredura é recusa, não "nenhuma captura"."""
        self.usar_varredura_real()
        with self.assertRaises(DeploymentError):
            capture_processes(proc=self.base / "proc-que-nao-existe")
        with self.assertRaises(DeploymentError):
            assert_quiescent(self.state, proc=self.base / "proc-que-nao-existe")
        falso = self.base / "proc-falso"
        # Cmdline ausente com entrada de PID presente NÃO prova morte.
        (falso / "5678").mkdir(parents=True)
        with self.assertRaises(DeploymentError):
            capture_processes(proc=falso)
        (falso / "5678").rmdir()
        self.assertEqual(capture_processes(proc=falso), [])
        # Erro que não é desaparecimento (EISDIR real, não simulado): fecha.
        (falso / "1234/cmdline").mkdir(parents=True)
        with self.assertRaises(DeploymentError):
            capture_processes(proc=falso)

    def test_permission_denied_on_a_live_pid_is_never_a_disappearance(self):
        """Root ignora modo de arquivo, então PermissionError entra injetado.

        A classificação é o que está sob teste: negar leitura de um PID que
        continua lá não prova que ele morreu, e tratar como morte devolveria
        "nenhuma captura" a partir de um erro.
        """
        self.usar_varredura_real()
        falso = self.base / "proc-negado"
        (falso / "4321").mkdir(parents=True)
        (falso / "4321/cmdline").write_bytes(b"ffmpeg\0")
        with patch.object(Path, "read_bytes", side_effect=PermissionError(13, "negado")):
            with self.assertRaises(DeploymentError):
                capture_processes(proc=falso)
            self.assertIsNone(capture_running(), "Relato honesto: não deu para olhar")

    def test_inventory_ignores_this_process_and_unrelated_ones(self):
        self.usar_varredura_real()
        legados = {str(self.cli), str(self.old / "bin/castanha")}
        self.assertEqual(legacy_processes(legados, [self.old]), [],
                         "Sem legado vivo o inventário precisa estar vazio")
        barreira = self.barreira()
        legado = self.spawn_cli_legado(barreira)
        self.esperar(barreira / "leu-ocioso")
        self.assertIn(legado.pid, legacy_processes(legados, [self.old]))
        self.assertNotIn(os.getpid(), legacy_processes(legados, [self.old]))
        self.assertEqual(legacy_processes(legados, [self.old], skip=[legado.pid]), [])
        settle(legados, [self.old], skip=[legado.pid], timeout=1.0, clean_scans=2, interval=0.0)
        legado.kill()
        legado.wait(timeout=5)

    def test_relative_legacy_cli_blocks_even_after_the_first_observation(self):
        self.usar_varredura_real()
        self.settle_rapido(timeout=0.2, clean_scans=2, interval=0.02)
        for entrypoint, cwd in (("bin/castanha", self.old),
                                ("./bin/castanha", self.old),
                                ("old/bin/castanha", self.base),
                                ("../cli", self.old)):
            with self.subTest(entrypoint=entrypoint):
                barrier = self.base / ("relative-" + str(len(list(self.base.iterdir()))))
                barrier.mkdir()
                child = self.spawn_cli_legado(barrier, entrypoint=entrypoint, cwd=cwd)
                self.esperar(barrier / "leu-ocioso")
                paths = {str(self.cli), str(self.old / "bin/castanha")}
                self.assertIn(child.pid, legacy_processes(paths, [self.old]))
                swaps = self.espiar_links()
                self.platform.start.side_effect = [self.old_daemon]
                result = self.run_deploy()
                self.assertEqual(result["status"], "rolled_back")
                self.assert_candidato_nunca_exposto(swaps)
                self.assertIsNone(child.poll(), "Instalador não pode matar legado")
                (barrier / "seguir").write_text("1")
                self.esperar(barrier / "gravando")
                child.wait(timeout=5)
                capture_pid = int((barrier / "gravando").read_text())
                self.assertIn(capture_pid, capture_processes())
                self.matar_pid(capture_pid)
                (self.state / "state.json").write_text('{"status":"idle","pid":null}')

    def test_live_module_with_deleted_cwd_is_not_a_dead_pid(self):
        self.usar_varredura_real()
        root = self.base / "deleted-root"
        (root / "castanha").mkdir(parents=True)
        (root / "castanha/__init__.py").write_text("")
        ready = self.base / "deleted-ready"
        script = root / "castanha/fixture.py"
        script.write_text("from pathlib import Path\nimport time\n"
                          f"Path({str(ready)!r}).touch()\ntime.sleep(60)\n")
        child = self.kill_later(subprocess.Popen(
            [sys.executable, "-B", "-m", "castanha.fixture"], cwd=root,
            env={"PATH": os.defpath}))
        self.esperar(ready)
        script.unlink()
        (root / "castanha/__init__.py").unlink()
        (root / "castanha").rmdir()
        root.rmdir()
        self.assertIsNone(child.poll())
        with self.assertRaises(FileNotFoundError):
            (Path("/proc") / str(child.pid) / "cwd").resolve(strict=True)
        with self.assertRaises(DeploymentError):
            legacy_processes({str(self.cli)}, [root])
        with self.assertRaises(DeploymentError):
            settle({str(self.cli)}, [root], timeout=0.1)
        self.assertIsNone(child.poll())

    def test_relative_legacy_spawns_capture_after_inventory_started(self):
        self.usar_varredura_real()
        self.settle_rapido(timeout=2, clean_scans=2, interval=0.02)
        barrier = self.barreira()
        child = self.spawn_cli_legado(barrier, entrypoint="bin/castanha", cwd=self.old)
        self.esperar(barrier / "leu-ocioso")
        scans = []

        def observe(*args, **kwargs):
            found = legacy_processes(*args, **kwargs)
            scans.append(found)
            if child.pid in found:
                (barrier / "seguir").write_text("1")
                self.esperar(barrier / "gravando")
            return found

        swaps = self.espiar_links()
        self.platform.start.side_effect = [self.old_daemon]
        with patch("castanha.deployment.legacy_processes", side_effect=observe):
            result = self.run_deploy()
        child.wait(timeout=5)
        capture_pid = int((barrier / "gravando").read_text())
        self.addCleanup(self.matar_pid, capture_pid)
        self.assertIn(child.pid, scans[0])
        self.assertEqual(result["status"], "rolled_back")
        self.assertTrue(result["capture_running"])
        self.assertIn(capture_pid, capture_processes())
        self.assert_candidato_nunca_exposto(swaps)

    def test_pid_is_ignored_only_after_its_directory_disappears(self):
        self.usar_varredura_real()
        proc = self.base / "vanishing-proc"
        entry = proc / "12345"
        entry.mkdir(parents=True)
        original_read = Path.read_bytes

        def disappear(path):
            if path == entry / "cmdline":
                entry.rmdir()
                raise FileNotFoundError("PID realmente desapareceu")
            return original_read(path)

        with patch.object(Path, "read_bytes", disappear):
            self.assertEqual(capture_processes(proc=proc), [])

    def test_legacy_inventory_permission_and_enumeration_errors_fail_closed(self):
        self.usar_varredura_real()
        with self.assertRaises(DeploymentError):
            legacy_processes({str(self.cli)}, [self.old], proc=self.base / "missing")
        proc = self.base / "unreadable-proc"
        entry = proc / "12345"
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"python\0-m\0castanha.daemon\0")
        resolve = Path.resolve

        def denied(path, *args, **kwargs):
            if path == entry / "cwd":
                raise PermissionError("cwd negado")
            return resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", denied):
            with self.assertRaises(DeploymentError):
                legacy_processes({str(self.cli)}, [self.old], proc=proc)
        # Nem ESRCH vindo de um filho de /proc prova que a entrada morreu.
        with patch.object(Path, "read_bytes", side_effect=ProcessLookupError("cmdline")):
            with self.assertRaises(DeploymentError):
                capture_processes(proc=proc)
        with patch.object(Path, "iterdir", side_effect=PermissionError("proc negado")):
            with self.assertRaises(DeploymentError):
                legacy_processes({str(self.cli)}, [self.old], proc=proc)

    def test_behavioral_lock_preflight_rejects_decoys_and_wrong_lock(self):
        package = self.new / "castanha"
        package.mkdir()
        (package / "__init__.py").write_text("")
        engine = package / "engine.py"
        source = """import fcntl, os
from pathlib import Path
class CastanhaEngine:
    def start_recording(self):
        path = Path(os.environ['XDG_STATE_HOME']) / 'castanha' / LOCK_FILE
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {'status': 'error'}
            self.state_mgr.read()
            child = self.recorder.start('unused')
            state = {'status': 'recording', 'pid': child.pid}
            self.state_mgr.write(state)
            return state
"""
        for lock in ('".deployment.lock"', '".other.lock"'):
            engine.write_text(source.replace("LOCK_FILE", lock))
            if lock == '".deployment.lock"':
                assert_lock_aware(self.new)
            else:
                with self.assertRaises(DeploymentError):
                    assert_lock_aware(self.new)
        for fake in ("# .deployment.lock\n", 'unused = ".deployment.lock"\n',
                     "class CastanhaEngine:\n    def start_recording(self):\n"
                     "        return {'status': 'error'} # .deployment.lock\n"):
            engine.write_text(fake)
            with self.assertRaises(DeploymentError):
                assert_lock_aware(self.new)
        # A prova livre detecta soltura antes do Popen ou antes da publicação.
        for stage in ("child = self.recorder.start", "self.state_mgr.write(state)"):
            broken = source.replace("LOCK_FILE", '".deployment.lock"')
            broken = broken.replace("            " + stage,
                                    "            fcntl.flock(lock, fcntl.LOCK_UN)\n            " + stage)
            engine.write_text(broken)
            with self.assertRaises(DeploymentError):
                assert_lock_aware(self.new)
        engine.write_text(source.replace("LOCK_FILE", '".deployment.lock"')
                          .replace("fcntl.LOCK_EX", "fcntl.LOCK_SH"))
        with self.assertRaises(DeploymentError):
            assert_lock_aware(self.new)

    def test_real_engine_obeys_the_installer_lock_contract(self):
        assert_lock_aware(Path(__file__).resolve().parents[1])
        LocalPlatform(self.state).validate_runtime(Path(__file__).resolve().parents[1])

    def test_uncooperative_initial_runtime_refuses_before_any_mutation(self):
        package = self.old / "castanha"
        package.mkdir()
        (package / "__init__.py").write_text("")
        # Mesma ordem de b97131e: leitura de estado antes de qualquer trava.
        (package / "engine.py").write_text(
            "class CastanhaEngine:\n"
            "    def start_recording(self):\n"
            "        state = self.state_mgr.read()\n"
            "        if state.get('status') in ['recording', 'paused']:\n"
            "            return {'status': 'error'}\n"
            "        self.recorder.start('unused')\n")
        self.platform.validate_runtime.side_effect = LocalPlatform(self.state).validate_runtime
        state_before = (self.state / "state.json").read_bytes()
        swaps = self.espiar_links()
        with self.assertRaisesRegex(LegacyTransitionBlocked, "Primeira instalação bloqueada"):
            self.run_deploy()
        self.assertEqual(swaps, [])
        self.platform.daemon.assert_not_called()
        self.platform.stop.assert_not_called()
        self.platform.start.assert_not_called()
        self.assertFalse((self.state / "deployment.json").exists())
        self.assertFalse(self.blocker().exists())
        self.assertEqual((self.state / "state.json").read_bytes(), state_before)
        self.assertEqual(self.cli.resolve(), self.old / "bin/castanha")
        self.assertEqual(self.plugin.resolve(), self.old)

    def test_real_start_releases_lock_after_an_audio_error(self):
        from castanha.deployment import exclusive
        from types import SimpleNamespace
        engine = CastanhaEngine.__new__(CastanhaEngine)
        engine.config = {}
        engine.state_mgr = SimpleNamespace(read=lambda: {"status": "idle"})
        engine.recorder = Mock()
        engine.recorder.start.side_effect = RuntimeError("áudio indisponível")
        with patch("castanha.capture_gate.get_state_dir", return_value=self.state), \
                patch("castanha.engine.is_default_source_muted", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "áudio indisponível"):
                engine.start_recording()
            with exclusive(self.state):
                result = engine.start_recording()
                self.assertEqual(result["status"], "error")
            self.assertEqual(engine.recorder.start.call_count, 1)

    def test_detector_stays_coupled_to_the_capture_output_name(self):
        self.assertIn(CAPTURE_OUTPUT_PREFIX, inspect.getsource(CastanhaEngine.start_recording),
                      "Mudou o nome do arquivo de captura: a varredura de /proc fica cega")


if __name__ == "__main__":
    unittest.main()
