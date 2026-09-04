"""Gerenciamento de armazenamento nas camadas Bronze, Silver e Gold."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from castanha.config import load_config

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50] or "reuniao"

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
        return f"{timestamp}_{slug}"

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
            shutil.copy2(audio_source_path, target_audio)

        # Salva metadados
        metadata_file = target_dir / "metadata.json"
        metadata["bronze_audio_file"] = str(target_audio)
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        # Salva transcrição bruta
        transcript_file = target_dir / "transcript_raw.txt"
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(raw_transcript)

        return target_dir

    def save_silver(
        self,
        slug: str,
        markdown_content: str,
    ) -> Path:
        target_file = self.silver_dir / f"{slug}.md"
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        return target_file

    def save_gold(
        self,
        slug: str,
        gold_data: Dict[str, Any],
    ) -> Path:
        target_file = self.gold_dir / f"{slug}.json"
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(gold_data, f, indent=2, ensure_ascii=False)
        return target_file

    def list_recent_meetings(self, limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        if not self.silver_dir.exists():
            return results

        files = sorted(self.silver_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:limit]:
            results.append({
                "slug": f.stem,
                "silver_path": str(f),
                "modified": f.stat().st_mtime,
            })
        return results
