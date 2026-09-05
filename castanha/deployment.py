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
import time
import uuid

from castanha.config import get_state_dir
from castanha.durability import file_sha256, sync_directory, write_json

# O ffmpeg da captura recebe o arquivo de saída no argv antes de o estado
# declarar a gravação. É o primeiro sinal observável de uma captura nascendo.
CAPTURE_OUTPUT_GLOB = "castanha_rec_*.ogg"
CAPTURE_OUTPUT_PREFIX = "castanha_rec_"


class DeploymentError(RuntimeError):
    pass


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


def capture_processes(*, proc=Path("/proc")):
    """PIDs de captura vivos deste usuário, mesmo antes de o estado declarar.

    `start_recording` faz ler estado, subir o ffmpeg e só então publicar o PID
    em state.json. Entre o Popen e a publicação o processo é a única prova de
    que existe gravação, e é justamente essa janela que derruba `assert_idle`.
    """
    found = []
    uid = os.getuid()
    try:
        entries = list(Path(proc).iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = entry.joinpath("cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # O processo morreu no meio da varredura.
        for arg in argv:
            if arg and fnmatch.fnmatch(os.path.basename(arg.decode("utf-8", "replace")),
                                       CAPTURE_OUTPUT_GLOB):
                found.append(int(entry.name))
                break
    return sorted(found)


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
    """A trava que o start da captura precisa passar a tomar (ver docs).

    Só o instalador a toma hoje; contenção vira recusa honesta, nunca traceback.
    """
    with (Path(state_dir) / ".deployment.lock").open("a") as lock:
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
        subprocess.run(["omarchy", "plugin", "validate", str(candidate)], check=True,
                       capture_output=True, timeout=20)

    def daemon(self, root):
        pidfile = self.state_dir / "daemon.pid"
        if not pidfile.exists():
            return None
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError) as exc:
            raise DeploymentError("Pidfile inválido") from exc
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


def deploy(candidate, sha, *, cli_link, plugin_link, state_dir, platform=None, apply=False):
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
    assert_quiescent(state_dir)
    previous_daemon = platform.daemon(old_root)
    plan = {"candidate": str(candidate), "sha": sha,
            "previous_root": str(old_root), "previous_sha": platform.revision(old_root), "phase": "prepared",
            "cli_link": str(cli_link), "plugin_link": str(plugin_link),
            "previous_cli_target": os.readlink(cli_link),
            "previous_plugin_target": os.readlink(plugin_link),
            "previous_daemon": asdict(previous_daemon) if previous_daemon else None}
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
        stopped = previous_daemon is None
        new_daemon = None
        try:
            if previous_daemon:
                platform.stop(previous_daemon)
                stopped = True
            # Daemon parado: nesta janela não sobra escritor legítimo de
            # state.json, então qualquer write é captura nascendo e a
            # impressão digital vale como prova até o daemon novo subir.
            quieto = assert_quiescent(state_dir)
            plan["phase"] = "swapping"
            write_json(journal, plan)
            replace_link(cli_link, candidate / "bin/castanha")
            assert_quiescent(state_dir, quieto)
            replace_link(plugin_link, candidate)
            assert_quiescent(state_dir, quieto)
            new_daemon = platform.start(candidate)
            plan.update(phase="checking", new_daemon=asdict(new_daemon))
            write_json(journal, plan)
            platform.health(candidate, new_daemon)
            # O daemon novo já publica agenda: aqui vale ocioso e processo,
            # não a impressão digital, que mudaria por escrita legítima.
            assert_quiescent(state_dir)
            plan["phase"] = "completed"
            write_json(journal, plan)
            return {"status": "ok", "sha": sha}
        except Exception as exc:
            plan.update(phase="rollback", error_type=type(exc).__name__)
            write_json(journal, plan)
            try:
                # Sem exigir ocioso: com captura nascida, deixar os dois links
                # trocados é pior do que devolvê-los ao checkout anterior.
                if new_daemon:
                    platform.stop(new_daemon)
                if platform.revision(old_root) != plan["previous_sha"]:
                    raise DeploymentError("Checkout anterior mudou, reversão recusada")
                replace_link(cli_link, plan["previous_cli_target"])
                replace_link(plugin_link, plan["previous_plugin_target"])
                if not stopped:
                    raise DeploymentError("Stop anterior não confirmado; não iniciar outro daemon")
                restored = platform.start(old_root) if previous_daemon else None
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
        if platform.revision(old_root) != plan["previous_sha"]:
            raise DeploymentError("Checkout anterior mudou, recuperação recusada")
        for link, allowed in ((cli_link, {str(candidate / "bin/castanha"), plan["previous_cli_target"]}),
                              (plugin_link, {str(candidate), plan["previous_plugin_target"]})):
            if not link.is_symlink() or os.readlink(link) not in allowed:
                raise DeploymentError("Link alterado por terceiro, recuperação recusada")
        # Recuperação não exige ocioso: ela só devolve os links ao alvo
        # anterior, e o par meio trocado é o pior estado possível para quem
        # gravou no meio da interrupção.
        current = None
        old_running = False
        try:
            current = platform.daemon(candidate)
        except DeploymentError:
            current = platform.daemon(old_root)
            old_running = current is not None
        plan["phase"] = "rollback"
        write_json(journal, plan)
        if current and not old_running:
            platform.stop(current)
        replace_link(cli_link, plan["previous_cli_target"])
        replace_link(plugin_link, plan["previous_plugin_target"])
        restored = current if old_running else platform.start(old_root) if plan["previous_daemon"] else None
        platform.health(old_root, restored)
        plan["phase"] = "rolled_back"
        write_json(journal, plan)
        return {"status": "rolled_back", "capture_running": capture_running()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate")
    parser.add_argument("--sha")
    parser.add_argument("--apply", action="store_true")
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
            result = deploy(args.candidate, args.sha, **paths, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
        success = result["status"] in ("ok", "preflight_ok", "already_rolled_back")
        return 0 if success or (args.rollback and result["status"] == "rolled_back") else 1
    except Exception as exc:
        # O stdout precisa contar a mesma história do disco: quem lê o código
        # de saída tem de saber se a reversão ficou pela metade.
        relato = {"status": "error", "error_type": type(exc).__name__,
                  "capture_running": capture_running()}
        try:
            relato["phase"] = json.loads((paths["state_dir"] / "deployment.json").read_text())["phase"]
        except Exception:
            pass
        print(json.dumps(relato, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
