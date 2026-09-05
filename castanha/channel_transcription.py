"""Transcrição por origem, sem inferir identidade pessoal a partir do canal.

O chamador deve manter meeting_lock durante toda a operação. Os checkpoints
ficam no Bronze e permitem retomar o segundo canal sem cobrar o primeiro de novo.
Ligado ao engine atrás da flag `transcription.por_canal`, desligada por padrão:
ativar de verdade depende do contrato de ingestão F4 e da QA no XPS.
"""

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from castanha.audio import measure_channel_levels, probe_channel_count, probe_duration_seconds
from castanha.durability import file_sha256, write_json
from castanha.transcription import TranscriptionResult, Utterance

LABELS = ("Microfone local", "Áudio do sistema")
ORIGINS = ("microfone_local", "audio_sistema")
# Gravação mono não tem duas origens para separar. Ela ganha uma origem técnica
# genérica em vez de virar "microfone", que seria afirmar quem falou sem prova.
MONO_LABEL = "Áudio da gravação"
MONO_ORIGIN = "gravacao_mono"
SILENT_PROVIDER = "nenhum (canal em silêncio)"
# v3 identifica o canal pelo PCM decodificado, não pelos bytes do FLAC.
CHECKPOINT_VERSION = 3
# Teto por canal, não por gravação: dois canais são dois envios cobrados.
MAX_CHANNEL_SECONDS = 3 * 3600


@dataclass
class ChannelUtterance(Utterance):
    """Cada fala carrega o canal, a origem técnica e os hashes que a provam.

    Origem é o canal de captura, não a pessoa: o mesmo campo NÃO deve receber
    nome de convidado do calendário em nenhuma etapa posterior.
    """

    channel: int
    origin: str
    source_sha256: str
    channel_sha256: str


def pcm_sha256(path: Path) -> str:
    """Identidade pelo áudio decodificado, não pelos bytes do contêiner.

    O FLAC derivado é recriado a cada execução. Identificar o canal pelos bytes
    do arquivo fazia uma atualização do FFmpeg recusar um checkpoint válido e
    obrigar a pagar o canal de novo. O PCM é o mesmo conteúdo em qualquer versão.
    """
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
            digest.update(chunk)
    finally:
        proc.stdout.close()
        erro = proc.stderr.read()
        proc.stderr.close()
        codigo = proc.wait(timeout=600)
    if codigo != 0:
        raise ValueError(f"Falha ao decodificar o canal para identidade: {erro[:200]!r}")
    return digest.hexdigest()


