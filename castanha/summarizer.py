"""Processador de notas: transforma transcrição bruta (Bronze) em Silver (Markdown) e Gold (Fatos)."""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from castanha.config import load_config

# O plano gratuito da Groq dá 8.000 tokens por MINUTO para o modelo de notas.
# Uma reunião de 2h13 (05/09/2026) tem 23.454 tokens de transcrição: a chamada
# única volta 413 e a nota saía como template vazio. Acima do limite, o texto
# vai em partes, uma por minuto, e uma chamada final consolida.
LLM_DEFAULT_TPM = 8000
# Fração do limite reservada para a entrada; o resto é prompt de sistema e resposta.
LLM_INPUT_SHARE = 0.6
# Português dá mais ou menos 4 caracteres por token; 3,5 é a margem.
CHARS_PER_TOKEN = 3.5
LLM_ATTEMPTS = 4
LLM_MAX_WAIT_SEC = 180.0

_LIMITE_RE = re.compile(r"Limit\s+(\d+).*?Requested\s+(\d+)", re.S)


class LlmTooLarge(Exception):
    """A Groq recusou a mensagem por tamanho (413). Traz o limite e o pedido, se ela disse."""

    def __init__(self, limit: Optional[int] = None, requested: Optional[int] = None, detail: str = ""):
        super().__init__(detail or f"mensagem grande demais (limite {limit}, pedido {requested})")
        self.limit = limit
        self.requested = requested


def _dividir_texto(texto: str, max_chars: int) -> List[str]:
    """Parte o texto em pedaços de até max_chars, cortando em fim de frase."""
    texto = texto.strip()
    if not texto:
        return []
    if len(texto) <= max_chars:
        return [texto]
    frases = re.split(r"(?<=[.!?])\s+|\n+", texto)
    partes: List[str] = []
    atual = ""
    for frase in frases:
        if not frase:
            continue
        while len(frase) > max_chars:
            # Frase sozinha maior que a parte: corte seco.
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(frase[:max_chars])
            frase = frase[max_chars:]
        if atual and len(atual) + 1 + len(frase) > max_chars:
            partes.append(atual)
            atual = frase
        else:
            atual = f"{atual} {frase}".strip() if atual else frase
    if atual:
        partes.append(atual)
    return partes


PARTIAL_SYSTEM_PROMPT = """Você recebe UMA PARTE de uma transcrição longa de reunião, em ordem.
Escreva em Português do Brasil, direto, sem floreios. Produza notas parciais só desta parte:

## Resumo da parte
(parágrafo curto)

## Discussões
(tópicos, com quem defendeu o quê, quando o nome aparece)

## Decisões
(só o que foi de fato decidido nesta parte; se nada, escreva "Nenhuma nesta parte")

## Tarefas
- [ ] Tarefa (Responsável: Nome / Prazo: quando dito)

Não invente, não complete, não reproduza a transcrição.
"""

COMBINE_SYSTEM_PROMPT = """Você recebe as notas parciais de uma reunião longa, na ordem em que a reunião aconteceu.
Consolide tudo em UMA nota final, em Português do Brasil, direto e objetivo, sem repetir e sem inventar.

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

Não inclua a transcrição: ela é anexada depois.
"""

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

O que você extrai é GRAVADO PARA SEMPRE e supersede o que já estava lá. Fato
errado ou irrelevante estraga a memória. Na dúvida, extraia menos.

REGRAS INEGOCIÁVEIS:
- Só entra o que foi DITO na reunião. Nunca infira, complete ou invente.
- Responsável e prazo só quando a pessoa foi nomeada e o prazo dito. Nunca
  escreva papéis genéricos ("Equipe de TI", "o time") que ninguém citou: use
  null.
- NADA sobre a gravação, o áudio, o microfone, a transcrição ou o teste da
  ferramenta. Isso é ruído de instrumentação, não é memória.
- Sujeito de fato é pessoa, empresa, projeto ou produto real, nomeado.
- Objeto de fato é um valor concreto, nunca "sim", "não", "true" ou "false":
  se o trio só faz sentido com booleano, ele não é um fato, descarte.
