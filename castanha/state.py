"""Gerenciador de estado para comunicação com a UI do Omarchy."""

import json
import sys
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from castanha.config import get_state_dir

DEFAULT_STATE: Dict[str, Any] = {
    "status": "idle",  # idle, recording, paused, processing
    "pid": None,
    "processing_pid": None,
    "audio_path": None,
    "mode": "dual",
    "started_at": None,
    "elapsed_seconds": 0,
    "current_meeting": None,
    "mic_muted_at_start": None,
    "next_meeting": None,
    "upcoming_meetings": [],
    "agenda_error": None,
    "last_result": None,
    "error": None,
    "updated_at": 0,
}

def get_state_file() -> Path:
    return get_state_dir() / "state.json"

class StateManager:
    def __init__(self):
        self.state_dir = get_state_dir()
        self.state_file = get_state_file()
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return dict(DEFAULT_STATE)
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                state = dict(DEFAULT_STATE)
                state.update(data)
                return state
        except Exception:
            return dict(DEFAULT_STATE)

    def write(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = self.read()
        current.update(updates)
        current["updated_at"] = int(time.time())
        tmp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
            tmp_file.replace(self.state_file)
        except Exception as e:
            print(f"[Castanha] Erro ao salvar estado: {e}", file=sys.stderr)
        return current

    def reset(self) -> Dict[str, Any]:
        # A agenda não é estado da gravação: sobrevive ao reset.
        atual = self.read()
        state = dict(DEFAULT_STATE)
        for chave in ("next_meeting", "upcoming_meetings", "agenda_error"):
            state[chave] = atual.get(chave)
        state["updated_at"] = int(time.time())
        return self.write(state)
