"""Instalação reversível, sem apagar checkout, configuração ou gravações.

Só executa mediante --apply. Não substitui revisão cruzada ou QA de interface.
O journal é gravado antes de interromper o daemon e antes de trocar links.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from castanha.config import get_state_dir
from castanha.capture_gate import LOCK_NAME, open_lock
from castanha.transition_fence import TransitionFence
from castanha.durability import atomic_write, file_sha256, sync_directory, write_json

# O ffmpeg da captura recebe o arquivo de saída no argv antes de o estado
# declarar a gravação. É o primeiro sinal observável de uma captura nascendo.
CAPTURE_OUTPUT_GLOB = "castanha_rec_*.ogg"
CAPTURE_OUTPUT_PREFIX = "castanha_rec_"

# Inventário de legados: o CLI antigo em voo já é processo, mas ainda não é
# gravação nem estado. Esperar ele resolver é o que fecha a janela entre a
# decisão de gravar e o Popen do ffmpeg.
SETTLE_TIMEOUT = 20.0
SETTLE_INTERVAL = 0.2
SETTLE_CLEAN_SCANS = 5

# Bloqueador transitório do entrypoint do CLI. 75 é EX_TEMPFAIL: a chamada não
# aconteceu e vale tentar de novo em segundos.
BLOCKER_NAME = "deployment-blocker"
BLOCKER_EXIT = 75
BLOCKER_SCRIPT = f"""#!/bin/sh
echo "[Castanha] Instalação em andamento: o CLI está bloqueado por segundos." >&2
echo "[Castanha] Nenhuma gravação começa agora; tente de novo em instantes." >&2
exit {BLOCKER_EXIT}
"""


class DeploymentError(RuntimeError):
    pass


class LegacyTransitionBlocked(DeploymentError):
    """Runtime anterior não oferece uma cerca comprovável para novos starts."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str
    cwd: str
    exe: str
    uid: int


def process_identity(pid, *, proc=Path("/proc")):
    if type(pid) is not int or pid <= 0:
        raise DeploymentError("PID inválido")
    directory = proc / str(pid)
    try:
        command = (directory / "cmdline").read_bytes().split(b"\0")
        if not any(command[i:i + 2] == [b"-m", b"castanha.daemon"] for i in range(len(command))):
            raise DeploymentError("PID não pertence ao daemon Castanha")
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        identity = ProcessIdentity(pid, int(fields[19]),
                                   (proc / "sys/kernel/random/boot_id").read_text().strip(),
                                   str((directory / "cwd").resolve(strict=True)),
                                   str((directory / "exe").resolve(strict=True)),
                                   directory.stat().st_uid)
    except (OSError, ValueError, IndexError) as exc:
        raise DeploymentError("Identidade do daemon indisponível") from exc
    if identity.uid != os.getuid():
        raise DeploymentError("Daemon pertence a outro usuário")
    return identity


