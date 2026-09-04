"""Gerenciamento de configuração do Castanha."""

import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "storage": {
        "base_dir": "~/Notes/Meetings",
        "bronze_dir": "~/Notes/Meetings/bronze",
        "silver_dir": "~/Notes/Meetings/silver",
        "gold_dir": "~/Notes/Meetings/gold",
    },
    "audio": {
        "default_mode": "dual",  # "dual" (mic + som do sistema) ou "mic_only" (só mic)
        "bitrate": "64k",
        "sample_rate": 48000,
        "format": "ogg",
    },
    "calendar": {
        "enabled": True,
        "poll_interval_sec": 60,
        "notify_minutes_before": 2,
        "auto_record": False,
        "feeds": [],  # lista de dicts: {"name": "...", "url": "..."}
    },
    "transcription": {
        "provider": "groq",  # "groq", "deepgram", "local_whisper", "vps_webhook", "none"
        "language": "auto",  # "auto" (detecta PT/EN/misto), "pt", "en", etc.
        "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
        "groq_model": "whisper-large-v3-turbo",
        "deepgram_api_key": os.environ.get("DEEPGRAM_API_KEY", ""),
        "deepgram_model": "nova-2",
        "vps_endpoint": "",
        "vps_auth_token": "",
    },
    "llm": {
        "provider": "groq",  # "groq", "openai", "openrouter", "vps"
        "api_key": os.environ.get("GROQ_API_KEY", ""),
        "model": "openai/gpt-oss-120b",  # llama-3.3-70b-versatile foi descontinuado na Groq
    },
    "zinom": {
        "enabled": False,
        "endpoint": "https://zinom.ai/mcp",
        "token": "",
    },
}

def get_config_dir() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "castanha"
    return Path.home() / ".config" / "castanha"

def get_config_file() -> Path:
    return get_config_dir() / "config.json"

def get_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "castanha"
    return Path.home() / ".local" / "state" / "castanha"

def load_config() -> Dict[str, Any]:
    cfg_file = get_config_file()
    config = dict(DEFAULT_CONFIG)
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                # Merge recursivo simples
                for section, vals in user_data.items():
                    if isinstance(vals, dict) and section in config:
                        config[section].update(vals)
                    else:
                        config[section] = vals
        except Exception as e:
            print(f"[Castanha] Erro ao ler config: {e}. Usando padrões.")
    return config

def save_config(config_data: Dict[str, Any]) -> None:
    cfg_dir = get_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    with open(get_config_file(), "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
