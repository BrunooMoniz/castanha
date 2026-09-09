"""Gerenciamento de configuração do Castanha."""

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

from castanha.secure_io import (
    InsecureConfigError,
    read_private_json,
    write_private_json,
)

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
        "zinom": {
            # As contas Google já conectadas no portal do Zinom. Sem feed iCal,
            # sem credencial do Google guardada aqui.
            "enabled": True,
            "poll_interval_sec": 300,
            "window_hours": 12,
            # Vazio = a principal de cada conta mais as agendas que ele pode
            # editar (owner/writer). Só-leitura (feriados, agenda alheia) fica fora.
            "calendars": [],
            # Dia inteiro entra: lembrete também é agenda. O que não for
            # reunião, ele esconde no painel (série inteira).
            "skip_all_day": False,
        },
    },
    "transcription": {
        "provider": "vps_ssh",  # Groq somente por revisão manual explícita.
        "provider_revision": 0,
        "fallback_from": None,
        "groq_fallback_mode": "manual",
        "vps_model": "large-v3",
        "vps_compute_type": "int8",
        "vps_cpu_threads": 8,
        "vps_multilingual": True,
        "vps_worker_contract": "faster-whisper-json-v2",
        "vps_condition_on_previous_text": False,
        # Sem estratégia aprovada: contrato incompleto mantém o envio pendente.
        "vps_segmentation_strategy": "whisper-vad-v1",
        "vps_chunk_length": 30,
        "language": "auto",  # "auto" (detecta PT/EN/misto), "pt", "en", etc.
        "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
        "groq_model": "whisper-large-v3-turbo",
        "deepgram_api_key": os.environ.get("DEEPGRAM_API_KEY", ""),
        "deepgram_model": "nova-2",
        "vps_endpoint": "",
        "vps_auth_token": "",
        # Transcrição por canal (microfone e sistema separados). DESLIGADA: ligar
        # dobra os minutos cobrados e depende do contrato F4 e da QA no XPS.
        "por_canal": False,
    },
    "llm": {
        # "hermes_ssh": Hermes Agent na VPS, com as assinaturas do Bruno (Claude e
        # Codex), sem ferramentas nem memória. "groq" continua válido como primário.
        "provider": "hermes_ssh",
        "hermes_ssh_host": "zinom-vps-2",
        # Cadeia ordenada: o próximo modelo só entra quando o anterior falhou de fato.
        "hermes_models": [
            {"provider": "anthropic", "model": "claude-opus-5"},
            {"provider": "openai-codex", "model": "gpt-5.5"},
        ],
        "hermes_reasoning": "medium",
        "hermes_timeout_sec": 900,
        # Reserva quando a cadeia Hermes inteira falha; "" desliga a reserva.
        "fallback_provider": "groq",
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
    # Cópia profunda: a rasa deixava o `update` de cada seção escrever DENTRO
    # do DEFAULT_CONFIG, e uma config lida antes contaminava a seguinte no
    # mesmo processo (na suíte, o `provider: mock` de um teste vazava para o
    # outro).
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        # Leitura por descritor, sem seguir symlink e com teto de bytes: o
        # arquivo guarda chaves de Groq, Deepgram, VPS e Zinom, então um
        # symlink plantado no caminho não pode virar fonte de config nem
        # destino de escrita.
        user_data = read_private_json(cfg_file)
    except InsecureConfigError as e:
        # Caminho inseguro não é "config ausente": seguir com os padrões
        # esconderia que outra pessoa controla o arquivo de credenciais.
        print(f"[Castanha] Configuração recusada por segurança: {e}", file=sys.stderr)
        user_data = None
    except Exception as e:
        print(f"[Castanha] Erro ao ler config: {e}. Usando padrões.", file=sys.stderr)
        user_data = None

    if isinstance(user_data, dict):
        # Merge recursivo simples
        for section, vals in user_data.items():
            if isinstance(vals, dict) and section in config:
                config[section].update(vals)
            else:
                config[section] = vals
    return config

def save_config(config_data: Dict[str, Any]) -> None:
    # Publicação atômica como arquivo comum `0600` dentro de um diretório
    # `0700`: nunca existe em disco uma janela em que o segredo esteja legível
    # por outro usuário local.
    write_private_json(get_config_file(), config_data)
