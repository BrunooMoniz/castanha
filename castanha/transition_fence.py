"""Cerca inicial para o checkout b97131e, com troca atômica e reversão por inode.

A cerca fecha imports pelo pacote comum, inclusive bytecode. Não é um sandbox
contra código copiado, importadores próprios ou adulteração pelo mesmo usuário.
"""
import ctypes
import hashlib
import json
import importlib.util
import marshal
import types
import os
from pathlib import Path
import stat
import sys
import subprocess
import uuid

from castanha.durability import sync_directory, write_json

LEGACY_SHA = "b97131edbb18cbca0e11b959dc8f92a4b52fa171"
RECEIPT = "transition-fence.json"
STUB = ('"""Castanha temporariamente cercado para instalação reversível."""\n'
        'raise SystemExit("Castanha: transição em andamento; gravação recusada.")\n')


class FenceError(RuntimeError):
    pass


def checked_path(path, *, directory=True):
    """Não normalizar um alias inesperado para depois declará-lo seguro."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise FenceError("Cerca exige caminho absoluto sem aliases")
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if part == path and not directory:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise FenceError("Diretório ou ancestral é link/tipo inesperado")
        if info.st_uid not in {0, os.getuid()}:
            raise FenceError("Dono de diretório inesperado")
        # /tmp root-owned com sticky é um ancestral legítimo das fixtures.
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise FenceError("Diretório gravável por terceiros")
        if not info.st_mode & stat.S_IXUSR:
            raise FenceError("Diretório sem permissão de busca")
    return path


def file_record(path):
    checked_path(path, directory=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or info.st_mode & 0o7022 or
                not info.st_mode & stat.S_IRUSR):
            raise FenceError("Arquivo com tipo, dono, hardlink ou permissão inesperados")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise FenceError("Arquivo mudou durante inventário")
        return {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid, "gid": info.st_gid, "sha256": digest.hexdigest()}
    finally:
        os.close(descriptor)


def tree_record(path):
    checked_path(path)
    info = path.lstat()
    result = {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode),
              "uid": info.st_uid, "gid": info.st_gid, "entries": {}}
    for child in sorted(path.iterdir()):
        if child.is_symlink():
            raise FenceError("Symlink dentro do pacote recusado")
        result["entries"][child.name] = (tree_record(child) if child.is_dir()
                                          else file_record(child))
    return result


def verified_cache(package):
    """Aceita apenas bytecode equivalente às fontes, nunca cache só pelo nome."""
    cache = package / "__pycache__"
    if not cache.exists():
        return
    checked_path(cache)
    for path in cache.iterdir():
        file_record(path)
        parts = path.name.split(".")
        if (len(parts) not in (3, 4) or parts[-1] != "pyc" or
                parts[1] != sys.implementation.cache_tag or
                (len(parts) == 4 and parts[2] not in {"opt-1", "opt-2"})):
            raise FenceError("Cache Python não reconhecido")
        source = package / (parts[0] + ".py")
        file_record(source)
        data = path.read_bytes()
        if data[:4] != importlib.util.MAGIC_NUMBER or len(data) < 16:
            raise FenceError("Cache Python inválido")
        def normalize(code):
            if not isinstance(code, types.CodeType):
                raise FenceError("Cache não contém código Python")
            return code.replace(co_filename="<verified>", co_consts=tuple(
                normalize(c) if isinstance(c, types.CodeType) else c for c in code.co_consts))
        try:
            actual = normalize(marshal.loads(data[16:]))
            expected = normalize(compile(source.read_bytes(), str(source), "exec",
                                         optimize=int(parts[2][-1]) if len(parts) == 4 else 0))
        except (ValueError, EOFError, TypeError, SyntaxError) as exc:
            raise FenceError("Cache Python inválido") from exc
        if actual != expected:
            raise FenceError("Cache diverge da fonte auditada")


def stub_record(path):
    verified_cache(path)
    result = tree_record(path)
    result["entries"].pop("__pycache__", None)
    return result


def link_record(path, expected):
    checked_path(path, directory=False)
    info = path.lstat()
    if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1):
        raise FenceError("Entrypoint não é symlink único do usuário")
    target = os.readlink(path)
    absolute = Path(os.path.abspath(path.parent / target))
    if absolute != expected:
        raise FenceError("Entrypoint aponta por alias ou para alvo inesperado")
    return {"target": target, "dev": info.st_dev, "ino": info.st_ino,
            "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}


def exchange(left, right):
    """Uma syscall, sem fallback com duas renomeações e janela aberta."""
    checked_path(left.parent)
    checked_path(right.parent)
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise FenceError("Kernel/libc sem renameat2; cerca indisponível") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    descriptors = []
    try:
        for path in (left, right):
            descriptors.append(os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        if rename(descriptors[0], os.fsencode(left.name), descriptors[1], os.fsencode(right.name), 2):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        for descriptor in descriptors:
            os.fsync(descriptor)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def sync_tree(path):
    """Torna os bytes originais duráveis antes de publicar o recibo de troca."""
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            sync_tree(child)
        else:
            file_record(child)
            descriptor = os.open(child, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    sync_directory(path)


def git(root, *args):
    return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                          capture_output=True, check=True, timeout=15).stdout


def verify_legacy(root):
    """Somente a revisão auditada, sem código extra nem caches não verificados."""
    if git(root, "rev-parse", "HEAD").decode().strip() != LEGACY_SHA:
        raise FenceError("Cerca inicial aceita somente b97131e exato")
    names = git(root, "ls-tree", "-r", "--name-only", LEGACY_SHA, "castanha", "bin/castanha").decode().splitlines()
    package = tree_record(root / "castanha")
    verified_cache(root / "castanha")
    if (set(package["entries"]) - {"__pycache__"}) != {Path(name).name for name in names if name.startswith("castanha/")}:
        raise FenceError("Pacote legado contém arquivos/caches não auditados")
    for name in names:
        content = git(root, "show", f"{LEGACY_SHA}:{name}")
        if file_record(root / name)["sha256"] != hashlib.sha256(content).hexdigest():
            raise FenceError("Bytes do legado divergem de b97131e")
    return package


class TransitionFence:
    def __init__(self, root, state_dir, cli_link, plugin_link, *, checkpoint=None):
        self.root, self.state = Path(root), Path(state_dir)
        self.cli, self.plugin = Path(cli_link), Path(plugin_link)
        self.receipt = self.state / RECEIPT
        self.checkpoint = checkpoint or (lambda phase: None)

    def inspect(self):
        checked_path(self.root)
        checked_path(self.state)
        if self.root == self.state or self.root in self.state.parents:
            raise FenceError("Recibo precisa ficar fora do checkout cercado")
        package = verify_legacy(self.root)
        if self.receipt.exists() or self.receipt.is_symlink():
            previous = self.load()
            if previous["phase"] != "restored":
                raise FenceError("Cerca anterior exige recuperação")
        return {"root": str(self.root), "legacy_sha": LEGACY_SHA,
                "package": package, "cli": link_record(self.cli, self.root / "bin/castanha"),
                "plugin": link_record(self.plugin, self.root),
                "cli_path": str(self.cli), "plugin_path": str(self.plugin),
                "script": file_record(self.root / "bin/castanha")}

    def save(self, record, phase):
        record["phase"] = phase
        write_json(self.receipt, record)
        self.checkpoint(phase)

    def load(self):
        info = file_record(self.receipt)
        if info["mode"] != 0o600:
            raise FenceError("Recibo com permissão inesperada")
        record = json.loads(self.receipt.read_text())
        if (record.get("root") != str(self.root) or record.get("legacy_sha") != LEGACY_SHA or
                record.get("cli_path") != str(self.cli) or record.get("plugin_path") != str(self.plugin)):
            raise FenceError("Recibo pertence a outros caminhos")
        slot = Path(record["slot"])
        suffix = slot.name.removeprefix(".castanha-fence-")
        if (not slot.name.startswith(".castanha-fence-") or
                slot.parent != self.root.parent or len(suffix) != 32 or
                any(c not in "0123456789abcdef" for c in suffix)):
            raise FenceError("Backup fora da fronteira do recibo")
        return record

    def prepare(self):
        record = self.inspect()
        sync_tree(self.root / "castanha")
        slot = self.root.parent / (".castanha-fence-" + uuid.uuid4().hex)
        record["slot"] = str(slot)
        self.save(record, "preparing")
        slot.mkdir(mode=0o700)
        sync_directory(slot.parent)
        self.checkpoint("slot_created")
        stub = slot / "__init__.py"
        fd = os.open(stub, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(STUB)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(slot)
        self.checkpoint("stub_written")
        record["stub"] = tree_record(slot)
        self.save(record, "prepared")
        return record

    def activate(self, record):
        if tree_record(self.root / "castanha") != record["package"]:
            raise FenceError("Legado mudou antes da cerca")
        self.assert_links(record)
        if file_record(self.root / "bin/castanha") != record["script"]:
            raise FenceError("CLI legado mudou antes da cerca")
        slot = Path(record["slot"])
        if stub_record(slot) != record["stub"]:
            raise FenceError("Bloqueador mudou antes da cerca")
        self.save(record, "exchanging")
        exchange(self.root / "castanha", slot)
        self.checkpoint("exchanged")
        self.assert_active(record)
        self.save(record, "fenced")

    def assert_links(self, record):
        if (link_record(self.cli, self.root / "bin/castanha") != record["cli"] or
                link_record(self.plugin, self.root) != record["plugin"]):
            raise FenceError("Links legados mudaram antes da cerca")

    def assert_active(self, record=None):
        record = record or self.load()
        if (stub_record(self.root / "castanha") != record["stub"] or
                tree_record(Path(record["slot"])) != record["package"] or
                file_record(self.root / "bin/castanha") != record["script"]):
            raise FenceError("Cerca ou backup mudou; transição recusada")

    def restore(self):
        """Orienta pelos inodes, inclusive crash entre exchange e gravação da fase."""
        record = self.load()
        package = self.root / "castanha"
        current = tree_record(package)
        if current == record["package"]:
            self.save(record, "restored")
            return
        if "stub" not in record or stub_record(package) != record["stub"]:
            raise FenceError("Pacote alterado por terceiro; recuperação recusada")
        self.assert_active(record)
        self.save(record, "restoring")
        exchange(package, Path(record["slot"]))
        self.checkpoint("restore_exchanged")
        if tree_record(package) != record["package"]:
            raise FenceError("Restauração não preservou pacote")
        self.save(record, "restored")

    def assert_no_readers(self, *, proc=Path("/proc")):
        """Recusa imediatamente; nenhuma sequência de scans limpos é licença.

        Depois do exchange um exec novo não importa o legado. O b97131e não
        faz fork de runtime carregado: seus filhos passam por exec e reimportam
        a cerca, ou são ffmpeg (checado DEPOIS pelo preflight de captura).
        """
        from castanha.deployment import scan_processes
        record = self.load()
        self.assert_active(record)
        paths = {str(self.cli), str(self.plugin / "bin/castanha"),
                 str(self.root / "bin/castanha")}
        roots = (self.root, Path(record["slot"]))
        # O arquivo pode já estar fechado e o cwd pode ser externo. Usar o
        # inventário ANTERIOR ao exchange: no caminho atual só existe o stub.
        def package_files(tree, prefix=Path()):
            for name, item in tree["entries"].items():
                relative = prefix / name
                if "entries" in item:
                    yield from package_files(item, relative)
                else:
                    yield relative
        for relative in package_files(record["package"]):
            paths.update(str(root / relative) for root in (
                self.root / "castanha", self.plugin / "castanha", roots[1]))

        def selected(entry, argv):
            if int(entry.name) == os.getpid():
                return False
            if any(os.path.isabs(arg) and os.path.abspath(arg) in paths for arg in argv):
                return True
            # -m é conservador mesmo quando PYTHONPATH selecionou o legado
            # desde outro cwd. Não tentar adivinhar ambiente de processo vivo.
            if any(argv[i] == "-m" and argv[i + 1].startswith("castanha.")
                   for i in range(len(argv) - 1)):
                return True
            cwd = (entry / "cwd").resolve(strict=True)
            if any(cwd == root or root in cwd.parents for root in roots):
                return True
            for arg in argv:
                if not os.path.isabs(arg) and str(Path(os.path.abspath(cwd / arg))) in paths:
                    return True
            # Inclui quem abriu um arquivo/diretório imediatamente antes da
            # cerca e ainda não apareceu como CLI no argv.
            for descriptor in (entry / "fd").iterdir():
                try:
                    target = Path(os.readlink(descriptor))
                except FileNotFoundError:
                    # FD fechado é diferente de /proc/PID ilegível.
                    try:
                        descriptor.lstat()
                    except FileNotFoundError:
                        continue
                    raise
                if any(target == root or root in target.parents for root in roots):
                    return True
            return False
        readers = scan_processes(proc, selected)
        if readers:
            raise FenceError(f"Legado aberto antes da cerca (PID {readers[0]}); swap recusado")
        self.assert_active(record)
