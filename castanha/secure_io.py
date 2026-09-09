"""E/S endurecida para segredos e para corpos HTTP remotos.

Duas classes de risco que o resto do código não deve ter que lembrar:

1. **Credencial em arquivo.** O `config.json` guarda chaves de Groq, Deepgram,
   LLM, VPS e Zinom. Criar o arquivo com o modo padrão (`0644` sob umask 022)
   deixa qualquer outro usuário local ler os segredos, e abrir por *caminho*
   permite que um symlink plantado antes da escrita redirecione o conteúdo para
   fora do diretório do usuário. Por isso o diretório é criado e verificado como
   `0700`, a escrita é atômica (arquivo temporário no mesmo diretório + rename) e
   a leitura é feita por descritor, sem seguir link e recusando o que não for um
   arquivo comum de dono certo.

2. **Corpo de resposta sem limite.** `resp.read()` sem argumento deixa o tamanho
   do buffer na mão do servidor remoto: um endpoint hostil (ou apenas quebrado)
   responde um stream infinito e o processo consome memória até morrer. Toda
   leitura de rede passa a ser limitada e o excesso é recusado antes do parse.
"""

import errno
import io
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# Modos exigidos: diretório só do dono, arquivo de segredo só do dono.
DIR_MODE = 0o700
FILE_MODE = 0o600

# Teto padrão para um corpo de resposta JSON (8 MiB). Transcrição de reunião
# longa cabe com folga; um stream sem fim, não.
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024


class InsecureConfigError(RuntimeError):
    """O caminho da configuração não é seguro para guardar credencial."""


class ResponseTooLarge(RuntimeError):
    """O corpo remoto passou do limite e foi recusado sem ser interpretado."""


def ensure_private_dir(path: Path) -> Path:
    """Garante um diretório dono-somente (`0700`), sem seguir symlink.

    Um diretório pré-existente com modo frouxo é *corrigido*, não aceito: o
    arquivo de credencial dentro dele só é privado se o pai também for.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)

    # lstat, não stat: se o "diretório" for um symlink, isto tem que aparecer.
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise InsecureConfigError(f"{path} é um symlink; esperado diretório real")
    if not stat.S_ISDIR(info.st_mode):
        raise InsecureConfigError(f"{path} não é um diretório")
    if info.st_uid != os.geteuid():
        raise InsecureConfigError(f"{path} pertence a outro usuário (uid {info.st_uid})")
    if stat.S_IMODE(info.st_mode) != DIR_MODE:
        os.chmod(path, DIR_MODE)
    return path


def _open_no_follow(path: Path) -> int:
    """Abre para leitura sem seguir symlink e sem bloquear em FIFO.

    `O_NOFOLLOW` recusa o link plantado no lugar do arquivo; `O_NONBLOCK` evita
    que um FIFO no mesmo caminho congele o processo na abertura.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    return os.open(path, flags)


def read_private_json(path: Path, max_bytes: int = 4 * 1024 * 1024) -> Optional[Dict[str, Any]]:
    """Lê um JSON de credencial por descritor, validando o que foi aberto.

    Devolve `None` quando o arquivo não existe. Levanta `InsecureConfigError`
    quando o caminho existe mas não é um arquivo comum do próprio usuário — o
    chamador não deve tratar isso como "config vazia" e seguir em frente.
    """
    path = Path(path)
    try:
        fd = _open_no_follow(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP é o próprio O_NOFOLLOW recusando um symlink no caminho.
        if exc.errno == errno.ELOOP:
            raise InsecureConfigError(f"{path} é um symlink; recusado") from exc
        raise

    try:
        info = os.fstat(fd)
        # A validação é sobre o descritor já aberto, não sobre o caminho: entre
        # um stat por caminho e o open, o alvo pode ter sido trocado.
        if not stat.S_ISREG(info.st_mode):
            raise InsecureConfigError(f"{path} não é um arquivo comum")
        if info.st_uid != os.geteuid():
            raise InsecureConfigError(f"{path} pertence a outro usuário (uid {info.st_uid})")
        if info.st_size > max_bytes:
            raise InsecureConfigError(f"{path} tem {info.st_size} bytes; máximo {max_bytes}")

        # Modo frouxo em arquivo que já contém segredo: fecha o acesso agora,
        # em vez de recusar e deixar o usuário sem configuração.
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, FILE_MODE)

        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            # Lê 1 byte além do teto para detectar excesso sem confiar no st_size.
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise InsecureConfigError(f"{path} passou de {max_bytes} bytes na leitura")
    finally:
        os.close(fd)

    if not raw.strip():
        return None
    return json.loads(raw)


def write_private_json(path: Path, data: Dict[str, Any]) -> Path:
    """Publica um JSON de credencial atomicamente como `0600` do dono.

    O temporário nasce no mesmo diretório (rename atômico exige mesmo
    filesystem) já com modo restrito, então o conteúdo nunca existe em disco
    sob um modo legível por outros — nem por uma janela curta.
    """
    path = Path(path)
    ensure_private_dir(path.parent)

    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            # fsync antes do rename: sem isto, um corte de energia publica um
            # arquivo de tamanho certo e conteúdo vazio.
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # O rename preserva o modo do temporário, mas um arquivo de destino
    # pré-existente com modo frouxo teria sido substituído — reafirma.
    os.chmod(path, FILE_MODE)
    return path


def read_bounded(resp: Any, max_bytes: int = MAX_HTTP_BODY_BYTES) -> bytes:
    """Lê no máximo `max_bytes` de um corpo de resposta e recusa o excesso.

    O limite é aplicado do lado de quem lê, não confiando no `Content-Length`
    anunciado: o cabeçalho pode mentir ou faltar (`Transfer-Encoding: chunked`).
    Lê um byte além do teto para distinguir "exatamente no limite" de "cortado".
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes deve ser positivo")

    declared = None
    try:
        raw_len = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
        declared = int(raw_len) if raw_len is not None else None
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > max_bytes:
        raise ResponseTooLarge(
            f"resposta anuncia {declared} bytes; máximo {max_bytes}"
        )

    buffer = io.BytesIO()
    restante = max_bytes + 1
    while restante > 0:
        # Pedaços de 64 KiB: o read() sem limite é justamente o problema.
        pedaco = resp.read(min(65536, restante))
        if not pedaco:
            break
        buffer.write(pedaco)
        restante -= len(pedaco)

    corpo = buffer.getvalue()
    if len(corpo) > max_bytes:
        raise ResponseTooLarge(f"resposta passou de {max_bytes} bytes; recusada")
    return corpo


def read_json_bounded(resp: Any, max_bytes: int = MAX_HTTP_BODY_BYTES) -> Any:
    """Corpo limitado e só então interpretado como JSON."""
    return json.loads(read_bounded(resp, max_bytes).decode("utf-8", "replace"))


def read_text_bounded(resp: Any, max_bytes: int = MAX_HTTP_BODY_BYTES) -> str:
    """Corpo limitado como texto (feeds iCal, respostas não-JSON)."""
    return read_bounded(resp, max_bytes).decode("utf-8", "replace")