- Reunião só de teste, conversa fiada ou sem conteúdo durável devolve todas as
  listas vazias. Lista vazia é resposta certa e frequente.

A partir do transcript e resumo da reunião, extraia:
1. "facts": lista de trios {"subject": str, "predicate": str, "object": str}
   Predicado curto e reutilizável, no infinitivo ou como atributo.
   Exemplos bons:
   - {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"}
   - {"subject": "Projeto Castanha", "predicate": "usa", "object": "captura PipeWire em dois canais"}
   Exemplos que você NÃO pode devolver:
   - {"subject": "Microfone", "predicate": "estava mutado", "object": "sim"}
   - {"subject": "Teste de gravação", "predicate": "foi bem-sucedido", "object": "true"}
2. "decisions": lista de strings com decisões duráveis de fato tomadas
3. "action_items": lista de {"task": str, "assignee": str | null, "deadline": str | null}
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
        self.model = llm_cfg.get("model", "openai/gpt-oss-120b")

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

        ultimo: Any = None
        for tentativa in range(1, LLM_ATTEMPTS + 1):
            espera = float(2 ** tentativa)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    return res["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                corpo = ""
                try:
                    corpo = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == 413:
                    m = _LIMITE_RE.search(corpo)
                    limite = int(m.group(1)) if m else None
                    pedido = int(m.group(2)) if m else None
                    raise LlmTooLarge(limite, pedido, corpo[:200])
                if e.code in (408, 429) or e.code >= 500:
                    # 429 aqui é quase sempre a janela de um minuto do plano
                    # gratuito: o Retry-After diz quanto falta para ela abrir.
                    ultimo = f"HTTP {e.code}: {corpo[:200]}"
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        espera = float(ra) if ra else espera
                    except ValueError:
                        pass
                else:
                    print(f"[Castanha] Erro na chamada LLM: HTTP {e.code}: {corpo[:200]}", file=sys.stderr)
                    return ""
            except OSError as e:
                ultimo = e
            except (KeyError, IndexError, ValueError) as e:
                print(f"[Castanha] Resposta inesperada da LLM: {e}", file=sys.stderr)
                return ""
            if tentativa < LLM_ATTEMPTS:
                time.sleep(min(espera, LLM_MAX_WAIT_SEC))
        print(f"[Castanha] Erro na chamada LLM depois de {LLM_ATTEMPTS} tentativas: {ultimo}", file=sys.stderr)
        return ""

    # ------------------------------------------------------------ partes
    def _tamanho_da_parte(self, texto: str, erro: LlmTooLarge) -> int:
        """Quantos caracteres cabem numa chamada, pelo que a Groq disse no 413."""
        limite = erro.limit or LLM_DEFAULT_TPM
        chars_por_token = (len(texto) / erro.requested) if erro.requested else CHARS_PER_TOKEN
        chars_por_token = min(chars_por_token, CHARS_PER_TOKEN + 1.0)
        return max(2000, int(limite * LLM_INPUT_SHARE * chars_por_token))

    def _silver_em_partes(self, title: str, cabecalho: str, raw_transcript: str, part_chars: int, tentativa: int = 1) -> str:
        """Mapa e consolidação: notas parciais por parte, depois uma nota final."""
        partes = _dividir_texto(raw_transcript, part_chars)
        n = len(partes)
        parciais: List[str] = []
        for i, parte in enumerate(partes, 1):
            prompt = f"{cabecalho}\nParte {i} de {n} da transcrição:\n{parte}\n"
            try:
                saida = self._call_llm(PARTIAL_SYSTEM_PROMPT, prompt)
            except LlmTooLarge:
                if tentativa >= 2:
                    return ""
                return self._silver_em_partes(title, cabecalho, raw_transcript, max(2000, part_chars // 2), tentativa + 1)
            if not saida.strip():
                return ""
            parciais.append(f"### Parte {i} de {n}\n{saida.strip()}")

        juntas = "\n\n".join(parciais)
        try:
            final = self._call_llm(COMBINE_SYSTEM_PROMPT, f"{cabecalho}\n{juntas}\n")
        except LlmTooLarge:
            final = ""
        if not final.strip():
            # A consolidação não coube ou falhou: as parciais já são nota útil.
            final = f"# {title}\n\n## 📌 Resumo Executivo\nReunião longa, resumida em {n} partes abaixo.\n\n{juntas}"
        return final.strip() + f"\n\n## 📝 Transcrição Bruta\n{raw_transcript}\n"

    def generate_silver(self, metadata: Dict[str, Any], raw_transcript: str) -> str:
        title = metadata.get("title", "Reunião")
        date_str = metadata.get("recorded_at", "")
        attendees = metadata.get("calendar_event", {}).get("attendees", [])
        attendees_str = ", ".join([a.get("name") or a.get("email", "") for a in attendees]) or "Não identificados"

        cabecalho = f"""Título: {title}
Data: {date_str}
Participantes: {attendees_str}
"""
        prompt = f"""{cabecalho}
Transcrição Bruta:
{raw_transcript}
"""

        audio_status = metadata.get("audio_status", "ok")
        audio_aviso = metadata.get("audio_diagnostico", "")

        llm_output = ""
        if raw_transcript.strip():
            try:
                llm_output = self._call_llm(SILVER_SYSTEM_PROMPT, prompt)
            except LlmTooLarge as e:
                print(f"[Castanha] Transcrição grande para uma chamada ({e}); resumindo em partes...", file=sys.stderr)
                llm_output = self._silver_em_partes(title, cabecalho, raw_transcript, self._tamanho_da_parte(prompt, e))

        # Sem LLM configurada não existe resumo. O template abaixo diz isso em vez
        # de inventar "decisões tomadas" que ninguém tomou.
        if not llm_output:
            motivo = (
                "a gravação não tem áudio para resumir"
                if not raw_transcript.strip()
                else "o resumo automático não rodou (LLM não configurada ou indisponível)"
            )
            aviso = f"\n> ⚠️ {audio_aviso}\n" if audio_status not in ("ok", "desconhecido") else ""
            llm_output = f"""# {title}
{aviso}
## 📌 Resumo Executivo
Sem resumo: {motivo}. Gravada em {date_str}. Participantes: {attendees_str}.

## 💬 Principais Discussões
_Pendente: depende do resumo automático._

## 🎯 Decisões Tomadas
_Pendente: depende do resumo automático._

## ✅ Próximos Passos & Tarefas
- [ ] Revisar a transcrição bruta abaixo e aprovar os fatos para o Zinom.

## 📝 Transcrição Estruturada
{raw_transcript or "_(nenhum áudio capturado)_"}
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
        frontmatter += f'audio_status: "{metadata.get("audio_status", "ok")}"\n'
        frontmatter += "tags:\n  - meeting\n  - castanha\n  - silver\n---\n\n"

        return frontmatter + llm_output

    def generate_gold(self, metadata: Dict[str, Any], silver_content: str, raw_transcript: str) -> Dict[str, Any]:
        # O Silver carrega a transcrição inteira no fim; para os fatos bastam as
        # notas, senão uma reunião longa estoura o limite de novo.
        notas = re.split(r"\n## 📝 Transcrição", silver_content, maxsplit=1)[0]
        meta_enxuta = {k: v for k, v in metadata.items() if k not in ("audio_levels", "recordings", "zinom")}
        prompt = f"""Metadados da Reunião:
{json.dumps(meta_enxuta, indent=2, ensure_ascii=False)}

Notas Silver:
{notas}

Transcrição:
{raw_transcript[:4000]}
"""

        llm_output = ""
        if raw_transcript.strip():
            try:
                llm_output = self._call_llm(GOLD_SYSTEM_PROMPT, prompt, json_mode=True)
            except LlmTooLarge as e:
                print(f"[Castanha] Fatos: mensagem grande demais ({e}). Fica o fallback estruturado.", file=sys.stderr)
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
