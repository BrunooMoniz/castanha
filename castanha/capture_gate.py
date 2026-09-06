"""Trava comum ao instalador e ao início de captura, inclusive dentro do daemon."""
import fcntl
import os
import stat
from functools import wraps

from castanha.config import get_state_dir

LOCK_NAME = ".deployment.lock"


def open_lock(state_dir):
    """Nunca seguir link/hardlink para uma trava diferente da administrada."""
    directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or info.st_mode & 0o7022 or
                info.st_mode & 0o600 != 0o600):
            raise OSError("Trava com tipo, dono, links ou permissão inesperados")
        return os.fdopen(descriptor, "r+")
    except BaseException:
        os.close(descriptor)
        raise


def capture_start(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            lock = open_lock(get_state_dir())
        except OSError:
            return {"status": "error", "message": "Trava de instalação indisponível; gravação recusada."}
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"status": "error", "message": "Instalação ou outra captura em andamento; tente novamente."}
            # O close libera inclusive se o start falhar. Nenhum erro de áudio
            # ou publicação pode ser confundido com contenção da trava.
            return function(*args, **kwargs)
    return guarded
