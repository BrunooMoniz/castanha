"""Mecanismo de captura de áudio com PipeWire / FFmpeg."""

import os
import json
import math
import re
import signal
import stat as stat_mode
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from castanha.i18n import audio_status_message

@dataclass
class AudioDeviceInfo:
    source: str
    sink: str
    monitor: str
    source_name: Optional[str] = None
    monitor_name: Optional[str] = None

@dataclass
class RecordingResult:
    audio_path: Path
    duration_seconds: float
    file_size_bytes: int
    mode: str
    format: str

def check_dependencies() -> None:
    for cmd in ["ffmpeg", "ffprobe", "pactl"]:
        res = subprocess.run(["which", cmd], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Dependência obrigatória não encontrada no sistema: {cmd}")

def get_audio_devices() -> AudioDeviceInfo:
    check_dependencies()
    source = "default"
    sink = "default"
    monitor = "default.monitor"

    try:
        src_res = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, check=True)
        if src_res.stdout.strip():
            source = src_res.stdout.strip()
    except Exception:
        pass

    try:
        sink_res = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True)
        if sink_res.stdout.strip():
            sink = sink_res.stdout.strip()
            monitor = f"{sink}.monitor"
    except Exception:
        pass

    names = {}
    try:
        listing = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                                 capture_output=True, text=True, check=True, timeout=5)
        names = {item["name"]: item.get("description") or item["name"]
                 for item in json.loads(listing.stdout) if isinstance(item, dict) and item.get("name")}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # The exact capture source remains a truthful fallback if labels fail.
        pass
    return AudioDeviceInfo(source=source, sink=sink, monitor=monitor,
                           source_name=names.get(source, source), monitor_name=names.get(monitor, monitor))

