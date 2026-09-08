#!/usr/bin/env python3
"""Atualiza o checkout único do systemd, com SHA exato e rollback de saúde.

Uso: python3 scripts/deploy-local.py CHECKOUT SHA_COMPLETO
Execute depois dos testes e review. A árvore instalada deve estar limpa.
Não altera links, configuração ou gravações.
"""
import fcntl
import json
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


def health(root):
    run("systemctl", "--user", "is-active", "castanha.service")
    pid = int(run("systemctl", "--user", "show", "castanha.service", "-p", "MainPID", "--value"))
    assert process_identity(pid).cwd == str(root), "Daemon fora do checkout instalado"
    state = json.loads(run(sys.executable, str(root / "bin/castanha"), "status", "--json"))
    assert state.get("status") == "idle", "CLI sem estado ocioso válido"
    run("omarchy-shell", "shell", "rescanPlugins")


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
        try:
            run("systemctl", "--user", "stop", "castanha.service")
            git("merge", "--ff-only", sha)
            run("systemctl", "--user", "start", "castanha.service")
            time.sleep(2)
            health(root)
        except BaseException:
            run("systemctl", "--user", "stop", "castanha.service")
            git("reset", "--keep", previous)
            run("systemctl", "--user", "start", "castanha.service")
            time.sleep(2)
            health(root)
            print("Falha na atualização; versão anterior restaurada.", file=sys.stderr)
            raise
    print(f"Castanha instalado e saudável: {sha}")


if __name__ == "__main__":
    deploy(*sys.argv[1:])
