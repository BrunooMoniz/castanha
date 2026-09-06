#!/root/.castanha-env/bin/python
"""Worker offline do Castanha. SSH/nohup do cliente já mantém o job durável.

Um flock global limita memória/CPU sem adicionar servidor ou banco de filas.
Só stdout contém o resultado; erro retorna código não zero, nunca texto fictício.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys

CONTRACT = {
    "contract": "faster-whisper-json-v2", "model": "large-v3",
    "compute_type": "int8", "cpu_threads": 8, "multilingual": True,
    "condition_on_previous_text": False, "chunk_length": 30,
    "segmentation_strategy": "whisper-vad-v1",
}


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


def transcribe(request, audio_path, model_factory=None, lock_path=None):
    duration = validate_audio(audio_path, request["pcm_sha256"])
    lock_path = Path(lock_path or Path.home() / ".local/state/castanha/asr.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
    group.add_argument("--request")
    args = parser.parse_args()
    if args.describe_contract:
        print(canonical(CONTRACT))
        return 0
    try:
        request, audio_path = read_request(args.request)
        result = transcribe(request, audio_path)
        print(canonical(result))
        return 0
    except Exception as exc:
        # Não registrar caminhos, áudio ou transcrição pessoal em erro operacional.
        print("Transcrição pendente: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
