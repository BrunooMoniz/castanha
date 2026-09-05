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

# A Groq recusa arquivo acima de 25 MB no plano gratuito (100 MB no pago). Em
# 05/09/2026 uma reunião de 2h13 deu 65 MB, a Groq derrubou a conexão ("Broken
# pipe") e a gravação ficou sem transcrição. Acima deste teto o áudio vai em
# fatias; abaixo dele vai inteiro, porque cada emenda custa contexto.
GROQ_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_CHUNK_DURATION_SEC = 600        # 10 minutos por fatia (~5 MB a 64 kbps)
GROQ_ATTEMPTS_PER_CHUNK = 3
# A VPS transcreve em CPU: 2,5x a duração, entre 30 min e 3 h. Passado isso a
# resposta certa é falhar, e o "tentar de novo" do painel resolve depois.
VPS_MIN_TIMEOUT_SEC = 1800
VPS_MAX_TIMEOUT_SEC = 3 * 3600


def _retry_after_seconds(err: Any) -> Optional[float]:
    """O Retry-After do 429 da Groq, em segundos, quando vier."""
    try:
        valor = err.headers.get("Retry-After") if getattr(err, "headers", None) else None
        return float(valor) if valor else None
    except (TypeError, ValueError):
        return None

def chunk_audio(audio_path: Path, chunk_sec: int = DEFAULT_CHUNK_DURATION_SEC) -> List[tuple[Path, float]]:
    """Divide um arquivo de áudio longo em segmentos temporários (caminho_chunk, offset_segundos)."""
    import tempfile
    from castanha.audio import probe_duration_seconds

    size = audio_path.stat().st_size if audio_path.exists() else 0
    duration = probe_duration_seconds(audio_path)

    # Se o arquivo for menor que o limite de bytes e menor que o chunk, não divide
    if size <= GROQ_MAX_FILE_BYTES and (duration is None or duration <= chunk_sec):
        return [(audio_path, 0.0)]

    temp_dir = Path(tempfile.mkdtemp(prefix="castanha_chunks_"))
    chunks: List[tuple[Path, float]] = []

    if duration is not None and duration > 0:
        num_chunks = int(duration // chunk_sec) + (1 if (duration % chunk_sec) > 0 else 0)
        for i in range(num_chunks):
            start = i * chunk_sec
            dur = min(chunk_sec, duration - start)
            out_file = temp_dir / f"chunk_{i:03d}.ogg"
            res = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(audio_path), "-c", "copy", str(out_file)],
                capture_output=True
            )
            # Se cópia direta falhar, recodifica em opus 64k
            if res.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(audio_path), "-c:a", "libopus", "-b:a", "64k", str(out_file)],
                    capture_output=True, check=True
                )
            chunks.append((out_file, float(start)))
    else:
        # Duração desconhecida: usa segment do ffmpeg
        pattern = str(temp_dir / "chunk_%03d.ogg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path), "-f", "segment", "-segment_time", str(chunk_sec), "-c", "copy", pattern],
            capture_output=True, check=True
        )
        chunk_files = sorted(temp_dir.glob("chunk_*.ogg"))
        cur_offset = 0.0
        for cf in chunk_files:
            cf_dur = probe_duration_seconds(cf) or float(chunk_sec)
            chunks.append((cf, cur_offset))
            cur_offset += cf_dur

    return chunks

