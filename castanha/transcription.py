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

# O tipo declarado tem que casar com o arquivo. O canal separado vai em FLAC, e
# anunciar tudo como audio/ogg entregaria FLAC rotulado de Ogg para a Groq.
AUDIO_MIME = {
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".webm": "audio/webm",
}


def audio_mime(path: Path) -> str:
    """Desconhecido continua como audio/ogg: é o que a captura sempre gerou."""
    return AUDIO_MIME.get(path.suffix.lower(), "audio/ogg")


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
# O Retry-After da Groq pode ser de minutos (janela de áudio por hora). Vale
# esperar até 15 min por vez, mas nunca mais de 30 min somados: além disso a
# VPS ou o "tentar de novo" do painel resolvem melhor do que uma tela presa.
GROQ_MAX_WAIT_SEC = 900.0
GROQ_MAX_TOTAL_WAIT_SEC = 1800.0
# Resto de áudio menor que isto se junta à fatia anterior: fatia quase vazia
# volta 400 da Groq e derrubava a reunião inteira para a VPS.
MIN_CHUNK_TAIL_SEC = 5.0
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
    try:
        return _fatiar(audio_path, chunk_sec, duration, temp_dir)
    except Exception:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _fatiar(audio_path: Path, chunk_sec: int, duration: Optional[float], temp_dir: Path) -> List[tuple[Path, float]]:
    from castanha.audio import probe_duration_seconds

    chunks: List[tuple[Path, float]] = []

    if duration is not None and duration > 0:
        num_chunks = max(1, int(duration // chunk_sec) + (1 if (duration % chunk_sec) > 0 else 0))
        resto = duration - (num_chunks - 1) * chunk_sec
        if num_chunks > 1 and resto < MIN_CHUNK_TAIL_SEC:
            num_chunks -= 1
        for i in range(num_chunks):
            start = i * chunk_sec
            # A última fatia vai até o fim, inclusive o resto pequeno.
            dur = (duration - start) if i == num_chunks - 1 else chunk_sec
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
        # Quanto já se esperou por Retry-After nesta transcrição.
        self._esperado = 0.0

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
            "file": (file_path.name, audio_bytes, audio_mime(file_path))
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
        import http.client
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
                    pedido = _retry_after_seconds(e)
                    if pedido and pedido > GROQ_MAX_WAIT_SEC:
                        # Dormir 15 min para tentar antes da hora não adianta.
                        raise RuntimeError(
                            f"Groq pediu para esperar {int(pedido // 60)} min, mais do que o Castanha aceita ({corpo})"
                        ) from e
                    espera = pedido or espera
                else:
                    # 400, 401, 413: repetir não muda nada.
                    raise RuntimeError(f"Groq recusou o áudio (HTTP {e.code}): {corpo}") from e
            except (OSError, http.client.HTTPException, ValueError) as e:
                # URLError, timeout de socket, conexão derrubada, resposta
                # truncada (IncompleteRead) ou JSON pela metade.
                ultimo = e
            if tentativa < attempts:
                espera = min(espera, GROQ_MAX_WAIT_SEC)
                if self._esperado + espera > GROQ_MAX_TOTAL_WAIT_SEC:
                    raise RuntimeError(
                        f"Groq pediu para esperar mais do que os {int(GROQ_MAX_TOTAL_WAIT_SEC // 60)} min "
                        f"que o Castanha aceita ({ultimo})"
                    )
                self._esperado += espera
                _time.sleep(espera)
        raise RuntimeError(f"Groq falhou em {attempts} tentativas: {ultimo}")

    @staticmethod
    def _registrar_consumo(total_duration: float, utterances: List[Utterance]) -> None:
        try:
            from castanha.budget import BudgetManager
            if not total_duration and utterances:
                total_duration = utterances[-1].end
            if total_duration > 0:
                BudgetManager().record_usage(total_duration)
        except Exception:
            pass

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
        self._esperado = 0.0

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
            # A Groq cobra as fatias que transcreveu mesmo que a seguinte falhe.
            self._registrar_consumo(total_duration, all_utterances)

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
            from castanha.audio import probe_duration_seconds
            duration = probe_duration_seconds(audio_path) or 600
            worker_timeout = int(min(max(VPS_MIN_TIMEOUT_SEC, duration * 2.5), VPS_MAX_TIMEOUT_SEC))
            script = (f"test -f {remote}/result.json && exit 0; "
                      f"timeout --kill-after=30s {worker_timeout}s /root/castanha-transcribe.py {remote}/audio.ogg > {remote}/result.part "
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

    # O mock só existe para teste e demonstração, e só quando pedido. Como
    # último recurso ele inventava uma reunião ("[Transcrição Simulada]")
    # justamente quando o Bruno estava sem internet, marcava a gravação como
    # transcrita e mandava fatos falsos para o Zinom.
    if str(t_cfg.get("provider", "")).lower() == "mock" or os.environ.get("CASTANHA_MOCK_TRANSCRIBER") == "1":
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
