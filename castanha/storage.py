"""Gerenciamento de armazenamento nas camadas Bronze, Silver e Gold."""

import json
import hashlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from castanha.config import load_config
from castanha.durability import atomic_write, write_json
from castanha.i18n import t

AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".opus", ".flac", ".aac"}

def format_bytes(size: int) -> str:
    """Formata tamanho de arquivo em formato legível (KB, MB, GB)."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"

def _title_from_slug(slug: str) -> str:
    """Fallback para reunião cujo Bronze sumiu: '2026-09-04_1042_reuniao' -> 'reuniao'."""
    partes = slug.split("_", 2)
    return partes[2].replace("-", " ").strip().capitalize() if len(partes) == 3 else slug

def _extract_summary_preview(silver_content: str, max_chars: int = 260) -> str:
    """Extrai o primeiro parágrafo do Resumo Executivo das notas Silver."""
    if not silver_content:
        return ""
    content = silver_content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2]
    
    match = re.search(r"##\s*[📌]?\s*Resumo Executivo\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        if lines:
            first_para = " ".join(lines)
            if len(first_para) > max_chars:
                return first_para[:max_chars - 1].rstrip() + "…"
            return first_para
    return ""



def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50] or "reuniao"

def _pode_reprocessar(recordings: List[Dict[str, Any]], has_transcript: bool, meta: Dict[str, Any]) -> bool:
    """Há áudio no Bronze que ainda não virou texto: dá para tentar de novo.

    Gravação muda também fica sem transcrição, mas repetir não inventa fala:
    ela não ganha o botão. Com mais de uma gravação, basta uma ter falhado.
    """
    if not recordings:
        return False
    if (meta.get("audio_status") or "") == "sem_audio" and not has_transcript:
        return False
    if meta.get("summary_status") == "pending" and has_transcript:
        # Transcrito, mas a LLM não respondeu: "tentar de novo" refaz só o resumo.
        return True
    if any(r.get("transcribed") is False for r in recordings):
        return True
    return not has_transcript


class MeetingStorage:
    def __init__(self, base_dir: Optional[Path] = None):
        cfg = load_config()
        storage_cfg = cfg.get("storage", {})
        
        if base_dir is not None:
            # base_dir explícito manda em tudo. Antes a config ainda ditava os
            # subdiretórios, e por isso a suíte de testes escrevia nas notas reais.
            self.base_dir = Path(base_dir).expanduser()
            self.bronze_dir = self.base_dir / "bronze"
            self.silver_dir = self.base_dir / "silver"
            self.gold_dir = self.base_dir / "gold"
        else:
            self.base_dir = Path(storage_cfg.get("base_dir", "~/Notes/Meetings")).expanduser()
            self.bronze_dir = Path(storage_cfg.get("bronze_dir", self.base_dir / "bronze")).expanduser()
            self.silver_dir = Path(storage_cfg.get("silver_dir", self.base_dir / "silver")).expanduser()
            self.gold_dir = Path(storage_cfg.get("gold_dir", self.base_dir / "gold")).expanduser()

        for d in [self.bronze_dir, self.silver_dir, self.gold_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def create_meeting_slug(self, title: str, dt: Optional[datetime] = None) -> str:
        if dt is None:
            dt = datetime.now()
        timestamp = dt.strftime("%Y-%m-%d_%H%M")
        slug = slugify(title)
        base_slug = f"{timestamp}_{slug}"
        candidate = base_slug
        counter = 1
        while (self.bronze_dir / candidate).exists() or (self.silver_dir / f"{candidate}.md").exists():
            counter += 1
            candidate = f"{base_slug}-{counter}"
        return candidate

    def save_bronze(
        self,
        slug: str,
        audio_source_path: Path,
        metadata: Dict[str, Any],
        raw_transcript: str,
    ) -> Path:
        target_dir = self.bronze_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)

        # Copia ou move áudio para a pasta bronze
        target_audio = target_dir / f"audio{audio_source_path.suffix}"
        if audio_source_path.exists() and audio_source_path != target_audio:
            atomic_write(target_audio, audio_source_path)

        # Salva metadados
        metadata["bronze_audio_file"] = str(target_audio)
        if "recordings" not in metadata:
            size = target_audio.stat().st_size if target_audio.exists() else 0
            metadata["recordings"] = [{
                "id": target_audio.name,
                "filename": target_audio.name,
                "path": str(target_audio),
                "size_bytes": size,
                "size_human": format_bytes(size),
                "recorded_at": metadata.get("recorded_at") or datetime.now().isoformat(),
                "duration_seconds": metadata.get("duration_seconds") or 0,
                "audio_status": metadata.get("audio_status") or "ok",
            }]
        metadata["recordings_count"] = len(metadata.get("recordings", []))

        metadata_file = target_dir / "metadata.json"
        write_json(metadata_file, metadata)

        # Salva transcrição bruta
        transcript_file = target_dir / "transcript_raw.txt"
        atomic_write(transcript_file, raw_transcript)

        return target_dir

    def add_recording(
        self,
        slug: str,
        audio_source_path: Path,
        metadata_update: Optional[Dict[str, Any]] = None,
        raw_transcript: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adiciona uma gravação extra a uma reunião já existente."""
        target_dir = self.bronze_dir / slug
        if not target_dir.exists():
            raise FileNotFoundError(f"Reunião {slug} não encontrada no Bronze.")

        meta = self._read_bronze_metadata(slug)
        existing_recordings = meta.get("recordings", [])

        ext = audio_source_path.suffix or ".ogg"
        if not (target_dir / f"audio{ext}").exists():
            dest_name = f"audio{ext}"
        else:
            counter = len(existing_recordings) + 1
            dest_name = f"audio_{counter}{ext}"
            while (target_dir / dest_name).exists():
                counter += 1
                dest_name = f"audio_{counter}{ext}"

        dest_file = target_dir / dest_name
        atomic_write(dest_file, audio_source_path)
        size = dest_file.stat().st_size if dest_file.exists() else 0

        dur = (metadata_update or {}).get("duration_seconds") or 0
        rec_entry = {
            "id": dest_name,
            "filename": dest_name,
            "path": str(dest_file),
            "size_bytes": size,
            "size_human": format_bytes(size),
            "recorded_at": datetime.now().isoformat(),
            "duration_seconds": dur,
            "audio_status": (metadata_update or {}).get("audio_status", "ok"),
        }
        # Por gravação, para o "tentar de novo" saber qual arquivo ficou sem texto.
        for chave in ("transcribed", "transcription_error"):
            if chave in (metadata_update or {}):
                rec_entry[chave] = metadata_update[chave]
        if raw_transcript and raw_transcript.strip():
            rec_entry["transcribed"] = True
        existing_recordings.append(rec_entry)
        meta["recordings"] = existing_recordings
        meta["duration_seconds"] = (meta.get("duration_seconds") or 0) + dur
        meta["recordings_count"] = len(existing_recordings)
        meta["bronze_audio_file"] = str(dest_file)

        meta_file = target_dir / "metadata.json"
        write_json(meta_file, meta)

        if raw_transcript and raw_transcript.strip():
            transcript_file = target_dir / "transcript_raw.txt"
            now_str = datetime.now().strftime("%H:%M")
            append_text = f"\n\n--- Gravação {dest_name} ({now_str}) ---\n{raw_transcript}\n"
            with open(transcript_file, "a", encoding="utf-8") as f:
                f.write(append_text)

        return rec_entry

    def list_meeting_recordings(self, slug: str) -> List[Dict[str, Any]]:
        """Lista os arquivos de áudio gravados associados a uma reunião."""
        target_dir = self.bronze_dir / slug
        if not target_dir.exists():
            return []

        meta = self._read_bronze_metadata(slug)
        meta_recs = {r.get("filename"): r for r in meta.get("recordings", []) if isinstance(r, dict)}

        audio_files = sorted(
            [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS],
            key=lambda p: p.name
        )

        recordings = []
        for p in audio_files:
            size = p.stat().st_size
            meta_item = meta_recs.get(p.name, {})
            recordings.append({
                "id": p.name,
                "filename": p.name,
                "path": str(p),
                "size_bytes": size,
                "size_human": format_bytes(size),
                "duration_seconds": meta_item.get("duration_seconds") or 0,
                "recorded_at": meta_item.get("recorded_at") or "",
                "audio_status": meta_item.get("audio_status") or "ok",
                "transcribed": meta_item.get("transcribed"),
                "transcription_error": meta_item.get("transcription_error"),
                "exists": True,
            })
        return recordings

    def read_transcript(self, slug: str) -> str:
        arquivo = self.bronze_dir / slug / "transcript_raw.txt"
        try:
            return arquivo.read_text(encoding="utf-8") if arquivo.exists() else ""
        except Exception:
            return ""

    def append_transcript(self, slug: str, filename: str, texto: str) -> None:
        """Anexa a transcrição de uma gravação; a primeira entra sem cabeçalho.

        Nunca trunca: o texto que já estava lá é a única cópia.
        """
        if not texto or not texto.strip():
            return
        arquivo = self.bronze_dir / slug / "transcript_raw.txt"
        existente = self.read_transcript(slug)
        if existente.strip():
            now_str = datetime.now().strftime("%H:%M")
            atomic_write(arquivo, existente + f"\n\n--- Gravação {filename} ({now_str}) ---\n{texto}\n")
        else:
            atomic_write(arquivo, texto)

    def update_recording(self, slug: str, filename: str, **campos: Any) -> None:
        """Atualiza os campos de uma gravação no metadata (transcribed, erro, duração)."""
        meta = self._read_bronze_metadata(slug)
        recs = [r for r in (meta.get("recordings") or []) if isinstance(r, dict)]
        for r in recs:
            if r.get("filename") == filename:
                r.update(campos)
                break
        else:
            recs.append({"id": filename, "filename": filename, **campos})
        meta["recordings"] = recs
        meta["recordings_count"] = len(recs)
        self.write_bronze_metadata(slug, meta)

    def write_bronze_metadata(self, slug: str, meta: Dict[str, Any]) -> None:
        arquivo = self.bronze_dir / slug / "metadata.json"
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        write_json(arquivo, meta)

    def delete_recording(self, slug: str, recording_name: Optional[str] = None) -> Dict[str, Any]:
        """Apaga um arquivo de áudio específico sem apagar a reunião, suas notas ou transcrição."""
        target_dir = self.bronze_dir / slug
        if not target_dir.exists():
            return {"status": "error", "message": f"Reunião '{slug}' não encontrada."}

        recordings = self.list_meeting_recordings(slug)
        if not recordings:
            return {"status": "error", "message": f"Nenhuma gravação de áudio encontrada na reunião '{slug}'."}

        if recording_name:
            target_name = Path(recording_name).name
            matching = [r for r in recordings if r["filename"] == target_name]
            if not matching:
                return {"status": "error", "message": f"Gravação '{target_name}' não encontrada na reunião '{slug}'."}
            target_rec = matching[0]
        else:
            if len(recordings) == 1:
                target_rec = recordings[0]
            else:
                nomes = ", ".join([r["filename"] for r in recordings])
                return {
                    "status": "error",
                    "message": f"A reunião possui múltiplas gravações ({nomes}). Especifique qual deseja apagar.",
                }

        target_path = Path(target_rec["path"])
        if target_path.exists():
            target_path.unlink()

        meta = self._read_bronze_metadata(slug)
        existing_recs = meta.get("recordings", [])
        new_recs = [r for r in existing_recs if r.get("filename") != target_rec["filename"]]
        meta["recordings"] = new_recs

        remaining = self.list_meeting_recordings(slug)
        if remaining:
            meta["bronze_audio_file"] = remaining[0]["path"]
        else:
            meta["bronze_audio_file"] = None
            meta["audio_status"] = "audio_apagado"
            meta["audio_diagnostico"] = t("audio_status.audio_apagado")

        meta_file = target_dir / "metadata.json"
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        return {
            "status": "ok",
            "slug": slug,
            "deleted_file": target_rec["filename"],
            "deleted_path": str(target_path),
            "remaining_recordings": [r["filename"] for r in remaining],
            "remaining_count": len(remaining),
        }

    def save_silver(
        self,
        slug: str,
        markdown_content: str,
    ) -> Path:
        target_file = self.silver_dir / f"{slug}.md"
        atomic_write(target_file, markdown_content)
        return target_file

    def save_gold(
        self,
        slug: str,
        gold_data: Dict[str, Any],
    ) -> Path:
        target_file = self.gold_dir / f"{slug}.json"
        write_json(target_file, gold_data)
        return target_file

    def delivery_projection(self, slug: str, previous: Dict[str, Any]) -> Dict[str, Any]:
        """Estado para apresentação, nunca persistido nos originais congelados."""
        from castanha.legacy_recovery import legacy_delivery_projection
        if not isinstance(slug, str) or not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
            return previous
        if (self.bronze_dir / slug / ".brain-ingest" / "destination.json").exists():
            # A síntese entrou pela ponte Bronze; o recibo vivo é o do metadata.
            return previous
        receipt = legacy_delivery_projection(self.bronze_dir / slug)
        return receipt if receipt is not None else previous

    def status_projection(self, state: Dict[str, Any]) -> Dict[str, Any]:
        last = state.get("last_result")
        if not isinstance(last, dict):
            return state
        return {**state, "last_result": {**last, "zinom": self.delivery_projection(
            last.get("slug"), last.get("zinom") or {})}}

    def list_recent_meetings(self, limit: int = 10) -> List[Dict[str, Any]]:
        """As últimas reuniões, com metadados detalhados, participantes, transcrição e gravações."""
        results = []
        slugs = set()
        if self.silver_dir.exists():
            for f in self.silver_dir.glob("*.md"):
                slugs.add(f.stem)
        if self.bronze_dir.exists():
            for d in self.bronze_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    slugs.add(d.name)

        if not slugs:
            return results

        def _get_mtime(s: str) -> float:
            sf = self.silver_dir / f"{s}.md"
            if sf.exists():
                return sf.stat().st_mtime
            bd = self.bronze_dir / s
            if bd.exists():
                return bd.stat().st_mtime
            return 0.0

        sorted_slugs = sorted(slugs, key=_get_mtime, reverse=True)

        for slug in sorted_slugs[:limit]:
            meta = self._read_bronze_metadata(slug)
            silver_file = self.silver_dir / f"{slug}.md"
            transcript_file = self.bronze_dir / slug / "transcript_raw.txt"
            has_transcript = transcript_file.exists() and transcript_file.stat().st_size > 0
            gold_file = self.gold_dir / f"{slug}.json"

            cal_evt = meta.get("calendar_event") or {}
            attendees = cal_evt.get("attendees") or meta.get("attendees") or []
            recordings = self.list_meeting_recordings(slug)

            summary_preview = ""
            if silver_file.exists():
                try:
                    summary_preview = _extract_summary_preview(silver_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            provider = meta.get("transcription_provider") or ""
            error = meta.get("transcription_error") or ""
            can_retry = _pode_reprocessar(recordings, has_transcript, meta)

            results.append({
                "slug": slug,
                "title": meta.get("title") or _title_from_slug(slug),
                "when": meta.get("recorded_at") or "",
                "duration_seconds": meta.get("duration_seconds") or 0,
                "mode": meta.get("mode") or "dual",
                "audio_status": meta.get("audio_status") or ("ok" if recordings else "audio_apagado"),
                "audio_diagnostico": meta.get("audio_diagnostico") or "",
                "zinom": self.delivery_projection(slug, meta.get("zinom") or {}),
                "silver_path": str(silver_file) if silver_file.exists() else "",
                "bronze_dir": str(self.bronze_dir / slug),
                "gold_path": str(gold_file) if gold_file.exists() else "",
                "transcript_path": str(transcript_file) if has_transcript else "",
                "has_transcript": has_transcript,
                "transcription_provider": provider,
                "transcription_error": error,
                "can_retry": can_retry,
                "processing_status": meta.get("processing_status") or "",
                "summary_status": meta.get("summary_status") or "",
                "summary_error": meta.get("summary_error") or "",
                "attendees": attendees,
                "recordings": recordings,
                "recordings_count": len(recordings),
                "has_audio": len(recordings) > 0,
                "summary_preview": summary_preview,
                "modified": _get_mtime(slug),
            })
        return results

    def get_meeting(self, slug: str) -> Optional[Dict[str, Any]]:
        """Busca os detalhes completos de uma reunião pelo seu slug."""
        silver_file = self.silver_dir / f"{slug}.md"
        bronze_dir = self.bronze_dir / slug
        if not silver_file.exists() and not bronze_dir.exists():
            return None
        meta = self._read_bronze_metadata(slug)
        transcript_file = bronze_dir / "transcript_raw.txt"
        has_transcript = transcript_file.exists() and transcript_file.stat().st_size > 0
        gold_file = self.gold_dir / f"{slug}.json"

        cal_evt = meta.get("calendar_event") or {}
        attendees = cal_evt.get("attendees") or meta.get("attendees") or []
        recordings = self.list_meeting_recordings(slug)

        summary_preview = ""
        if silver_file.exists():
            try:
                summary_preview = _extract_summary_preview(silver_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        provider = meta.get("transcription_provider") or ""
        error = meta.get("transcription_error") or ""
        can_retry = _pode_reprocessar(recordings, has_transcript, meta)

        return {
            "slug": slug,
            "title": meta.get("title") or _title_from_slug(slug),
            "when": meta.get("recorded_at") or "",
            "duration_seconds": meta.get("duration_seconds") or 0,
            "mode": meta.get("mode") or "dual",
            "audio_status": meta.get("audio_status") or ("ok" if recordings else "audio_apagado"),
            "audio_diagnostico": meta.get("audio_diagnostico") or "",
            "zinom": self.delivery_projection(slug, meta.get("zinom") or {}),
            "processing_status": meta.get("processing_status") or "",
            "summary_status": meta.get("summary_status") or "",
            "summary_error": meta.get("summary_error") or "",
            "silver_path": str(silver_file) if silver_file.exists() else "",
            "bronze_dir": str(bronze_dir),
            "gold_path": str(gold_file) if gold_file.exists() else "",
            "transcript_path": str(transcript_file) if has_transcript else "",
            "has_transcript": has_transcript,
            "transcription_provider": provider,
            "transcription_error": error,
            "can_retry": can_retry,
            "attendees": attendees,
            "recordings": recordings,
            "recordings_count": len(recordings),
            "has_audio": len(recordings) > 0,
            "summary_preview": summary_preview,
            "modified": silver_file.stat().st_mtime if silver_file.exists() else (bronze_dir.stat().st_mtime if bronze_dir.exists() else 0.0),
        }

    def delivery_content_sha256(self, slug: str) -> str:
        """Identidade local do conteúdo, sem incluir o próprio recibo de envio."""
        metadata = self._read_bronze_metadata(slug)
        metadata.pop("zinom", None)
        parts = [json.dumps(metadata, sort_keys=True, ensure_ascii=False)]
        for path in (self.silver_dir / f"{slug}.md", self.gold_dir / f"{slug}.json"):
            parts.append(path.read_text(encoding="utf-8") if path.exists() else None)
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()

    def record_zinom_result(self, slug: str, resultado: Dict[str, Any]) -> None:
        """Anota no Bronze o que o Zinom fez com esta reunião."""
        arquivo = self.bronze_dir / slug / "metadata.json"
        if not arquivo.exists():
            return
        metadata = self._read_bronze_metadata(slug)
        remember = (resultado or {}).get("remember") or {}
        previous = metadata.get("zinom") or {}
        content_sha256 = previous.get("local_content_sha256")
        if resultado.get("note_status") == "ok" and remember.get("id"):
            content_sha256 = self.delivery_content_sha256(slug)
        metadata["zinom"] = {
            "status": (resultado or {}).get("status", "error"),
            "remember_id": remember.get("id") or (metadata.get("zinom") or {}).get("remember_id"),
            "facts_ingested": (resultado or {}).get("facts_ingested", 0),
            "facts_descartados": (resultado or {}).get("facts_descartados", previous.get("facts_descartados", 0)),
            "facts_status": resultado.get("facts_status", previous.get("facts_status", "none")),
            "facts_pending": resultado.get("facts_pending", previous.get("facts_pending", [])),
            "source": resultado.get("source", previous.get("source")),
            "note_status": resultado.get("note_status", "pending" if resultado.get("status") in ("pending", "error")
                                         else previous.get("note_status")),
            "local_content_sha256": content_sha256,
            "errors": (resultado or {}).get("errors", []),
            "reason": (resultado or {}).get("reason"),
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
        write_json(arquivo, metadata)

    def _read_bronze_metadata(self, slug: str) -> Dict[str, Any]:
        arquivo = self.bronze_dir / slug / "metadata.json"
        try:
            return json.loads(arquivo.read_text(encoding="utf-8"))
        except Exception:
            return {}
