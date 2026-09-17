"""Microfone sem eco: tira do canal do microfone o som que sai do alto-falante.

Sem fone, o microfone capta os participantes remotos e o ASR atribui a fala
deles ao canal local. O módulo echo-cancel do PipeWire (WebRTC) resolve isso com
`monitor.mode`: a referência é o monitor da saída, então nenhum aplicativo
precisa mudar de dispositivo e os padrões do sistema ficam intactos.

O nó vive num processo `pipewire -c` próprio, só durante a gravação. Este mesmo
arquivo roda como supervisor (`python3 -B echo_cancel.py <conf>`): recebe o PID
do FFmpeg pela entrada padrão e encerra o nó quando a captura termina, qualquer
que seja o comando (daemon ou CLI) que a finalize.
https://docs.pipewire.org/page_module_echo_cancel.html
"""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional

SOURCE_PREFIX = "castanha_mic_aec"
SOURCE_DESCRIPTION = "Microfone sem eco (Castanha)"
READY_TIMEOUT_SECONDS = 3.0
_PID_WAIT_SECONDS = 15.0

_CONF = """context.properties = {{ log.level = 0 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt args = {{ }} flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            audio.rate = 48000
            audio.channels = 2
            audio.position = [ FL FR ]
            # Ganho automático mudaria os níveis que classificam canal mudo.
            aec.args = {{ webrtc.gain_control = false }}
            capture.props = {{ node.name = "{name}_capture" target.object = "{mic}" }}
            sink.props = {{ node.name = "{name}_reference" target.object = "{sink}" }}
            # Prioridade mínima: nunca vira o microfone padrão de outro aplicativo.
            source.props = {{ node.name = "{name}" node.description = "{description}"
                             priority.session = 1 priority.driver = 1 }}
        }}
    }}
]
"""


def _quoted(value: str) -> str:
    if not value or any(ch in value for ch in '"\\\n{}[]'):
        raise ValueError(f"Nome de dispositivo inválido para o PipeWire: {value!r}")
    return value


def render_conf(name: str, mic: str, sink: str) -> str:
    return _CONF.format(name=_quoted(name), mic=_quoted(mic), sink=_quoted(sink),
                        description=SOURCE_DESCRIPTION)


def _source_exists(name: str) -> bool:
    try:
        listing = subprocess.run(["pactl", "list", "short", "sources"],
                                 capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split("\t")[1:2] == [name] for line in listing.stdout.splitlines())


class EchoCancel:
    """Nó de microfone sem eco em uso por UMA gravação."""

    def __init__(self, source: str, supervisor: subprocess.Popen):
        self.source = source
        self._supervisor = supervisor

    def bind(self, capture_pid: int) -> bool:
        """Entrega ao supervisor o PID da captura; o nó morre junto com ela.

        False: o supervisor já tinha saído e o FFmpeg caiu no microfone cru.
        """
        try:
            self._supervisor.stdin.write(f"{capture_pid}\n".encode())
            self._supervisor.stdin.close()
            return True
        except (OSError, ValueError):
            self.close()
            return False

    def close(self) -> None:
        """Encerra o nó e recolhe o supervisor; o daemon não acumula zumbis."""
        try:
            self._supervisor.terminate()
            self._supervisor.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Supervisor é líder da própria sessão: o grupo leva o nó junto.
            try:
                os.killpg(self._supervisor.pid, signal.SIGKILL)
            except OSError:
                self._supervisor.kill()
            try:
                self._supervisor.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass
        if self._supervisor.stdin and not self._supervisor.stdin.closed:
            try:
                self._supervisor.stdin.close()
            except OSError:
                pass


def start(mic: str, sink: str) -> Optional[EchoCancel]:
    """Sobe o nó e devolve a fonte pronta, ou None: a gravação segue no mic cru."""
    name = f"{SOURCE_PREFIX}_{os.getpid()}_{int(time.time() * 1000)}"
    conf_path = None
    supervisor = None
    try:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        fd, conf_path = tempfile.mkstemp(prefix="castanha-aec-", suffix=".conf", dir=runtime)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_conf(name, mic, sink))
        supervisor = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), conf_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid)
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline and supervisor.poll() is None:
            if _source_exists(name):
                return EchoCancel(name, supervisor)
            time.sleep(0.1)
    except (OSError, ValueError):
        pass
    if supervisor is not None:
        EchoCancel(name, supervisor).close()
    if conf_path:
        # Supervisor que morreu antes de instalar os handlers não limpa a conf.
        Path(conf_path).unlink(missing_ok=True)
    return None


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def _supervise(conf_path: str) -> int:
    node = None

    def finish(*_):
        if node is not None:
            node.terminate()
            try:
                node.wait(timeout=3)
            except subprocess.TimeoutExpired:
                node.kill()
        Path(conf_path).unlink(missing_ok=True)
        os._exit(0)

    # Handlers antes do nó: sinal recebido durante o Popen não deixa órfão.
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    # Sinal entre o fork e a atribuição de `node` deixaria o nó órfão: fica
    # pendente até o supervisor conhecer o filho. O filho nasce desbloqueado.
    stop_signals = {signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, stop_signals)
    try:
        node = subprocess.Popen(["pipewire", "-c", conf_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals))
    except OSError:
        finish()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals)

    # Espera o PID da captura vigiando o nó: nó que cai cedo libera o start na
    # hora, e quem iniciou e morreu sem entregar o PID não deixa o nó vivo.
    line = ""
    deadline = time.monotonic() + _PID_WAIT_SECONDS
    while time.monotonic() < deadline and node.poll() is None:
        if select.select([sys.stdin], [], [], 0.2)[0]:
            line = sys.stdin.readline().strip()
            break
    capture_pid = int(line) if line.isdigit() else 0
    while capture_pid and _alive(capture_pid) and node.poll() is None:
        time.sleep(1)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(_supervise(sys.argv[1]))
