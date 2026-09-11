#!/usr/bin/env python3
"""Atualiza o checkout único do systemd, com SHA exato e rollback de saúde.

Uso: python3 scripts/deploy-local.py CHECKOUT SHA_COMPLETO
Execute depois dos testes e review. A árvore instalada deve estar limpa.
Atualiza o lançador Castanha com backup; não altera gravações ou configuração de captura.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from castanha.capture_gate import open_lock
from castanha.config import get_state_dir
from castanha.deployment import assert_idle, process_identity


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=45).strip()


def health(root, verify_ui=False):
    run("systemctl", "--user", "is-active", "castanha.service")
    pid = int(run("systemctl", "--user", "show", "castanha.service", "-p", "MainPID", "--value"))
    assert process_identity(pid).cwd == str(root), "Daemon fora do checkout instalado"
    state = json.loads(run(sys.executable, str(root / "bin/castanha"), "status", "--json"))
    assert state.get("status") == "idle", "CLI sem estado ocioso válido"
    if verify_ui:
        spec = importlib.util.spec_from_file_location("castanha_ui_release", Path(__file__).with_name("deploy-ui.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.reload_panel(verify_new=verify_ui == "candidate", expected_token="castanha-gui-v5")
    else:
        run("omarchy-shell", "shell", "rescanPlugins")


def launcher_path():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications/castanha.desktop"


def install_launcher(root, journal_dir):
    source = root / "scripts/castanha.desktop"
    if not source.exists():
        return None
    target = launcher_path()
    if target.is_symlink():
        raise RuntimeError("Lançador é um link; preserve o destino antes de atualizar")
    previous = target.read_bytes() if target.exists() else None
    backup = journal_dir / "launcher-before-release.desktop"
    if previous is not None:
        backup.write_bytes(previous)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".desktop.castanha-new")
    try:
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return (target, previous)


def restore_launcher(snapshot):
    if snapshot is None:
        return
    target, previous = snapshot
    if previous is None:
        target.unlink(missing_ok=True)
    else:
        temporary = target.with_suffix(".desktop.castanha-rollback")
        temporary.write_bytes(previous)
        os.replace(temporary, target)


def deploy(root, sha):
    root = Path(root).resolve(strict=True)
    git = lambda *args: run("git", "-C", str(root), *args)
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), "SHA completo obrigatório"
    assert git("rev-parse", sha + "^{commit}") == sha
    assert not git("status", "--porcelain"), "Preserve alterações locais antes de instalar"
    previous = git("rev-parse", "HEAD")
    git("merge-base", "--is-ancestor", previous, sha)
    state_dir = get_state_dir()
    with open_lock(state_dir) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert_idle(state_dir)
        health(root)
        # Registrado ANTES de parar o daemon. Nenhum dado de reunião é revertido.
        journal = state_dir / "local-release.json"
        journal.write_text(json.dumps({"previous": previous, "candidate": sha, "checkout": str(root)}))
        launcher_snapshot = None
        try:
            run("systemctl", "--user", "stop", "castanha.service")
            git("merge", "--ff-only", sha)
            run("systemctl", "--user", "start", "castanha.service")
            time.sleep(2)
            launcher_snapshot = install_launcher(root, state_dir)
            health(root, verify_ui="candidate")
        except BaseException:
            launcher_error = None
            try:
                restore_launcher(launcher_snapshot)
            except BaseException as error:
                # Restaurar um lançador não pode impedir recuperar o serviço.
                launcher_error = error
            run("systemctl", "--user", "stop", "castanha.service")
            git("reset", "--keep", previous)
            run("systemctl", "--user", "start", "castanha.service")
            time.sleep(2)
            health(root, verify_ui="rollback")
            if launcher_error is not None:
                raise RuntimeError("Código e serviço restaurados; falha ao restaurar lançador. Backup em launcher-before-release.desktop") from launcher_error
            print("Falha na atualização; versão anterior restaurada.", file=sys.stderr)
            raise
    print(f"Castanha instalado e saudável: {sha}")


if __name__ == "__main__":
    deploy(*sys.argv[1:])