class AudioRecorder:
    def __init__(self):
        self.devices = None
        self.process: Optional[subprocess.Popen] = None
        self.output_path: Optional[Path] = None
        self.start_time: Optional[float] = None
        self.mode: str = "dual"
        self.peak_path: Optional[Path] = None
        self.mic_peak_path: Optional[Path] = None
        self.call_peak_path: Optional[Path] = None

    def is_recording(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def start(self, output_path: Path, mode: str = "dual", bitrate: str = "64k") -> subprocess.Popen:
        if self.is_recording():
            raise RuntimeError("Uma gravação já está em andamento.")

        if self.devices is None:
            self.devices = get_audio_devices()
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.peak_path = self._peak_path(self.output_path)
        self.mic_peak_path = self._peak_path(self.output_path, "mic")
        self.call_peak_path = self._peak_path(self.output_path, "call") if mode == "dual" else None
        for field in ("peak_path", "mic_peak_path", "call_peak_path"):
            path = getattr(self, field)
            if path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    setattr(self, field, None)

        # Garante caminhos e fontes
        if mode == "dual":
            # Canal esquerdo: Microfone (usuário)
            # Canal direito: Monitor do som do sistema (participantes remotos)
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "pulse", "-i", self.devices.source,
                "-f", "pulse", "-i", self.devices.monitor,
                "-filter_complex",
                "[0:a]pan=mono|c0=0.5*c0+0.5*c1[mic];[1:a]pan=mono|c0=0.5*c0+0.5*c1[sys];"
                "[mic][sys]amerge=inputs=2[mix];[mix]asplit=2[aout][meterin];"
                f"[meterin]{self._meter_stats()}[meter]",
                "-map", "[aout]",
                "-c:a", "libopus",
                "-b:a", bitrate,
                str(self.output_path),
                "-map", "[meter]", "-f", "null", "-",
            ]
        elif mode == "mic_only":
            # Reunião presencial: grava apenas microfone físico
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "pulse", "-i", self.devices.source,
                "-filter_complex",
                f"[0:a]asplit=2[aout][meterin];[meterin]{self._meter_stats()}[meter]",
                "-map", "[aout]",
                "-c:a", "libopus",
                "-b:a", "32k",
                str(self.output_path),
                "-map", "[meter]", "-f", "null", "-",
            ]
        else:
            raise ValueError(f"Modo de gravação desconhecido: {mode}")

        self.start_time = time.time()
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,  # Isolamento de grupo de processos
            )
        except OSError:
            self.stop_meter()
            raise
        return self.process

    @staticmethod
    def _peak_path(output_path: Path, channel: Optional[str] = None) -> Path:
        if channel not in (None, "mic", "call"):
            raise ValueError("Canal de medição desconhecido")
        suffix = f".{channel}.peak" if channel else ".peak"
        return output_path.with_name(output_path.stem + suffix)

    @staticmethod
    def _filter_path(path: Path) -> str:
        # A opção file= usa a sintaxe do filtro do FFmpeg, que reserva ':' e '\\'.
        return str(path).replace('\\', '\\\\').replace(':', '\\:')

    def _meter_stats(self) -> str:
        outputs = [(self.peak_path, "Overall")]
        outputs.append((self.mic_peak_path, "1" if self.mode == "dual" else "Overall"))
        if self.mode == "dual":
            outputs.append((self.call_peak_path, "2"))
        outputs = [(path, channel) for path, channel in outputs if path]
        if not outputs:
            return "anull"
        # Metadata comes from the same pre-Opus capture, never decoded lossy
        # stereo (which can leak energy between channels) or another PW client.
        # https://ffmpeg.org/ffmpeg-filters.html#astats-1
        stats = ["asetnsamples=n=12000:p=1",
                 "astats=metadata=1:reset=1:measure_perchannel=Peak_level:measure_overall=Peak_level"]
        stats.extend(f"ametadata=mode=print:key=lavfi.astats.{channel}.Peak_level:"
                     f"file={self._filter_path(path)}:direct=1" for path, channel in outputs)
        return ",".join(stats)

    def stop_meter(self) -> None:
        # All three files belong to the main capture process; no extra recorder.
        for field in ("peak_path", "mic_peak_path", "call_peak_path"):
            path = getattr(self, field)
            if path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            setattr(self, field, None)

    def pause(self) -> None:
        if self.process and self.is_recording():
            os.killpg(os.getpgid(self.process.pid), signal.SIGSTOP)

    def resume(self) -> None:
        if self.process and self.is_recording():
            os.killpg(os.getpgid(self.process.pid), signal.SIGCONT)

    def stop(self) -> RecordingResult:
        if not self.process:
            raise RuntimeError("Nenhuma gravação em andamento para parar.")

        # Finalização graciosa com SIGINT para o FFmpeg fechar os headers do container Ogg/Opus
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self.process.wait(timeout=2)
        except ProcessLookupError:
            pass

        duration = time.time() - (self.start_time or time.time())
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.process = None
        self.stop_meter()

        if not self.output_path or not self.output_path.exists():
            raise FileNotFoundError("O arquivo de áudio resultante não foi gerado.")

        file_size = self.output_path.stat().st_size
        if file_size == 0:
            raise RuntimeError("O arquivo de áudio gerado está vazio (0 bytes).")

        return RecordingResult(
            audio_path=self.output_path,
            duration_seconds=round(duration, 2),
            file_size_bytes=file_size,
            mode=self.mode,
            format=self.output_path.suffix.lstrip("."),
        )


_PEAK_LINE = re.compile(r"lavfi\.astats\.(?:Overall|1|2)\.Peak_level=(-?(?:inf|\d+(?:\.\d+)?))")
_CAPTURE_NAME = re.compile(r"castanha_rec_\d+\.ogg\Z")


