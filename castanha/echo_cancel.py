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

    def bind(self, capture_pid: int) -> None:
        """Entrega ao supervisor o PID da captura; o nó morre junto com ela."""
        try:
            self._supervisor.stdin.write(f"{capture_pid}\n".encode())
            self._supervisor.stdin.close()
        except (OSError, ValueError):
            self.close()

    def close(self) -> None:
        """Encerra o nó e recolhe o supervisor; o daemon não acumula zumbis."""
        try:
            self._supervisor.terminate()
            self._supervisor.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._supervisor.kill()
            self._supervisor.wait(timeout=2)
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
    elif conf_path:
        Path(conf_path).unlink(missing_ok=True)
    return None


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def _supervise(conf_path: str) -> int:
    node = subprocess.Popen(["pipewire", "-c", conf_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def finish(*_):
        node.terminate()
        try:
            node.wait(timeout=3)
        except subprocess.TimeoutExpired:
            node.kill()
        Path(conf_path).unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    signal.signal(signal.SIGALRM, finish)
    signal.alarm(int(_PID_WAIT_SECONDS))  # quem iniciou morreu antes de entregar o PID
    line = sys.stdin.readline().strip()
    signal.alarm(0)
    capture_pid = int(line) if line.isdigit() else 0
    while capture_pid and _alive(capture_pid) and node.poll() is None:
        time.sleep(1)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(_supervise(sys.argv[1]))
