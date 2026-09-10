#!/usr/bin/env python3
"""Deploy restrito ao painel, com rollback e sem reiniciar daemon ou captura."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from castanha.capture_gate import open_lock
from castanha.config import get_state_dir
from castanha.durability import write_json


def run(*args, timeout=5):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout).strip()


def validate_paths(paths):
    allowed = {"Panel.qml", "DeliveryStatus.js", "i18n.js", "scripts/deploy-ui.py"}
    if not paths or not all(p in allowed or p.startswith(("tests/", "docs/")) for p in paths):
        raise ValueError("Deploy de painel recusa mudanças no backend, CLI, configuração ou manifesto")


def snapshot(root):
    run("systemctl", "--user", "is-active", "castanha.service")
    daemon = run("systemctl", "--user", "show", "castanha.service", "-p", "MainPID", "--value")
    state = json.loads(run(sys.executable, str(root / "bin/castanha"), "status", "--json"))
    identities = {}
    for pid in [int(daemon), state.get("pid"), state.get("processing_pid")]:
        if isinstance(pid, int) and pid > 0:
            # PID e início do processo: não confundir reutilização de PID.
            stat = (Path("/proc") / str(pid) / "stat").read_text()
            identities[pid] = stat.rsplit(")", 1)[1].split()[19]
    audio = state.get("audio_path") if state.get("status") == "recording" else None
    return {"pids": identities, "audio_path": audio,
            "audio_bytes": Path(audio).stat().st_size if audio else None}


def guard_shell_jobs(proc_root=Path("/proc")):
    """Não destruir um Process QML que ainda executa comando do Castanha."""
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(a.rsplit(b"/", 1)[-1] == b"castanha" or a.startswith(b"castanha.") for a in args):
                continue
            current = entry
            seen = set()
            while current.name not in seen:
                seen.add(current.name)
                stat = (current / "stat").read_text()
                name = stat[stat.index("(") + 1:stat.rindex(")")]
                if name in ("quickshell", "qs"):
                    raise RuntimeError("Comando do Castanha ativo no painel; aguarde sua conclusão")
                parent = int(stat.rsplit(")", 1)[1].split()[1])
                if parent <= 1:
                    break
                current = proc_root / str(parent)
        except (FileNotFoundError, ProcessLookupError):
            continue


def wait_for_shell_jobs():
    deadline = time.monotonic() + 5
    while True:
        try:
            guard_shell_jobs()
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def restart_stale_shell():
    state = json.loads((get_state_dir() / "state.json").read_text())
    if state.get("status") != "idle":
        raise RuntimeError("Barra manteve cache antigo; reinício recusado durante captura/processamento")
    wait_for_shell_jobs()
    run("omarchy", "restart", "shell", timeout=30)


def reload_panel(verify_new=True):
    wait_for_shell_jobs()
    run("omarchy-shell", "shell", "rescanPlugins")
    # O rescan é assíncrono. A presença do painel confirma que o componente
    # carregou, antes de declarar saudável uma barra que só responde a ping.
    for attempt in range(10):
        time.sleep(0.2)
        try:
            if verify_new and run("omarchy-shell", "castanha-view", "health") != "uploaded-audio-pending-v1":
                raise subprocess.CalledProcessError(1, "castanha-view health")
            run("omarchy-shell", "castanha", "open")
            return
        except subprocess.CalledProcessError:
            if attempt == 9:
                if not verify_new:
                    raise
                restart_stale_shell()
                if run("omarchy-shell", "castanha-view", "health") != "uploaded-audio-pending-v1":
                    raise RuntimeError("Interface nova não carregou após reiniciar barra")
                run("omarchy-shell", "castanha", "open")
                return


def deploy(root, sha):
    root = Path(root).resolve(strict=True)
    git = lambda *args: run("git", "-C", str(root), *args)
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise ValueError("SHA completo obrigatório")
    if git("status", "--porcelain"):
        raise ValueError("Checkout instalado precisa estar limpo")
    previous = git("rev-parse", "HEAD")
    git("merge-base", "--is-ancestor", previous, sha)
    validate_paths(git("diff", "--no-renames", "--name-only", previous, sha).splitlines())
    state_dir = get_state_dir()
    with open_lock(state_dir) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = snapshot(root)
        wait_for_shell_jobs()
        journal = state_dir / "local-ui-release.json"
        write_json(journal, {"previous": previous, "candidate": sha, "checkout": str(root),
                             "risk": "AMARELO", "phase": "applying"})
        try:
            guard_shell_jobs()
            git("merge", "--ff-only", sha)
            reload_panel()
            after = snapshot(root)
            if after["pids"] != before["pids"] or after["audio_path"] != before["audio_path"]:
                raise RuntimeError("Identidade da captura/daemon mudou durante a troca")
            if before["audio_path"]:
                deadline = time.monotonic() + 5
                while after["audio_bytes"] <= before["audio_bytes"] and time.monotonic() < deadline:
                    time.sleep(0.5)
                    after = snapshot(root)
                if (after["pids"] != before["pids"] or after["audio_path"] != before["audio_path"]
                        or after["audio_bytes"] <= before["audio_bytes"]):
                    raise RuntimeError("Não foi possível comprovar continuidade do áudio")
            run("omarchy-shell", "shell", "ping")
        except BaseException as original_error:
            try:
                wait_for_shell_jobs()
                git("reset", "--keep", previous)
                reload_panel(verify_new=False)
                restored = snapshot(root)
                run("omarchy-shell", "shell", "ping")
                write_json(journal, {"previous": previous, "candidate": sha, "phase": "rolled_back",
                                     "processes": restored})
            except BaseException as rollback_error:
                write_json(journal, {"previous": previous, "candidate": sha, "phase": "rollback_failed",
                                     "error": type(original_error).__name__, "rollback_error": type(rollback_error).__name__})
                raise rollback_error from original_error
            raise
        write_json(journal, {"previous": previous, "candidate": sha, "checkout": str(root),
                             "risk": "AMARELO", "phase": "healthy", "processes": before})
        write_json(state_dir / "local-release.json", {"previous": previous, "candidate": sha,
                   "checkout": str(root), "mode": "ui-only"})
    print("Painel atualizado; daemon e captura preservados: " + sha)


if __name__ == "__main__":
    deploy(*sys.argv[1:])