def is_safe_capture_peak(audio_path: Optional[Path], peak_path: Optional[Path]) -> bool:
    """Aceita limpeza apenas do artefato que o próprio start derivou.

    O caminho vem do state.json, portanto não pode ser usado como autorização
    para apagar um arquivo arbitrário. Capturas do Castanha nascem em /tmp
    com o nome abaixo, e o pico válido é exatamente o mesmo nome com .peak.
    """
    if not audio_path or not peak_path:
        return False
    audio = Path(audio_path)
    peak = Path(peak_path)
    return (audio.parent == Path("/tmp")
            and bool(_CAPTURE_NAME.fullmatch(audio.name))
            and peak in {AudioRecorder._peak_path(audio, channel) for channel in (None, "mic", "call")})


def read_audio_peak(path: Optional[Path], max_age: float = 1.5) -> Optional[float]:
    sample = read_audio_peak_sample(path, max_age)
    return sample[0] if sample else None


def read_audio_peak_sample(path: Optional[Path], max_age: float = 1.5):
    """Lê o último pico do output auxiliar do FFmpeg sem travar a captura.

    O output auxiliar escreve uma amostra a cada 250 ms. A janela curta transforma
    processo parado, pausa ou silêncio em zero, em vez de congelar a última
    barrinha na UI.
    """
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            stat = os.fstat(stream.fileno())
            if not stat_mode.S_ISREG(stat.st_mode):
                return None
            if not -0.5 <= time.time() - stat.st_mtime <= max_age:
                return None
            stream.seek(max(0, stat.st_size - 8192))
            text = stream.read().decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    matches = _PEAK_LINE.findall(text)
    if not matches:
        return None
    raw = matches[-1]
    if raw == "-inf":
        return 0.0, stat.st_mtime
    try:
        db = float(raw)
    except ValueError:
        return None
    if not math.isfinite(db):
        return None
    linear = 10.0 ** (min(0.0, db) / 20.0)
    # A mesma compressão perceptual (cúbica) usada pelo visualizador do
    # Quickshell, para que o valor do sidecar preserve a escala já aprovada.
    return max(0.0, min(1.0, linear ** (1.0 / 3.0))), stat.st_mtime


def capture_channel_peaks(state):
    """Independent samples: zero + timestamp 0 means unavailable, not silence."""
    updates = {}
    for channel in ("mic", "call"):
        path = state.get(f"{channel}_peak_path")
        enabled = state.get("status") == "recording" and (channel == "mic" or state.get("mode") == "dual")
        sample = read_audio_peak_sample(Path(path)) if path and enabled else None
        updates[f"{channel}_peak"] = sample[0] if sample else 0.0
        updates[f"{channel}_peak_updated_at"] = sample[1] if sample else 0.0
    return updates


# ---------------------------------------------------------------------------
# Verificação de sanidade do áudio
#
# O teste de 2026-09-04 gravou 35s com o microfone mudo no teclado da Dell: o
# canal do mic saiu em silêncio digital (-91 dB) e o Whisper alucinou
# "Thank you. Thank you." em cima do nada. O Castanha declarou sucesso.
# As funções abaixo existem para que isso nunca mais passe calado.
# ---------------------------------------------------------------------------

# Um canal só conta como mudo quando as duas coisas valem: energia média
# praticamente nula E nenhum pico perto de nível de fala. O par é necessário
# porque o Opus vaza um pouco do canal alto no canal mudo (medido: canal em
# silêncio absoluto sobe para pico -38 dB quando o outro está em 0 dBFS), e
# porque só a média derrubaria uma reunião em que alguém falou três segundos.
#
# Referências medidas em gravações reais:
#   mic mudo no teclado ....... média -91,0 / pico -91,0
#   som do sistema com bipes .. média -48,5 / pico -10,6
#   fala normal ............... média -34,4 / pico -14,1
SILENCE_MEAN_DB = -60.0
SILENCE_MAX_DB = -35.0


def is_silent(mean_db: float, max_db: float) -> bool:
    return mean_db <= SILENCE_MEAN_DB and max_db <= SILENCE_MAX_DB


