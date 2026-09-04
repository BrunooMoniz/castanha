"""Mecanismo de captura de áudio com PipeWire / FFmpeg."""

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    for cmd in ["ffmpeg", "pactl"]:
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
        check_dependencies()
        self.devices = get_audio_devices()
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
