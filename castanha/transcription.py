"""Provedores de transcrição para o Castanha (Groq, Deepgram, Local Whisper, VPS Webhook)."""

import json
import hashlib
import shlex
import os
import subprocess
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from castanha.config import load_config

@dataclass
class Utterance:
    speaker: str
    text: str
    start: float
    end: float

@dataclass
class TranscriptionResult:
    text: str
    utterances: List[Utterance]
    provider: str
    raw_response: Dict[str, Any]

def build_multipart_form(fields: Dict[str, str], files: Dict[str, tuple]) -> tuple[bytes, str]:
    boundary = f"----CastanhaBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, val in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{val}\r\n".encode("utf-8"))
    for name, (filename, data, content_type) in files.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"

class BaseTranscriber:
    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        raise NotImplementedError

class GroqTranscriber(BaseTranscriber):
    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        self.api_key = api_key
        self.model = model

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        if not self.api_key:
            raise ValueError("GROQ_API_KEY não configurada no Castanha.")

        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        fields = {
            "model": self.model,
            "response_format": "verbose_json",
        }
        # Se um idioma específico for informado e não for 'auto', envia; caso contrário omite para detecção automática (EN/PT/misto)
        cfg_lang = load_config().get("transcription", {}).get("language", "auto")
        if cfg_lang and cfg_lang != "auto":
            fields["language"] = cfg_lang

        files = {
            "file": (audio_path.name, audio_bytes, "audio/ogg")
        }

        body, content_type = build_multipart_form(fields, files)
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "User-Agent": "Castanha-Omarchy/0.1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[Castanha] Falha na Groq ({e}). Fazendo fallback automático para o modelo pesado na VPS...")
            vps_transcriber = VpsSshTranscriber(load_config().get("transcription", {}).get("vps_ssh_host", "zinom-vps-2"))
            return vps_transcriber.transcribe(audio_path, mode=mode)

        full_text = data.get("text", "")
        utterances: List[Utterance] = []
        for seg in data.get("segments", []):
            utterances.append(Utterance(
                speaker="Falante",
                text=seg.get("text", "").strip(),
                start=seg.get("start", 0.0),
                end=seg.get("end", 0.0),
            ))

        # Registra consumo no Budget
        try:
            from castanha.budget import BudgetManager
            budget_mgr = BudgetManager()
            duration_sec = data.get("duration", 0.0)
            if not duration_sec and utterances:
                duration_sec = utterances[-1].end
            if duration_sec > 0:
                budget_mgr.record_usage(duration_sec)
        except Exception:
            pass

        return TranscriptionResult(
            text=full_text,
            utterances=utterances,
            provider="groq",
            raw_response=data,
        )