@dataclass
class ChannelLevels:
    channel: int
    label: str
    mean_db: float
    max_db: float
    silent: bool


def is_default_source_muted() -> Optional[bool]:
    """True/False se der para saber pelo PipeWire; None se não der."""
    try:
        res = subprocess.run(
            ["pactl", "get-source-mute", "@DEFAULT_SOURCE@"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0:
            return None
        out = res.stdout.strip().lower()
        if "yes" in out:
            return True
        if "no" in out:
            return False
    except Exception:
        pass
    return None


def probe_duration_seconds(audio_path: Path) -> Optional[float]:
    """Duração real do arquivo, medida no container (não no cronômetro do daemon)."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode == 0 and res.stdout.strip():
            return round(float(res.stdout.strip()), 2)
    except Exception:
        pass
    return None


def probe_channel_count(audio_path: Path) -> int:
    """Zero quer dizer "não deu para saber", e não "um canal".

    Chutar 1 aqui fazia a medição de um arquivo estéreo olhar só o canal do
    microfone: com o mic mudo, a gravação inteira era classificada como
    silenciosa e nem ia para transcrição.
    """
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode == 0 and res.stdout.strip():
            return int(res.stdout.strip())
    except Exception:
        pass
    return 0


def _channel_label(index: int, mode: str, total: int) -> str:
    if mode == "dual" and total >= 2:
        return "microfone" if index == 0 else "sistema"
    return "microfone"


def measure_channel_levels(audio_path: Path, mode: str = "dual") -> List[ChannelLevels]:
    """Nível de cada canal via ffmpeg volumedetect.

    Ou mede TODOS os canais, ou devolve lista vazia. Medição pela metade é pior
    do que medição nenhuma: se o canal do microfone é o que falhou, o que sobra
    parece uma gravação sadia; se foi o do sistema, a gravação parece muda e o
    Castanha pula a transcrição de uma reunião que tinha áudio.
    """
    total = probe_channel_count(audio_path)
    if total <= 0:
        return []

    levels: List[ChannelLevels] = []
    for idx in range(total):
        try:
            res = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(audio_path),
                 "-af", f"pan=mono|c0=c{idx},volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            return []
        if res.returncode != 0:
            return []

        mean_db, max_db = None, None
        for line in res.stderr.splitlines():
            if "mean_volume:" in line:
                mean_db = _parse_db(line)
            elif "max_volume:" in line:
                max_db = _parse_db(line)
        if max_db is None:
            return []

        mean_value = mean_db if mean_db is not None else max_db
        levels.append(ChannelLevels(
            channel=idx,
            label=_channel_label(idx, mode, total),
            mean_db=mean_value,
            max_db=max_db,
            silent=is_silent(mean_value, max_db),
        ))
    return levels


def _parse_db(line: str) -> Optional[float]:
    try:
        return float(line.split(":")[-1].replace("dB", "").strip())
    except Exception:
        return None


def classify_audio(levels: List[ChannelLevels]) -> str:
    """'ok' | 'mic_mudo' | 'sem_audio' | 'desconhecido'."""
    if not levels:
        return "desconhecido"
    if all(ch.silent for ch in levels):
        return "sem_audio"
    # TODOS os canais de microfone, não o primeiro: no modo mic_only uma
    # interface estéreo entrega a voz só num dos lados, e olhar o canal 0
    # sozinho reprovaria uma gravação boa.
    mics = [ch for ch in levels if ch.label == "microfone"]
    if mics and all(ch.silent for ch in mics):
        return "mic_mudo"
    return "ok"


AUDIO_STATUS_MESSAGES = {
    "ok": audio_status_message("ok", locale="pt"),
    "mic_mudo": audio_status_message("mic_mudo", locale="pt"),
    "sem_audio": audio_status_message("sem_audio", locale="pt"),
    "desconhecido": audio_status_message("desconhecido", locale="pt"),
}
