"""Mecanismo de captura de áudio com PipeWire / FFmpeg."""

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

@dataclass
class AudioDeviceInfo:
    source: str
    sink: str
    monitor: str

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

    return AudioDeviceInfo(source=source, sink=sink, monitor=monitor)

class AudioRecorder:
    def __init__(self):
        self.devices = None
        self.process: Optional[subprocess.Popen] = None
        self.output_path: Optional[Path] = None
        self.start_time: Optional[float] = None
        self.mode: str = "dual"

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
                "[0:a]pan=mono|c0=0.5*c0+0.5*c1[mic];[1:a]pan=mono|c0=0.5*c0+0.5*c1[sys];[mic][sys]amerge=inputs=2[aout]",
                "-map", "[aout]",
                "-c:a", "libopus",
                "-b:a", bitrate,
                str(self.output_path),
            ]
        elif mode == "mic_only":
            # Reunião presencial: grava apenas microfone físico
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "pulse", "-i", self.devices.source,
                "-c:a", "libopus",
                "-b:a", "32k",
                str(self.output_path),
            ]
        else:
            raise ValueError(f"Modo de gravação desconhecido: {mode}")

        self.start_time = time.time()
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,  # Isolamento de grupo de processos
        )
        return self.process

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
        self.process = None

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
    "ok": "Áudio capturado nos dois canais.",
    "mic_mudo": "O canal do microfone saiu em silêncio: o mic estava mudo (teclado ou sistema). Só o áudio da chamada foi gravado.",
    "sem_audio": "Nenhum canal captou áudio: a gravação está em silêncio do início ao fim.",
    "desconhecido": "Não foi possível medir os níveis do áudio.",
}
