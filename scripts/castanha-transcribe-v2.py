#!/root/.castanha-env/bin/python
"""Worker offline do Castanha. SSH/nohup do cliente já mantém o job durável.

Um flock global limita memória/CPU sem adicionar servidor ou banco de filas.
Só stdout contém o resultado; erro retorna código não zero, nunca texto fictício.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

CONTRACT = {
    "contract": "faster-whisper-json-v2", "model": "large-v3",
    "compute_type": "int8", "cpu_threads": 8, "multilingual": True,
    "condition_on_previous_text": False, "chunk_length": 30,
    "segmentation_strategy": "whisper-vad-v1",
}
RUNNER = {"runner": "queue-timeout-v1"}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_status(path, value):
    """Estado operacional privado, sem áudio ou texto da reunião."""
    fd, name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def asr_lock(lock_path=None, inherited_fd=None):
    path = Path(lock_path or Path.home() / ".local/state/castanha/asr.lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if inherited_fd is None:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    else:
        expected, actual = path.lstat(), os.fstat(inherited_fd)
        if (not stat.S_ISREG(expected.st_mode) or not stat.S_ISREG(actual.st_mode)
                or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
                or actual.st_uid != os.getuid() or actual.st_mode & 0o077):
            raise ValueError("Lock herdado inválido")
        fd = os.dup(inherited_fd)
    with os.fdopen(fd, "r+") as lock:
        # O filho herda a mesma descrição de arquivo, portanto não disputa o
        # lock que o supervisor mantém até terminar ou matar o filho.
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield lock


def run_job(request_path, timeout_seconds, *, worker_command=None, lock_path=None):
    """Fila sem prazo; somente a execução recebe timeout e resultado atômico."""
    if not 0 < timeout_seconds <= 10800:
        raise ValueError("Prazo de execução inválido")
    request_path = Path(request_path).absolute()
    request, _ = read_request(request_path)
    directory = request_path.parent
    fd = os.open(directory / "job.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as job_lock:
        try:
            fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if (directory / "result.json").exists():
            return 0
        status_path = directory / "status.json"
        state = {"request_sha256": request["request_sha256"], "phase": "queued",
                 "queued_at": time.time(), "execution_timeout_seconds": timeout_seconds}
        write_status(status_path, state)
        try:
            with asr_lock(lock_path) as lock:
                state.update(phase="running", started_at=time.time())
                write_status(status_path, state)
                command = worker_command or [sys.executable, str(Path(__file__).resolve()),
                    "--request", str(request_path), "--lock-fd", str(lock.fileno())]
                fd, part = tempfile.mkstemp(prefix=".result-", dir=directory)
                try:
                    with os.fdopen(fd, "w+b") as output:
                        process = subprocess.Popen(command, stdout=output,
                            pass_fds=(lock.fileno(),), start_new_session=True)
                        try:
                            returncode = process.wait(timeout=timeout_seconds)
                        except subprocess.TimeoutExpired:
                            # Também encerra o ffmpeg de validação, caso o limite
                            # termine nessa etapa. Nunca libera a fila com filho vivo.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass  # terminou entre o prazo expirar e o sinal
                            process.wait()
                            raise
                        if returncode:
                            raise subprocess.CalledProcessError(returncode, command)
                        output.flush()
                        os.fsync(output.fileno())
                        output.seek(0)
                        result = strict_json(output.read())
                        if (not isinstance(result, dict)
                                or result.get("request_sha256") != request["request_sha256"]
                                or result.get("contract") != CONTRACT
                                or not isinstance(result.get("text"), str)
                                or not isinstance(result.get("segments"), list)):
                            raise ValueError("Resultado sem vínculo com o pedido")
                    os.replace(part, directory / "result.json")
                    sync_directory(directory)
                    state.update(phase="completed", finished_at=time.time())
                    write_status(status_path, state)
                finally:
                    if os.path.exists(part):
                        os.unlink(part)
            return 0
        except Exception as exc:
            state.update(phase="failed", finished_at=time.time(),
                         error="execution_timeout" if isinstance(exc, subprocess.TimeoutExpired)
                         else type(exc).__name__)
            write_status(status_path, state)
            raise


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Campo JSON duplicado")
            result[key] = item
        return result

    def invalid(_):
        raise ValueError("Número JSON não finito")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_request(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError("Manifesto deve ser arquivo regular privado de até 64 KiB")
    request = strict_json(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("Manifesto inválido")
    if canonical(request.get("contract")) != canonical(CONTRACT):
        raise ValueError("Contrato ASR incompatível")
    if request.get("namespace") != "flac-mono-v1" or request.get("mode") != "mic_only":
        raise ValueError("Worker exige canal FLAC mono")
    identity = {key: request.get(key) for key in ("namespace", "mode", "pcm_sha256", "contract")}
    expected = hashlib.sha256(canonical(identity).encode()).hexdigest()
    if request.get("request_sha256") != expected:
        raise ValueError("Identidade do pedido divergente")
    audio = request.get("audio")
    if not isinstance(audio, dict) or not isinstance(audio.get("path"), str):
        raise ValueError("Áudio ausente")
    if (audio.get("mime_type") != "audio/flac" or type(audio.get("sample_rate")) is not int
            or audio["sample_rate"] != 16000 or type(audio.get("channels")) is not int
            or audio["channels"] != 1):
        raise ValueError("Formato de canal incompatível")
    audio_path = Path(audio["path"])
    if not stat.S_ISREG(audio_path.lstat().st_mode) or audio_path.resolve().parent != path.resolve().parent:
        raise ValueError("Áudio deve pertencer ao diretório do pedido")
    return request, audio_path


def validate_audio(path, expected_hash):
    with path.open("rb") as stream:
        if stream.read(4) != b"fLaC":
            raise ValueError("Assinatura FLAC inválida")
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=codec_name,channels,sample_rate:format=duration", "-of", "json", str(path),
    ], capture_output=True, check=True, timeout=30, text=True)
    data = strict_json(probe.stdout)
    streams = data.get("streams", [])
    duration = float(data.get("format", {}).get("duration", 0))
    if (len(streams) != 1 or streams[0].get("codec_name") != "flac"
            or streams[0].get("channels") != 1 or streams[0].get("sample_rate") != "16000"
            or not math.isfinite(duration) or not 0 < duration <= 10800):
        raise ValueError("Canal deve ser FLAC mono 16 kHz, até três horas")
    # timeout inclui decodificação; saída vai a arquivo temporário para não ocupar RAM.
    import tempfile
    with tempfile.TemporaryFile() as pcm:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
                        "-map", "0:a:0", "-f", "s16le", "-"], stdout=pcm,
                       stderr=subprocess.PIPE, check=True, timeout=600)
        pcm.seek(0)
        digest = hashlib.file_digest(pcm, "sha256").hexdigest()
    if digest != expected_hash:
        raise ValueError("PCM não corresponde ao pedido")
    return duration


def transcribe(request, audio_path, model_factory=None, lock_path=None, inherited_fd=None,
               progress=None):
    with asr_lock(lock_path, inherited_fd):
        duration = validate_audio(audio_path, request["pcm_sha256"])
        if model_factory is None:
            from faster_whisper import WhisperModel
            model_factory = WhisperModel
        model = model_factory("large-v3", device="cpu", compute_type="int8",
                              cpu_threads=8, local_files_only=True)
        segments, info = model.transcribe(
            str(audio_path), task="transcribe", language=None, beam_size=5,
            multilingual=True, condition_on_previous_text=False, chunk_length=30,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        )
        result = []
        for segment in segments:
            start, end, text = segment.start, segment.end, segment.text
            if (not isinstance(text, str) or not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start <= end <= duration + 0.5):
                raise ValueError("Segmento ASR inválido")
            if text.strip():
                result.append({"start": start, "end": end, "text": text.strip()})
            if progress is not None:
                progress(duration, end, len(result))
        if not result:
            # Canal com som mas sem fala reconhecível (ruído de sala, respiração,
            # teclado). Falhar aqui deixava o cliente relançando o job para sempre
            # e a reunião inteira presa. A resposta é explícita: nada inventado.
            return {"request_sha256": request["request_sha256"], "contract": CONTRACT,
                    "text": "", "segments": [], "no_speech": True}
        return {"request_sha256": request["request_sha256"], "contract": CONTRACT,
                "text": " ".join(segment["text"] for segment in result), "segments": result}


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--describe-contract", action="store_true")
    group.add_argument("--describe-runner", action="store_true")
    group.add_argument("--request")
    parser.add_argument("--run-job", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=10800)
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    if args.describe_contract:
        print(canonical(CONTRACT))
        return 0
    if args.describe_runner:
        print(canonical(RUNNER))
        return 0
    try:
        if args.run_job:
            if args.lock_fd is not None:
                raise ValueError("Supervisor não aceita lock herdado")
            return run_job(args.request, args.timeout_seconds)
        request, audio_path = read_request(args.request)
        progress = None
        if args.lock_fd is not None:
            status_path = Path(args.request).absolute().parent / "status.json"
            state = strict_json(status_path.read_text())
            last_update = [0.0]
            def progress(duration, end, segments):
                if time.monotonic() - last_update[0] >= 5:
                    state.update(updated_at=time.time(), audio_seconds=duration,
                                 processed_audio_seconds=end, segments=segments)
                    write_status(status_path, state)
                    last_update[0] = time.monotonic()
        result = transcribe(request, audio_path, inherited_fd=args.lock_fd, progress=progress)
        print(canonical(result))
        return 0
    except Exception as exc:
        # Não registrar caminhos, áudio ou transcrição pessoal em erro operacional.
        print("Transcrição pendente: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