class GroqTranscriber(BaseTranscriber):
    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        self.api_key = api_key
        self.model = model

    def _request_groq(self, file_path: Path) -> Dict[str, Any]:
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        fields = {
            "model": self.model,
            "response_format": "verbose_json",
        }
        cfg_lang = load_config().get("transcription", {}).get("language", "auto")
        if cfg_lang and cfg_lang != "auto":
            fields["language"] = cfg_lang

        files = {
            "file": (file_path.name, audio_bytes, "audio/ogg")
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

        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _request_groq_with_retry(self, file_path: Path, attempts: int = GROQ_ATTEMPTS_PER_CHUNK) -> Dict[str, Any]:
        """Uma fatia, com nova tentativa em erro transitório (429, 5xx, rede).

        Sem isto, um único 429 no meio de catorze fatias mandava a reunião
        inteira para a VPS, que leva horas.
        """
        import time as _time
        import urllib.error

        ultimo: Optional[Exception] = None
        for tentativa in range(1, attempts + 1):
            espera = float(2 ** tentativa)
            try:
                return self._request_groq(file_path)
            except urllib.error.HTTPError as e:
                corpo = ""
                try:
                    corpo = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                if e.code in (408, 429) or e.code >= 500:
                    ultimo = RuntimeError(f"Groq HTTP {e.code}: {corpo}")
                    espera = _retry_after_seconds(e) or espera
                else:
                    # 400, 401, 413: repetir não muda nada.
                    raise RuntimeError(f"Groq recusou o áudio (HTTP {e.code}): {corpo}") from e
            except OSError as e:
                # URLError, timeout de socket, conexão derrubada.
                ultimo = e
            if tentativa < attempts:
                _time.sleep(min(espera, 60.0))
        raise RuntimeError(f"Groq falhou em {attempts} tentativas: {ultimo}")

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        if not self.api_key:
            raise ValueError("GROQ_API_KEY não configurada no Castanha.")

        # Só fatia quem precisa: reunião curta vai inteira.
        size = audio_path.stat().st_size if audio_path.exists() else 0
        if size > GROQ_MAX_FILE_BYTES:
            chunks = chunk_audio(audio_path, chunk_sec=DEFAULT_CHUNK_DURATION_SEC)
        else:
            chunks = [(audio_path, 0.0)]

        temp_dirs_to_clean = {c_path.parent for c_path, _ in chunks if c_path != audio_path}

        all_utterances: List[Utterance] = []
        full_text_parts: List[str] = []
        raw_responses: List[Dict[str, Any]] = []
        total_duration = 0.0

        # Se uma fatia falhar de vez, a exceção sobe: quem decide o fallback
        # para a VPS é o engine, e decide UMA vez. Em 05/09 o fallback morava
        # aqui e lá, e a mesma reunião subiu duas vezes para a VPS.
        try:
            for chunk_file, offset_sec in chunks:
                data = self._request_groq_with_retry(chunk_file)
                raw_responses.append(data)

                txt = (data.get("text") or "").strip()
                if txt:
                    full_text_parts.append(txt)

                for seg in data.get("segments", []) or []:
                    all_utterances.append(Utterance(
                        speaker="Falante",
                        text=(seg.get("text") or "").strip(),
                        start=round(offset_sec + float(seg.get("start", 0.0) or 0.0), 2),
                        end=round(offset_sec + float(seg.get("end", 0.0) or 0.0), 2),
                    ))

                dur = data.get("duration") or 0.0
                seg_list = data.get("segments", []) or []
                if not dur and seg_list:
                    dur = seg_list[-1].get("end", 0.0) or 0.0
                total_duration += float(dur)
        finally:
            import shutil
            for t_dir in temp_dirs_to_clean:
                shutil.rmtree(t_dir, ignore_errors=True)

        # Registra consumo no Budget
        try:
            from castanha.budget import BudgetManager
            budget_mgr = BudgetManager()
            if not total_duration and all_utterances:
                total_duration = all_utterances[-1].end
            if total_duration > 0:
                budget_mgr.record_usage(total_duration)
        except Exception:
            pass

        full_text = " ".join(full_text_parts)
        return TranscriptionResult(
            text=full_text,
            utterances=all_utterances,
            provider="groq",
            raw_response=raw_responses[0] if len(raw_responses) == 1 else {"chunks": raw_responses},
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

class VpsSshTranscriber(BaseTranscriber):
    def __init__(self, host: str = "zinom-vps-2"):
        self.host = host

    def _cleanup_remote(self, remote_tmp: str, out_json: str) -> None:
        """Mata o whisper órfão e apaga o áudio na VPS. Melhor esforço."""
        # O [c] impede o pkill de casar com o próprio shell que o executa.
        padrao = f"[c]astanha-transcribe.py {remote_tmp}"
        cmd = ["ssh", "-o", "ConnectTimeout=10", self.host,
               f"pkill -f '{padrao}'; rm -f {remote_tmp} {out_json}"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass

    def transcribe(self, audio_path: Path, mode: str = "dual") -> TranscriptionResult:
        import time, uuid
        remote_tmp = f"/tmp/castanha_{uuid.uuid4().hex[:8]}.ogg"

        # 1. Copia áudio para a VPS via SCP
        scp_cmd = ["scp", "-o", "ConnectTimeout=10", str(audio_path), f"{self.host}:{remote_tmp}"]
        res_scp = subprocess.run(scp_cmd, capture_output=True, text=True)
        if res_scp.returncode != 0:
            raise RuntimeError(f"Falha ao enviar áudio para a VPS: {res_scp.stderr}")

        # 2. Executa a transcrição com Whisper large-v3 na VPS. O timeout
        # acompanha a duração do áudio: os 600 s fixos de antes não davam nem
        # para uma reunião de uma hora.
        from castanha.audio import probe_duration_seconds
        dur = probe_duration_seconds(audio_path) or 600.0
        vps_timeout = int(min(max(VPS_MIN_TIMEOUT_SEC, dur * 2.5), VPS_MAX_TIMEOUT_SEC))

        out_json = f"{remote_tmp}.json"
        remote_cmd = f"/root/castanha-transcribe.py {remote_tmp} > {out_json} && cat {out_json} && rm -f {remote_tmp} {out_json}"
        ssh_cmd = [
            "ssh", "-o", "ConnectTimeout=15", self.host,
            f"bash -c '{remote_cmd}'"
        ]
        try:
            res_ssh = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=vps_timeout)
        except subprocess.TimeoutExpired:
            # O ssh morreu aqui, mas o whisper continua lá: em 05/09 ele ficou
            # 90 min a 300% de CPU depois do timeout, com o áudio no /tmp.
            self._cleanup_remote(remote_tmp, out_json)
            raise RuntimeError(
                f"Transcrição na VPS passou de {vps_timeout // 60} min e foi abortada."
            )
        if res_ssh.returncode != 0:
            raise RuntimeError(f"Falha na transcrição remota na VPS: {res_ssh.stderr.strip() or res_ssh.stdout.strip()}")

        data = json.loads(res_ssh.stdout.strip())
        utterances: List[Utterance] = []
        for s in data.get("segments", []):
            utterances.append(Utterance(
                speaker="Falante",
                text=s.get("text", ""),
                start=s.get("start", 0.0),
                end=s.get("end", 0.0),
            ))

        return TranscriptionResult(
            text=data.get("text", ""),
            utterances=utterances,
            provider="vps_whisper_large_v3",
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

def get_transcriber(estimated_duration_sec: float = 60.0) -> BaseTranscriber:
    cfg = load_config()
    t_cfg = cfg.get("transcription", {})
    groq_key = t_cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")

    # 1. Verifica se Groq está configurada e se o budget de R$ 5,00 não estourou
    from castanha.budget import BudgetManager
    budget_mgr = BudgetManager()
    if groq_key and budget_mgr.can_use_groq(estimated_duration_sec):
        return GroqTranscriber(groq_key, t_cfg.get("groq_model", "whisper-large-v3-turbo"))

    # 2. Fallback / Primário: Modelo Pesado Local na VPS (Whisper large-v3 via SSH)
    vps_host = t_cfg.get("vps_ssh_host", "zinom-vps-2")
    try:
        test = subprocess.run(["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", vps_host, "true"], capture_output=True)
        if test.returncode == 0:
            return VpsSshTranscriber(vps_host)
    except Exception:
        pass

    # 3. Fallback mock elegante
    return MockTranscriber()
