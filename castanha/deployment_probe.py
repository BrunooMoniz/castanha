"""Probe interno do instalador, executado em subprocesso com HOME descartável."""
import errno
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch


def main():
    candidate, lock_path, mode = sys.argv[1:]
    sys.path.insert(0, candidate)
    from castanha import engine as module

    engine = module.CastanhaEngine.__new__(module.CastanhaEngine)
    engine.config = {}
    engine.state_mgr = SimpleNamespace(state_dir=Path(lock_path).parent,
                                       state_file=Path(lock_path).parent / "state.json")
    visited = []

    def assert_owned(stage):
        if mode == "held":
            raise AssertionError("Start não recusou antes de ler estado")
        for operation in (fcntl.LOCK_EX, fcntl.LOCK_SH):
            with open(lock_path, "a") as contender:
                try:
                    fcntl.flock(contender, operation | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                else:
                    raise AssertionError("Start não segura o mesmo lock exclusivo")
        visited.append(stage)

    def read():
        assert_owned("read")
        return {"status": "idle"}

    def start(*args, **kwargs):
        assert_owned("capture")
        return SimpleNamespace(pid=424242)

    def write(state):
        assert_owned("publish")
        assert state["status"] == "recording" and state["pid"] == 424242
        return state

    engine.state_mgr.read = read
    engine.state_mgr.write = write
    engine.recorder = SimpleNamespace(
        start=start,
        peak_path=Path(lock_path).parent / "capture.peak",
    )
    engine.storage = Mock()
    # Mesmo um start incorreto não pode disparar áudio ou subprocessos reais.
    with patch("subprocess.Popen", side_effect=AssertionError("Processo externo no probe")), \
            patch.object(module, "notify", return_value=None, create=True), \
            patch.object(module, "is_default_source_muted", return_value=False, create=True):
        result = engine.start_recording()
    if mode == "held":
        assert not visited and result.get("status") == "error"
    else:
        assert visited == ["read", "capture", "publish"]
        assert result.get("status") == "recording"
        with open(lock_path, "a") as released:
            fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(json.dumps({"lock_contract": mode}))


if __name__ == "__main__":
    main()