class DeepgramTranscriber(BaseTranscriber):
    def __init__(self, api_key: str, model: str = "nova-2"):
        self.api_key = api_key
        self.model = model

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY não configurada no Castanha.")

        # Se for dual, usa multichannel=true (canal 0 = você, canal 1 = outros)
        is_multichannel = "true" if mode == "dual" else "false"
        url = (
            f"https://api.deepgram.com/v1/listen?model={self.model}"
            f"&language=pt-BR&punctuate=true&diarize=true&multichannel={is_multichannel}"
        )

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        req = urllib.request.Request(
            url,
            data=audio_bytes,
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "audio/ogg",
                "User-Agent": "Castanha-Omarchy/0.1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Extrai canais e falas
        results = data.get("results", {})
        channels = results.get("channels", [])
        utterances: List[Utterance] = []
        full_text_parts = []

        for ch_idx, ch in enumerate(channels):
            speaker_label = "Você" if (mode == "dual" and ch_idx == 0) else "Participante"
            alt = ch.get("alternatives", [{}])[0]
            transcript = alt.get("transcript", "")
            if transcript:
                full_text_parts.append(f"{speaker_label}: {transcript}")
            for word_or_seg in alt.get("paragraphs", {}).get("paragraphs", []):
                for sent in word_or_seg.get("sentences", []):
                    utterances.append(Utterance(
                        speaker=speaker_label,
                        text=sent.get("text", ""),
                        start=sent.get("start", 0.0),
                        end=sent.get("end", 0.0),
                    ))

        full_text = "\n\n".join(full_text_parts) or results.get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")

        return TranscriptionResult(
            text=full_text,
            utterances=utterances,
            provider="deepgram",
            raw_response=data,
        )

class TranscriptionPending(RuntimeError):
    """Job aceito ou transporte indisponível; retomar usando o mesmo original."""


class VpsSshTranscriber(BaseTranscriber):
    def __init__(self, host: str = "zinom-vps-2"):
        self.host = host

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        if not self.host:
            raise TranscriptionPending("VPS de transcrição não configurada")
        digest = hashlib.sha256()
        with audio_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update((mode + ":whisper-large-v3").encode())
        # Caminho estável: repetir SCP/SSH consulta ou retoma o MESMO job.
        remote = f".local/state/castanha/jobs/{digest.hexdigest()}"
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1"]

        def ssh(command):
            try:
                result = subprocess.run(["ssh", *opts, self.host, command],
                                        capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionPending("SSH indisponível; job preservado para retomar") from exc
            if result.returncode != 0:
                raise TranscriptionPending("SSH falhou; job preservado para retomar")
            return result.stdout.strip()

        output = ssh(f"mkdir -p {remote} && if test -f {remote}/result.json; then "
                     f"cat {remote}/result.json; elif test -f {remote}/audio.ogg; then "
                     "echo READY; else echo UPLOAD; fi")
        if output == "UPLOAD":
            upload = f"{remote}/upload-{uuid.uuid4().hex}.ogg"
            try:
                result = subprocess.run(["scp", *opts, str(audio_path), f"{self.host}:{upload}"],
                                        capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionPending("SCP indisponível; original preservado para retomar") from exc
            if result.returncode != 0:
                raise TranscriptionPending("SCP falhou; original preservado para retomar")
            ssh(f"mv {upload} {remote}/audio.ogg")
            output = "READY"
        if output == "READY":
            script = (f"test -f {remote}/result.json && exit 0; "
                      f"/root/castanha-transcribe.py {remote}/audio.ogg > {remote}/result.part "
                      f"&& mv {remote}/result.part {remote}/result.json")
            # flock evita duas transcrições após queda do cliente. nohup libera
            # a conexão enquanto o Whisper trabalha, sem esperar 600 segundos.
            ssh(f"nohup flock -n {remote}/job.lock sh -c {shlex.quote(script)} "
                f">{remote}/worker.log 2>&1 </dev/null &")
            raise TranscriptionPending("Transcrição remota em andamento; execute castanha sync --all para retomar")
        data = json.loads(output)
        if not isinstance(data.get("text"), str) or not data["text"].strip():
            raise TranscriptionPending("Resultado remoto vazio; original preservado")
        return TranscriptionResult(
            text=data["text"],
            utterances=[Utterance(speaker="Falante", text=s.get("text", ""),
                                  start=s.get("start", 0.0), end=s.get("end", 0.0))
                        for s in data.get("segments", [])],
            provider="vps_whisper_large_v3", raw_response=data,
        )

class VpsTranscriber(BaseTranscriber):
    def __init__(self, endpoint: str, auth_token: str = ""):
        self.endpoint = endpoint
        self.auth_token = auth_token

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        if not self.endpoint:
            raise ValueError("Endpoint de VPS não configurado no Castanha.")

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        fields = {"mode": mode}
        files = {"audio": (audio_path.name, audio_bytes, "audio/ogg")}
        body, content_type = build_multipart_form(fields, files)

        headers = {
            "Content-Type": content_type,
            "User-Agent": "Castanha-Omarchy/0.1.0",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        return TranscriptionResult(
            text=data.get("transcript", ""),
            utterances=[],
            provider="vps",
            raw_response=data,
        )

class MockTranscriber(BaseTranscriber):
    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        dummy_text = (
            "[Transcrição Simulada do Castanha]\n"
            "Participante 1: Bom dia pessoal, iniciando nosso alinhamento.\n"
            "Você: Bom dia! Vamos repassar as entregas da semana.\n"
            "Participante 1: O deploy do novo plugin no Linux foi concluído com sucesso.\n"
            "Você: Perfeito, vou registrar os fatos no Zinom e documentar o fluxo.\n"
        )
        return TranscriptionResult(
            text=dummy_text,
            utterances=[
                Utterance(speaker="Participante 1", text="Bom dia pessoal, iniciando nosso alinhamento.", start=0.0, end=2.0),
                Utterance(speaker="Você", text="Bom dia! Vamos repassar as entregas da semana.", start=2.1, end=4.0),
            ],
            provider="mock",
            raw_response={"status": "mocked"},
        )

def get_transcriber(estimated_duration_sec: float = 60.0) -> BaseTranscriber:
    cfg = load_config()
    t_cfg = cfg.get("transcription", {})
    if t_cfg.get("provider") == "mock":
        return MockTranscriber()
    groq_key = t_cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")

    # 1. Verifica se Groq está configurada e se o budget de R$ 5,00 não estourou
    from castanha.budget import BudgetManager
    budget_mgr = BudgetManager()
    if groq_key and budget_mgr.can_use_groq(estimated_duration_sec):
        return GroqTranscriber(groq_key, t_cfg.get("groq_model", "whisper-large-v3-turbo"))

    vps_host = t_cfg.get("vps_ssh_host", "zinom-vps-2")
    if vps_host:
        return VpsSshTranscriber(vps_host)
    raise TranscriptionPending("Nenhum transcritor configurado; original preservado")
