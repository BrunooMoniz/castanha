"""Transcrição por origem, sem inferir identidade pessoal a partir do canal.

O chamador deve manter meeting_lock durante toda a operação. Os checkpoints
ficam no Bronze e permitem retomar o segundo canal sem cobrar o primeiro de novo.
Ainda não conectado ao daemon: integração requer o contrato de ingestão F4.
"""

import json
import math
import subprocess
from dataclasses import asdict
from pathlib import Path

from castanha.durability import file_sha256, write_json
from castanha.transcription import TranscriptionResult, Utterance

LABELS = ("Microfone local", "Áudio do sistema")


def split_channels(source: Path, directory: Path) -> list[Path]:
    """Derivados sem nova compressão com perda; original nunca é alterado."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=channels", "-of", "json", str(source)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if len(streams) != 1 or streams[0].get("channels") != 2:
        raise ValueError("Gravação dual exige exatamente dois canais de áudio")
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for channel in range(2):
        target = directory / f"channel-{channel}.flac"
        # Recriar o derivado é seguro: nunca é a fonte e o conteúdo é determinista.
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-map", "0:a:0", "-af", f"pan=mono|c0=c{channel}",
             "-c:a", "flac", str(target)],
            capture_output=True, check=True, timeout=600,
        )
        outputs.append(target)
    return outputs


def _segments(result, channel):
    if not isinstance(result.text, str) or not result.text.strip():
        raise ValueError("Canal sem transcrição válida; checkpoint não concluído")
    if not result.utterances:
        raise ValueError("Transcrição sem timestamps; não é possível ordenar os canais")
    values = []
    for segment in result.utterances:
        if (not isinstance(segment.text, str) or not segment.text.strip()
                or any(isinstance(t, bool) or not isinstance(t, (int, float))
                       or not math.isfinite(t) for t in (segment.start, segment.end))
                or segment.start < 0 or segment.end < segment.start):
            raise ValueError("Segmento inválido; áudio preservado")
        values.append(Utterance(LABELS[channel], segment.text, segment.start, segment.end))
    return values


def transcribe_dual(source: Path, checkpoint_root: Path, transcribe_mono, *,
                    pipeline_id: str) -> TranscriptionResult:
    """Callback recebe FLAC mono; deve aplicar orçamento e fallback por canal.

    pipeline_id identifica versão/modelo e impede reutilizar resposta incompatível.
    Canal não é pessoa. Todos os participantes remotos compartilham o canal 1.
    Não cortamos silêncio nem sobreposições, mantendo a mesma linha do tempo.
    """
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise ValueError("pipeline_id obrigatório")
    digest = file_sha256(source)
    directory = checkpoint_root / digest
    paths = split_channels(source, directory)
    if file_sha256(source) != digest:
        raise ValueError("Áudio alterado durante a separação; recuse o processamento")
    utterances = []
    providers = set()
    for channel, path in enumerate(paths):
        checkpoint = directory / f"channel-{channel}.json"
        identity = {"version": 1, "source_sha256": digest, "channel": channel,
                    "pipeline_id": pipeline_id, "derived_sha256": file_sha256(path)}
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("identity") != identity:
                raise ValueError("Checkpoint incompatível; preservado para inspeção")
            result = TranscriptionResult(
                saved["text"], [Utterance(**s) for s in saved["utterances"]],
                saved["provider"], {},
            )
        else:
            result = transcribe_mono(path)
        if not isinstance(result.provider, str) or result.provider in ("", "mock", "failed"):
            raise ValueError("Provedor não real; não concluir transcrição por origem")
        segments = _segments(result, channel)
        if not checkpoint.exists():
            write_json(checkpoint, {"identity": identity, "text": result.text,
                                   "utterances": [asdict(s) for s in segments],
                                   "provider": result.provider})
        utterances.extend(segments)
        providers.add(result.provider)
    utterances.sort(key=lambda s: (s.start, s.end, s.speaker))
    text = "\n".join(f"[{s.start:.2f}s–{s.end:.2f}s] {s.speaker}: {s.text}" for s in utterances)
    return TranscriptionResult(text, utterances,
                               next(iter(providers)) if len(providers) == 1 else "mixed",
                               {"channel_provenance": True, "identity_inferred": False,
                                "source_sha256": digest, "pipeline_id": pipeline_id})
