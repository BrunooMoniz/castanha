"""Processador de notas: transforma transcrição bruta (Bronze) em Silver (Markdown) e Gold (Fatos)."""

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional
from castanha.config import load_config

SILVER_SYSTEM_PROMPT = """Você é o assistente executivo de notas de reunião do Castanha.
Seu objetivo é transformar transcrições de áudio e metadados de reuniões em notas de alta qualidade ("Silver").
Escreva em Português do Brasil, tom direto, profissional e objetivo. Sem floreios.

Estrutura obrigatória da saída Markdown:
# {Título da Reunião}

## 📌 Resumo Executivo
(Parágrafo conciso com os objetivos centrais e o resultado da reunião)

## 💬 Principais Discussões
(Tópicos discutidos com contexto e posições defendidas pelos participantes)

## 🎯 Decisões Tomadas
(Lista com bullets objetivos do que foi de fato decidido e aprovado)

## ✅ Próximos Passos & Tarefas
- [ ] Tarefa (Responsável: Nome / Prazo: quando aplicável)

## 📝 Transcrição Estruturada
(Transcrição limpa, com pontuação e agrupada por temas ou falantes)
"""

GOLD_SYSTEM_PROMPT = """Você é um extrator de fatos atômicos para o cérebro de memória durável (Zinom / LLM Wiki).
A partir do transcript e resumo da reunião, extraia:
1. "facts": lista de trios {"subject": str, "predicate": str, "object": str}
   Exemplos:
   - {"subject": "Bruno Moniz", "predicate": "aprovou", "object": "arquitetura do plugin Castanha"}
   - {"subject": "Projeto Castanha", "predicate": "usa", "object": "captura PipeWire em dois canais"}
2. "decisions": lista de strings com decisões duráveis
3. "action_items": lista de {"task": str, "assignee": str, "deadline": str | null}
4. "people_notes": lista de {"name": str, "note": str} com contexto relevante sobre os participantes

Retorne EXCLUSIVAMENTE um objeto JSON válido no formato:
{
  "facts": [...],
  "decisions": [...],
  "action_items": [...],
  "people_notes": [...]
}
"""

class MeetingSummarizer:
    def __init__(self):
        cfg = load_config()
        llm_cfg = cfg.get("llm", {})
        self.provider = llm_cfg.get("provider", "groq")
        self.api_key = llm_cfg.get("api_key") or os.environ.get("GROQ_API_KEY", "")
        self.model = llm_cfg.get("model", "llama-3.3-70b-versatile")

    def _call_llm(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        if not self.api_key:
            return ""

        url = "https://api.groq.com/openai/v1/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Castanha-Omarchy/0.1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return res["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[Castanha] Erro na chamada LLM: {e}")
            return ""

    def generate_silver(self, metadata: Dict[str, Any], raw_transcript: str) -> str:
        title = metadata.get("title", "Reunião")
        date_str = metadata.get("recorded_at", "")
        attendees = metadata.get("calendar_event", {}).get("attendees", [])
        attendees_str = ", ".join([a.get("name") or a.get("email", "") for a in attendees]) or "Não identificados"

        prompt = f"""Título: {title}
Data: {date_str}
Participantes: {attendees_str}

Transcrição Bruta:
{raw_transcript}
"""

        llm_output = self._call_llm(SILVER_SYSTEM_PROMPT, prompt)

        # Se a LLM não estiver configurada ou falhar, usa template de fallback
        if not llm_output:
            llm_output = f"""# {title}

## 📌 Resumo Executivo
Reunião gravada pelo Castanha em {date_str}. Participantes: {attendees_str}.

## 💬 Principais Discussões
Discussões gravadas via captura direta PipeWire.

## 🎯 Decisões Tomadas
- Gravação registrada com sucesso na esteira Bronze do Castanha.

## ✅ Próximos Passos & Tarefas
- [ ] Revisar transcrição bruta e aprovar fatos para o Zinom.

## 📝 Transcrição Estruturada
{raw_transcript}
"""

        # Adiciona Frontmatter YAML padrão para Markdown / Obsidian / LLM Wiki
        frontmatter = f"""---
title: "{title}"
date: "{date_str}"
duration_seconds: {metadata.get('duration_seconds', 0)}
mode: "{metadata.get('mode', 'dual')}"
attendees:
"""
        for att in attendees:
            name = att.get("name", "")
            email = att.get("email", "")
            frontmatter += f'  - name: "{name}"\n    email: "{email}"\n'
        if not attendees:
            frontmatter += "  []\n"
        frontmatter += "tags:\n  - meeting\n  - castanha\n  - silver\n---\n\n"

        return frontmatter + llm_output

    def generate_gold(self, metadata: Dict[str, Any], silver_content: str, raw_transcript: str) -> Dict[str, Any]:
        prompt = f"""Metadados da Reunião:
{json.dumps(metadata, indent=2, ensure_ascii=False)}

Notas Silver:
{silver_content}

Transcrição:
{raw_transcript[:4000]}
"""

        llm_output = self._call_llm(GOLD_SYSTEM_PROMPT, prompt, json_mode=True)
        if llm_output:
            try:
                return json.loads(llm_output)
            except Exception:
                pass

        # Fallback estruturado
        attendees = metadata.get("calendar_event", {}).get("attendees", [])
        facts = []
        for att in attendees:
            name = att.get("name")
            if name:
                facts.append({
                    "subject": name,
                    "predicate": "participou_da_reuniao",
                    "object": metadata.get("title", "Reunião"),
                })

        return {
            "facts": facts,
            "decisions": [],
            "action_items": [],
            "people_notes": [{"name": a.get("name", ""), "note": "Presente na reunião"} for a in attendees],
        }
