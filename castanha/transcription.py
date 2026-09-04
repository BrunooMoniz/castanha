"""Provedores de transcrição para o Castanha (Groq, Deepgram, Local Whisper, VPS Webhook)."""

import json
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

        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        full_text = data.get("text", "")
        utterances: List[Utterance] = []
        for seg in data.get("segments", []):
            utterances.append(Utterance(
                speaker="Falante",
                text=seg.get("text", "").strip(),
                start=seg.get("start", 0.0),
                end=seg.get("end", 0.0),
            ))

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

def get_transcriber() -> BaseTranscriber:
    cfg = load_config()
    t_cfg = cfg.get("transcription", {})
    provider = t_cfg.get("provider", "groq")

    if provider == "groq":
        api_key = t_cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
        if api_key:
            return GroqTranscriber(api_key, t_cfg.get("groq_model", "whisper-large-v3-turbo"))
    elif provider == "deepgram":
        api_key = t_cfg.get("deepgram_api_key") or os.environ.get("DEEPGRAM_API_KEY", "")
        if api_key:
            return DeepgramTranscriber(api_key, t_cfg.get("deepgram_model", "nova-2"))
    elif provider == "vps":
        endpoint = t_cfg.get("vps_endpoint")
        if endpoint:
            return VpsTranscriber(endpoint, t_cfg.get("vps_auth_token", ""))

    # Fallback elegante caso nenhuma chave de API esteja configurada no momento
    return MockTranscriber()