def assert_idle(state_dir):
    try:
        state = json.loads((state_dir / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise DeploymentError("Estado de captura indisponível, instalação recusada") from exc
    if (not isinstance(state, dict) or state.get("status") != "idle" or
            state.get("pid") is not None or state.get("processing_pid") is not None):
        raise DeploymentError("Captura ou finalização ativa, instalação recusada")


def scan_processes(proc, select):
    """Varredura de `/proc` que falha fechado: só desaparecimento é ignorado.

    A varredura é a prova de ausência do instalador. Sem conseguir enumerar
    `/proc`, ou sem conseguir ler um PID que continua lá, não existe prova
    nenhuma: devolver lista vazia seria afirmar "não há captura" a partir de um
    erro. ENOENT/ESRCH só provam desaparecimento se a entrada do PID também
    tiver sumido; um cwd apagado pode continuar pertencendo a processo vivo. Qualquer
    outro erro, PermissionError à frente, interrompe a instalação.
    """
    found = []
    uid = os.getuid()
    try:
        entries = list(Path(proc).iterdir())
    except OSError as exc:
        raise DeploymentError("/proc não pôde ser lido, instalação recusada") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = [arg.decode("utf-8", "replace")
                    for arg in entry.joinpath("cmdline").read_bytes().split(b"\0") if arg]
            interessa = select(entry, argv)
        except (FileNotFoundError, ProcessLookupError) as exc:
            # ENOENT de cwd também acontece com processo VIVO em diretório
            # apagado. Só a ausência da entrada do PID prova desaparecimento.
            try:
                entry.stat()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as probe_error:
                raise DeploymentError(f"Não foi possível verificar PID {entry.name}") from probe_error
            raise DeploymentError(f"PID {entry.name} continua presente mas ilegível") from exc
        except OSError as exc:
            raise DeploymentError(
                f"PID {entry.name} ilegível ({type(exc).__name__}), varredura sem valor") from exc
        if interessa:
            found.append(int(entry.name))
    return sorted(found)


def capture_processes(*, proc=Path("/proc")):
    """PIDs de captura vivos deste usuário, mesmo antes de o estado declarar.

    `start_recording` faz ler estado, subir o ffmpeg e só então publicar o PID
    em state.json. Entre o Popen e a publicação o processo é a única prova de
    que existe gravação, e é justamente essa janela que derruba `assert_idle`.
    """
    return scan_processes(proc, lambda entry, argv: any(
        fnmatch.fnmatch(os.path.basename(arg), CAPTURE_OUTPUT_GLOB) for arg in argv))


def legacy_processes(paths, roots, *, proc=Path("/proc"), skip=()):
    """Castanha antigo ainda vivo: o CLI legado em voo, e daemon não identificado.

    O argv pode citar o link instalado, o script absoluto ou um caminho
    relativo ao cwd do processo, inclusive `python bin/castanha`. A comparação
    lexical preserva a assinatura de quem entrou antes da troca do link para o
    bloqueador. Módulos `-m castanha.*` são reconhecidos pelo cwd do checkout.
    """
    caminhos = {str(p) for p in paths}
    raizes = [str(r) for r in roots]
    ignorar = {os.getpid(), *skip}

    def legado(entry, argv):
        if int(entry.name) in ignorar:
            return False
        if any(arg in caminhos for arg in argv):
            return True
        modulo = any(argv[i] == "-m" and argv[i + 1].startswith("castanha.")
                     for i in range(len(argv) - 1))
        nomes = {os.path.basename(path) for path in caminhos}
        relativos = [arg for arg in argv if not os.path.isabs(arg) and
                     os.path.basename(arg) in nomes]
        if not modulo and not relativos:
            return False
        cwd = str((entry / "cwd").resolve(strict=True))
        # Normalizar lexicalmente preserva o argv anterior à troca de links.
        # Resolver também cobre um cwd físico sob um alias do plugin.
        for arg in relativos:
            absoluto = os.path.normpath(os.path.join(cwd, arg))
            if absoluto in caminhos:
                return True
            if Path(absoluto).resolve() in {Path(r) / "bin/castanha" for r in raizes}:
                return True
        return modulo and any(cwd == raiz or cwd.startswith(raiz + os.sep) for raiz in raizes)

    return scan_processes(proc, legado)


def settle(paths, roots, *, proc=Path("/proc"), timeout=None, interval=None,
           clean_scans=None, skip=()):
    """Inventário de legados até estado estável, ou recusa nomeando quem sobrou.

    O CLI legado que já leu "ocioso" e ainda não chamou o Popen é invisível como
    gravação e invisível no estado, mas é um processo desde o exec. Ele termina
    de um jeito ou de outro: sai sem gravar, e o inventário esvazia, ou sobe o
    ffmpeg, e a varredura de captura enxerga. Esperar é o que fecha a janela.

    Captura viva não espera: o ffmpeg dura a reunião inteira, então insistir só
    adiaria a mesma recusa. Exige leituras limpas seguidas porque uma chamada
    legada de milissegundos (o painel lendo `notes`) some entre duas varreduras.
    """
    timeout = SETTLE_TIMEOUT if timeout is None else timeout
    interval = SETTLE_INTERVAL if interval is None else interval
    clean_scans = SETTLE_CLEAN_SCANS if clean_scans is None else clean_scans
    limite = time.monotonic() + timeout
    limpas, vivos = 0, []
    while True:
        capturas = capture_processes(proc=proc)
        if capturas:
            raise DeploymentError(
                f"Captura viva (PID {capturas[0]}) ainda não declarada, instalação recusada")
        vivos = legacy_processes(paths, roots, proc=proc, skip=skip)
        limpas = 0 if vivos else limpas + 1
        if limpas >= clean_scans:
            return
        if time.monotonic() >= limite:
            raise DeploymentError(
                f"Castanha antigo ainda vivo (PID {vivos[0]}), instalação recusada" if vivos
                else "Inventário de processos não estabilizou, instalação recusada")
        time.sleep(interval)


def capture_running():
    """Relato honesto, nunca fonte de exceção: None quando não deu para olhar."""
    try:
        return bool(capture_processes())
    except Exception:
        return None


def state_fingerprint(state_dir):
    """Identidade do state.json. Cada write publica um inode novo por rename."""
    path = Path(state_dir) / "state.json"
    try:
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, file_sha256(path))
    except OSError as exc:
        raise DeploymentError("Estado de captura indisponível, instalação recusada") from exc


def assert_quiescent(state_dir, fingerprint=None, *, proc=Path("/proc")):
    """As três provas repetidas imediatamente antes de cada mutação.

    Estado ocioso declarado, nenhum processo de captura vivo e, na janela em
    que o daemon já parou, nenhum write em state.json por terceiro.
    """
    assert_idle(state_dir)
    vivos = capture_processes(proc=proc)
    if vivos:
        raise DeploymentError(f"Captura viva (PID {vivos[0]}) ainda não declarada, instalação recusada")
    atual = state_fingerprint(state_dir)
    if fingerprint is not None and atual != fingerprint:
        raise DeploymentError("Estado de captura mudou durante a instalação, instalação recusada")
    return atual


@contextmanager
def exclusive(state_dir):
    """A mesma trava do start da captura; contenção vira recusa honesta."""
    with open_lock(state_dir) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DeploymentError("Outra instalação ou captura em andamento") from exc
        yield lock


def replace_link(path, target):
    """Um rename atômico por link; o par é compensado pelo journal/rollback."""
    temporary = path.with_name(path.name + ".deploy-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def install_blocker(state_dir, cli_link):
    """Bloqueador transitório no entrypoint do CLI, por rename atômico.

    A primeira instalação sai de um runtime que não conhece a trava: o botão do
    painel, o atalho de teclado e o `castanha start` do terminal chamam o link
    do CLI e gravam sem pedir licença. Enquanto a troca acontece, o entrypoint
    responde recusa honesta em vez de abrir gravação. Entra antes do stop de
    propósito: o stop tem prazo de 15 s e o painel chama o CLI o tempo todo.
    """
    blocker = Path(state_dir) / BLOCKER_NAME
    atomic_write(blocker, BLOCKER_SCRIPT)
    os.chmod(blocker, 0o700)  # atomic_write publica 0o600, que barra por engano
    replace_link(Path(cli_link), blocker)
    return blocker


def assert_lock_aware(candidate):
    """Executa o start real, com áudio/estado substituídos, em HOME temporário.

    Prova as duas ordens do mesmo flock e sua duração até publicar o PID.
    Comentário, constante solta ou trava de outro arquivo não são evidência.
    Nenhum construtor do engine é chamado; nenhum áudio/processo é iniciado.
    """
    probe = Path(__file__).with_name("deployment_probe.py")
    with tempfile.TemporaryDirectory(prefix="castanha-lock-probe-") as directory:
        root = Path(directory)
        state = root / "state/castanha"
        state.mkdir(parents=True)
        env = {"HOME": directory, "XDG_STATE_HOME": str(root / "state"),
               "XDG_CONFIG_HOME": str(root / "config"), "PATH": os.defpath}

        def run(mode):
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(probe), str(Path(candidate).resolve()),
                     str(state / LOCK_NAME), mode], cwd=root, env=env,
                    capture_output=True, text=True, check=True, timeout=10)
                if json.loads(result.stdout) != {"lock_contract": mode}:
                    raise ValueError("Contrato sem prova")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise DeploymentError("Candidato não comprovou a trava de instalação ao gravar") from exc

        with exclusive(state):
            run("held")
        run("free")