def prepare_channels(source: Path, directory: Path) -> list[dict]:
    """Um FLAC mono por canal, sem nova compressão com perda e sem tocar no original.

    Estéreo vira duas origens (microfone e sistema). Mono vira UMA origem
    genérica: alegar duas origens onde só existe uma seria inventar separação.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=channels", "-of", "json", str(source)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if len(streams) != 1 or streams[0].get("channels") not in (1, 2):
        raise ValueError("Gravação precisa ter exatamente um ou dois canais de áudio")
    total = streams[0]["channels"]
    duration = probe_duration_seconds(source)
    directory.mkdir(parents=True, exist_ok=True)
    canais = []
    for channel in range(total):
        target = directory / f"channel-{channel}.flac"
        # Recriar o derivado é seguro: nunca é a fonte e o conteúdo é determinista.
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-map", "0:a:0", "-af", f"pan=mono|c0=c{channel}",
             "-c:a", "flac", str(target)],
            capture_output=True, check=True, timeout=600,
        )
        _check_derived(target, duration)
        canais.append({"channel": channel, "path": target,
                       "label": LABELS[channel] if total == 2 else MONO_LABEL,
                       "origin": ORIGINS[channel] if total == 2 else MONO_ORIGIN,
                       "duration_seconds": probe_duration_seconds(target)})
    return canais


def _check_derived(target: Path, duration) -> None:
    """Derivado truncado é canal perdido; recusar antes de chamar o provedor."""
    if not target.exists() or target.stat().st_size == 0:
        raise ValueError("Canal derivado vazio; áudio original preservado")
    if probe_channel_count(target) != 1:
        raise ValueError("Canal derivado não ficou mono; recuse o processamento")
    derived = probe_duration_seconds(target)
    if duration and derived is not None and abs(derived - duration) > max(0.5, duration * 0.01):
        raise ValueError("Canal derivado com duração diferente do original")


def _silent(path: Path) -> bool:
    """Mesma medição que o engine usa para 'mic_mudo': critério único.

    Medição indisponível não vira silêncio presumido. Nesse caso o canal segue
    para o provedor, e resposta vazia continua sendo erro em vez de fala perdida.
    """
    levels = measure_channel_levels(path, mode="mic_only")
    return len(levels) == 1 and levels[0].silent


def _guard_budget(entry, budget) -> float:
    """Teto e orçamento POR CANAL: dois canais são dois envios cobrados.

    Recusar aqui deixa a gravação pendente com o áudio intacto, que é melhor do
    que descobrir o estouro depois de enviar. O canal já concluído fica no
    checkpoint e não é cobrado de novo na retomada.
    """
    duration = entry.get("duration_seconds")
    if duration is None:
        raise ValueError("Duração do canal desconhecida; não dá para aplicar o teto")
    if duration > MAX_CHANNEL_SECONDS:
        raise ValueError(f"Canal {entry['channel']} passa do teto de "
                         f"{MAX_CHANNEL_SECONDS // 3600} h; nada foi enviado")
    if budget is not None and not budget.can_use_groq(duration):
        raise ValueError(f"Orçamento não cobre o canal {entry['channel']}; nada foi enviado")
    return duration


def _require_real_provider(provider) -> None:
    if not isinstance(provider, str) or provider in ("", "mock", "failed"):
        raise ValueError("Provedor não real; não concluir transcrição por origem")


def _segments(result, entry, source_sha256, channel_sha256):
    if not isinstance(result.text, str) or not result.text.strip():
        raise ValueError("Canal com áudio e sem transcrição; checkpoint não concluído")
    if not result.utterances:
        raise ValueError("Transcrição sem timestamps; não é possível ordenar os canais")
    values = []
    for segment in result.utterances:
        if (not isinstance(segment.text, str) or not segment.text.strip()
                or any(isinstance(t, bool) or not isinstance(t, (int, float))
                       or not math.isfinite(t) for t in (segment.start, segment.end))
                or segment.start < 0 or segment.end < segment.start):
            raise ValueError("Segmento inválido; áudio preservado")
        # O falante devolvido pelo provedor é descartado de propósito: o canal é
        # origem técnica, e nome vindo de fora viraria identidade sem evidência.
        values.append(ChannelUtterance(entry["label"], segment.text, segment.start, segment.end,
                                       entry["channel"], entry["origin"],
                                       source_sha256, channel_sha256))
    return values


def transcribe_dual(source: Path, checkpoint_root: Path, transcribe_mono, *,
                    pipeline_id: str, budget=None) -> TranscriptionResult:
    """Callback recebe (FLAC mono, duração em segundos) e transcreve UM canal.

    pipeline_id identifica versão/modelo e impede reutilizar resposta incompatível.
    Canal não é pessoa. Todos os participantes remotos compartilham o canal 1.
    Não cortamos silêncio nem sobreposições, mantendo a mesma linha do tempo.
    """
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise ValueError("pipeline_id obrigatório")
    digest = file_sha256(source)
    directory = checkpoint_root / digest
    entradas = prepare_channels(source, directory)
    if file_sha256(source) != digest:
        raise ValueError("Áudio alterado durante a separação; recuse o processamento")
    utterances = []
    providers = set()
    channels = []
    for entry in entradas:
        channel, path = entry["channel"], entry["path"]
        checkpoint = directory / f"channel-{channel}.json"
        pcm = pcm_sha256(path)
        identity = {"version": CHECKPOINT_VERSION, "source_sha256": digest, "channel": channel,
                    "pipeline_id": pipeline_id, "origin": entry["origin"], "pcm_sha256": pcm}
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("identity") != identity:
                raise ValueError("Checkpoint incompatível; preservado para inspeção")
            provider, silent = saved["provider"], saved.get("silent") is True
            if not silent:
                _require_real_provider(provider)
            segments = [ChannelUtterance(**s) for s in saved["utterances"]]
        elif _silent(path):
            # Canal mudo não vai ao provedor: o Whisper alucina em silêncio e o
            # minuto seria cobrado igual. Silêncio fica registrado, não inventado.
            provider, silent, segments, provider_text = SILENT_PROVIDER, True, [], ""
        else:
            duration = _guard_budget(entry, budget)
            result = transcribe_mono(path, duration)
            _require_real_provider(result.provider)
            provider, silent, provider_text = result.provider, False, result.text
            segments = _segments(result, entry, digest, pcm)
        if not checkpoint.exists():
            # provider_text é auditoria da resposta original; a junção usa os segmentos.
            write_json(checkpoint, {"identity": identity, "provider": provider, "silent": silent,
                                    "provider_text": provider_text,
                                    "utterances": [asdict(s) for s in segments]})
        utterances.extend(segments)
        channels.append({"channel": channel, "origin": entry["origin"], "label": entry["label"],
                         "provider": provider, "silent": silent, "channel_sha256": pcm,
                         "duration_seconds": entry.get("duration_seconds"),
                         "utterance_count": len(segments)})
        if not silent:
            providers.add(provider)
    if all(entry["silent"] for entry in channels):
        raise ValueError("Nenhum canal com áudio; nada a transcrever e nada a inventar")
    utterances.sort(key=lambda s: (s.start, s.end, s.channel))
    text = "\n".join(f"[{s.start:.2f}s–{s.end:.2f}s] {s.speaker}: {s.text}" for s in utterances)
    total = sum(entry["utterance_count"] for entry in channels)
    # Entregar transcrição a menos é pior do que falhar: o resumo seguiria adiante
    # com a reunião pela metade e ninguém saberia o que ficou de fora.
    if total != len(utterances) or any(s.text not in text for s in utterances):
        raise ValueError("Conteúdo incompleto na junção dos canais; recuse a entrega")
    return TranscriptionResult(text, utterances,
                               next(iter(providers)) if len(providers) == 1 else "mixed",
                               {"channel_provenance": True, "identity_inferred": False,
                                "mono_fallback": len(entradas) == 1,
                                "source_sha256": digest, "pipeline_id": pipeline_id,
                                "channels": channels, "utterance_count": total})