class LocalPlatform:
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def revision(self, candidate):
        def git(*args):
            return subprocess.run(["git", "-C", str(candidate), *args], check=True,
                                  capture_output=True, text=True, timeout=15).stdout.strip()
        revision = git("rev-parse", "HEAD")
        if git("status", "--porcelain"):
            raise DeploymentError("Checkout possui alterações não preservadas por uma revisão")
        return revision

    def validate(self, candidate, sha):
        if self.revision(candidate) != sha:
            raise DeploymentError("Candidato mudou ou árvore não está limpa")
        for required in ("bin/castanha", "castanha/daemon.py", "manifest.json"):
            if not (candidate / required).is_file():
                raise DeploymentError("Candidato incompleto")
        assert_lock_aware(candidate)
        subprocess.run(["omarchy", "plugin", "validate", str(candidate)], check=True,
                       capture_output=True, timeout=20)

    def validate_runtime(self, root):
        # Trocar links não cerca `python bin/castanha` dentro do checkout
        # anterior. Sem cooperação do runtime, nenhuma quantidade de scans
        # impede um novo start depois da última observação. Não iniciar a
        # transação nesse caso: a primeira instalação exige uma cerca própria.
        try:
            assert_lock_aware(root)
        except DeploymentError as exc:
            raise LegacyTransitionBlocked(
                "Primeira instalação bloqueada: runtime anterior não comprovou a trava; "
                "é necessária uma transição separada") from exc

    def daemon(self, root, *, allow_dead=False):
        pidfile = self.state_dir / "daemon.pid"
        try:
            pid = int(pidfile.read_text().strip())
        except FileNotFoundError as exc:
            try:
                pidfile.lstat()
            except FileNotFoundError:
                return None
            except OSError as probe_error:
                raise DeploymentError("Pidfile não pôde ser verificado") from probe_error
            raise DeploymentError("Pidfile existe mas seu alvo está indisponível") from exc
        except (OSError, ValueError) as exc:
            raise DeploymentError("Pidfile inválido") from exc
        if allow_dead:
            if pid <= 0:
                raise DeploymentError("PID inválido")
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                return None  # ESRCH do kernel, nunca ENOENT de um filho de /proc.
            except (OSError, AttributeError) as exc:
                raise DeploymentError("Não foi possível comprovar vida/morte do daemon") from exc
            try:
                if select.select([descriptor], [], [], 0)[0]:
                    return None  # Inclui zombie: o processo inteiro já encerrou.
                try:
                    identity = process_identity(pid)
                except DeploymentError:
                    if select.select([descriptor], [], [], 0)[0]:
                        return None  # Morreu durante a leitura; pidfd prende qual processo.
                    raise
                if select.select([descriptor], [], [], 0)[0]:
                    return None
            finally:
                os.close(descriptor)
        else:
            identity = process_identity(pid)
        if identity.cwd != str(root) or identity.exe != str(Path(sys.executable).resolve()):
            raise DeploymentError("Daemon não corresponde ao checkout/executável esperado")
        return identity

    def stop(self, identity):
        # pidfd prende a identidade no kernel; PID reutilizado nunca recebe sinal.
        descriptor = os.pidfd_open(identity.pid)
        try:
            if process_identity(identity.pid) != identity:
                raise DeploymentError("Identidade do daemon mudou antes do stop")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], 15)[0]:
                raise DeploymentError("Daemon não encerrou, sem SIGKILL automático")
        finally:
            os.close(descriptor)

    def start(self, root):
        child = subprocess.Popen([sys.executable, "-B", "-m", "castanha.daemon"], cwd=root,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        descriptor = os.pidfd_open(child.pid)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise DeploymentError("Novo daemon encerrou durante inicialização")
                try:
                    identity = self.daemon(root)
                    if identity and identity.pid == child.pid:
                        return identity
                except DeploymentError:
                    pass  # Pidfile antigo durante a inicialização, prazo limitado.
                time.sleep(0.1)
            raise DeploymentError("Novo daemon não publicou identidade pronta")
        except Exception:
            if child.poll() is None:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                child.wait(timeout=15)
            raise
        finally:
            os.close(descriptor)

    def health(self, root, identity):
        time.sleep(1)
        if identity is not None and self.daemon(root) != identity:
            raise DeploymentError("Daemon perdeu prontidão")
        result = subprocess.run([sys.executable, str(root / "bin/castanha"), "status", "--json"],
                                capture_output=True, text=True, check=True, timeout=10)
        state = json.loads(result.stdout)
        if not isinstance(state, dict) or "status" not in state:
            raise DeploymentError("CLI sem resposta de saúde válida")
        subprocess.run(["omarchy-shell", "shell", "rescanPlugins"], check=True,
                       capture_output=True, timeout=15)


def deploy(candidate, sha, *, cli_link, plugin_link, state_dir, platform=None, apply=False,
           initial_transition=False):
    """Não toca gravações/config. Recusa journal incompleto de execução anterior."""
    candidate = Path(candidate).resolve(strict=True)
    cli_link, plugin_link, state_dir = Path(cli_link), Path(plugin_link), Path(state_dir)
    platform = platform or LocalPlatform(state_dir)
    journal = state_dir / "deployment.json"
    # Não seguir um journal arbitrário de volta a caminhos externos. Recuperação
    # de interrupção abrupta é diagnóstico explícito, nunca instalação por cima.
    if journal.exists():
        previous = json.loads(journal.read_text())
        if previous.get("phase") not in ("completed", "rolled_back"):
            raise DeploymentError("Instalação anterior interrompida; recuperar journal antes de continuar")
    if not cli_link.is_symlink() or not plugin_link.is_symlink():
        raise DeploymentError("CLI e plugin precisam ser links conhecidos, nada será removido")
    old_root = plugin_link.resolve(strict=True)
    if cli_link.resolve(strict=True) != old_root / "bin/castanha":
        raise DeploymentError("CLI e plugin apontam para versões diferentes")
    platform.validate(candidate, sha)
    fence = TransitionFence(old_root, state_dir, cli_link, plugin_link) if initial_transition else None
    if fence:
        fence.inspect()  # somente leitura; a cerca entra apenas dentro da transação
    else:
        platform.validate_runtime(old_root)
    assert_quiescent(state_dir)
    previous_daemon = platform.daemon(old_root)
    plan = {"candidate": str(candidate), "sha": sha,
            "previous_root": str(old_root), "previous_sha": platform.revision(old_root), "phase": "prepared",
            "cli_link": str(cli_link), "plugin_link": str(plugin_link),
            "previous_cli_target": os.readlink(cli_link),
            "previous_plugin_target": os.readlink(plugin_link),
            "blocker": str(state_dir / BLOCKER_NAME),
            "previous_daemon": asdict(previous_daemon) if previous_daemon else None,
            "initial_transition": initial_transition}
    if not apply:
        return {"status": "preflight_ok", "plan": plan}
    with exclusive(state_dir):
        # Revalidar após adquirir lock: preparação pode ter sido demorada.
        assert_quiescent(state_dir)
        if (os.readlink(cli_link) != plan["previous_cli_target"] or
                os.readlink(plugin_link) != plan["previous_plugin_target"] or
                platform.daemon(old_root) != previous_daemon):
            raise DeploymentError("Instalação mudou após preflight")
        write_json(journal, plan)
        # Nunca parada, parada incerta e parada confirmada são três coisas: só a
        # do meio proíbe subir outro daemon na reversão.
        parada = "confirmada" if previous_daemon is None else "nao_tentada"
        new_daemon = None
        # As três formas de chamar o CLI antigo: pelo link (painel, atalho de
        # teclado, PATH), pelo script do checkout e por dentro do link do plugin.
        legados = {str(cli_link), str(old_root / "bin/castanha"),
                   str(plugin_link / "bin/castanha"), plan["previous_cli_target"]}
        try:
            plan["phase"] = "blocking"
            write_json(journal, plan)
            if fence:
                fence_record = fence.prepare()
                fence.activate(fence_record)
            else:
                install_blocker(state_dir, cli_link)
            if previous_daemon:
                plan["phase"] = "stopping"
                write_json(journal, plan)
                parada = "incerta"
                platform.stop(previous_daemon)
                parada = "confirmada"
            # Bloqueador dentro e daemon identificado parado: os dois caminhos
            # que nascem gravação estão fechados para quem ainda vai começar.
            # Falta drenar quem já estava em voo antes dos dois.
            plan["phase"] = "settling"
            write_json(journal, plan)
            if fence:
                fence.assert_no_readers()
            else:
                settle(legados, [old_root])
            # Sem daemon e sem legado vivo não sobra escritor legítimo de
            # state.json, então qualquer write é captura nascendo e a
            # impressão digital vale como prova até o daemon novo subir.
            quieto = assert_quiescent(state_dir)
            plan["phase"] = "swapping"
            write_json(journal, plan)
            if fence:
                fence.assert_active()
            replace_link(plugin_link, candidate)
            assert_quiescent(state_dir, quieto)
            new_daemon = platform.start(candidate)
            plan.update(phase="checking", new_daemon=asdict(new_daemon))
            write_json(journal, plan)
            platform.health(candidate, new_daemon)
            # O daemon novo já publica agenda: aqui vale ocioso e processo,
            # não a impressão digital, que mudaria por escrita legítima.
            assert_quiescent(state_dir)
            # Último passo: o entrypoint só volta a existir apontando para o CLI
            # novo, que toma a trava (`assert_lock_aware`) e que este bloco
            # ainda segura. Antes disso nada expõe um CLI que grava sem licença.
            plan["phase"] = "releasing"
            write_json(journal, plan)
            replace_link(cli_link, candidate / "bin/castanha")
            assert_quiescent(state_dir)
            if fence:
                fence.assert_active()  # o checkout antigo permanece cercado após sucesso
            plan["phase"] = "completed"
            write_json(journal, plan)
            return {"status": "ok", "sha": sha}
        except Exception as exc:
            # Conservar a fase e a razão controlada antes que a reversão as
            # substitua. Não registrar saída de subprocesso ou configuração.
            plan.update(failed_phase=plan["phase"], phase="rollback", error_type=type(exc).__name__,
                        error_reason=str(exc) if type(exc).__name__ in {"DeploymentError", "FenceError"} else None)
            write_json(journal, plan)
            try:
                # Sem exigir ocioso: com captura nascida, deixar os dois links
                # trocados é pior do que devolvê-los ao checkout anterior.
                if new_daemon:
                    platform.stop(new_daemon)
                if not fence and platform.revision(old_root) != plan["previous_sha"]:
                    raise DeploymentError("Checkout anterior mudou, reversão recusada")
                replace_link(cli_link, plan["previous_cli_target"])
                replace_link(plugin_link, plan["previous_plugin_target"])
                if fence:
                    if fence.receipt.exists():
                        fence.restore()
                    if platform.revision(old_root) != plan["previous_sha"]:
                        raise DeploymentError("Checkout anterior mudou, reversão recusada")
                if parada == "incerta":
                    raise DeploymentError("Stop anterior não confirmado; não iniciar outro daemon")
                # Falha antes do stop devolve o entrypoint e não encosta no
                # daemon anterior, que nunca deixou de rodar.
                restored = (previous_daemon if parada == "nao_tentada"
                            else platform.start(old_root) if previous_daemon else None)
                plan.update(phase="rollback_checking",
                            restored_daemon=asdict(restored) if restored else None)
                write_json(journal, plan)
                platform.health(old_root, restored)
                plan["phase"] = "rolled_back"
                write_json(journal, plan)
                return {"status": "rolled_back", "error_type": type(exc).__name__,
                        "capture_running": capture_running()}
            except Exception as rollback_error:
                plan.update(phase="rollback_failed", rollback_error_type=type(rollback_error).__name__)
                write_json(journal, plan)
                raise DeploymentError("Reversão incompleta; journal preservado, sem ocultar falha") from rollback_error


def recover(*, cli_link, plugin_link, state_dir, platform=None):
    """Recupera um journal interrompido sem iniciar outro daemon por PID incerto."""
    cli_link, plugin_link, state_dir = Path(cli_link), Path(plugin_link), Path(state_dir)
    platform = platform or LocalPlatform(state_dir)
    journal = state_dir / "deployment.json"
    with exclusive(state_dir):
        plan = json.loads(journal.read_text())
        if plan.get("phase") == "rolled_back":
            return {"status": "already_rolled_back"}
        if plan.get("cli_link") != str(cli_link) or plan.get("plugin_link") != str(plugin_link):
            raise DeploymentError("Journal pertence a outros alvos")
        old_root, candidate = Path(plan["previous_root"]), Path(plan["candidate"])
        fence = (TransitionFence(old_root, state_dir, cli_link, plugin_link)
                 if plan.get("initial_transition") else None)
        if not fence and platform.revision(old_root) != plan["previous_sha"]:
            raise DeploymentError("Checkout anterior mudou, recuperação recusada")
        # O bloqueador é um alvo legítimo do CLI durante a transição: interrupção
        # no meio dela deixa o entrypoint nele, e isso não é terceiro mexendo.
        cli_esperado = {str(candidate / "bin/castanha"), plan["previous_cli_target"]}
        if plan.get("blocker") and not fence:
            cli_esperado.add(plan["blocker"])
        for link, allowed in ((cli_link, cli_esperado),
                              (plugin_link, {str(candidate), plan["previous_plugin_target"]})):
            if not link.is_symlink() or os.readlink(link) not in allowed:
                raise DeploymentError("Link alterado por terceiro, recuperação recusada")
        # Recuperação não exige ocioso: ela só devolve os links ao alvo
        # anterior, e o par meio trocado é o pior estado possível para quem
        # gravou no meio da interrupção.
        current = None
        old_running = False
        try:
            current = platform.daemon(candidate, allow_dead=True)
        except DeploymentError:
            current = platform.daemon(old_root, allow_dead=True)
            old_running = current is not None
        if current:
            expected = (plan.get("restored_daemon", plan.get("previous_daemon"))
                        if old_running else plan.get("new_daemon"))
            if asdict(current) != expected:
                raise DeploymentError("Identidade viva diverge do journal; recuperação recusada")
        plan["phase"] = "rollback"
        write_json(journal, plan)
        if current and not old_running:
            platform.stop(current)
        replace_link(cli_link, plan["previous_cli_target"])
        replace_link(plugin_link, plan["previous_plugin_target"])
        if fence:
            if fence.receipt.exists():
                fence.restore()
            if platform.revision(old_root) != plan["previous_sha"]:
                raise DeploymentError("Checkout anterior mudou, recuperação recusada")
        restored = current if old_running else platform.start(old_root) if plan["previous_daemon"] else None
        plan.update(phase="rollback_checking",
                    restored_daemon=asdict(restored) if restored else None)
        write_json(journal, plan)
        platform.health(old_root, restored)
        plan["phase"] = "rolled_back"
        write_json(journal, plan)
        return {"status": "rolled_back", "capture_running": capture_running()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate")
    parser.add_argument("--sha")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--initial-transition", action="store_true",
                        help="Cerca inicial reversível, exclusivamente desde b97131e")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    paths = {"cli_link": Path.home() / ".local/bin/castanha",
             "plugin_link": Path.home() / ".config/omarchy/plugins/io.github.brunoomoniz.castanha",
             "state_dir": get_state_dir()}
    try:
        if args.rollback:
            result = recover(**paths)
        else:
            if not args.candidate or not args.sha:
                parser.error("--candidate e --sha são obrigatórios fora de --rollback")
            result = deploy(args.candidate, args.sha, **paths, apply=args.apply,
                            initial_transition=args.initial_transition)
        print(json.dumps(result, ensure_ascii=False))
        success = result["status"] in ("ok", "preflight_ok", "already_rolled_back")
        return 0 if success or (args.rollback and result["status"] == "rolled_back") else 1
    except Exception as exc:
        # O stdout precisa contar a mesma história do disco: quem lê o código
        # de saída tem de saber se a reversão ficou pela metade.
        relato = {"status": "error", "error_type": type(exc).__name__,
                  "capture_running": capture_running()}
        if isinstance(exc, LegacyTransitionBlocked):
            relato["reason"] = str(exc)
        try:
            relato["phase"] = json.loads((paths["state_dir"] / "deployment.json").read_text())["phase"]
        except Exception:
            pass
        print(json.dumps(relato, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
