"""Processador de notas: transforma transcrição bruta (Bronze) em Silver (Markdown) e Gold (Fatos)."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional
from castanha.config import get_state_dir, load_config
from castanha.secure_io import read_json_bounded

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


class LlmUnavailable(Exception):
    """LLM configurada, mas indisponível agora (cota, rede, servidor, provedor não suportado).

    Não é "sem resumo": a transcrição já está salva e o resumo fica PENDENTE,
    para a retomada refazer sem transcrever de novo. Antes disso, uma cota
    esgotada virava nota "Sem resumo" e reunião marcada como pronta.
    """


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


def _extrair_json(texto: str) -> Optional[Dict[str, Any]]:
    """O objeto JSON da resposta, mesmo com cerca de código ou prosa em volta."""
    if not texto or not texto.strip():
        return None
    candidatos = [texto.strip()]
    cerca = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.S)
    if cerca:
        candidatos.append(cerca.group(1))
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio != -1 and fim > inicio:
        candidatos.append(texto[inicio:fim + 1])
    for c in candidatos:
        try:
            dados = json.loads(c)
        except Exception:
            continue
        if isinstance(dados, dict):
            return dados
    return None


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
- Todo fato, decisão e tarefa traz "evidencia": um trecho copiado LITERALMENTE
  das Notas Silver de onde ele saiu (mínimo 20 caracteres), sem parafrasear,
  sem corrigir, sem juntar trechos. Ele é conferido byte a byte: se não bater,
  o item é descartado. Sem trecho literal, não devolva o item.

A partir do transcript e resumo da reunião, extraia:
1. "facts": lista de {"subject": str, "predicate": str, "object": str, "evidencia": str}
   Predicado curto e reutilizável, no infinitivo ou como atributo.
   Exemplos bons:
   - {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance",
      "evidencia": "Bruno apresentou-se como cofundador da Nora Finance"}
   - {"subject": "Projeto Castanha", "predicate": "usa", "object": "captura PipeWire em dois canais",
      "evidencia": "o Castanha captura o áudio pelo PipeWire em dois canais"}
   Exemplos que você NÃO pode devolver:
   - {"subject": "Microfone", "predicate": "estava mutado", "object": "sim"}
   - {"subject": "Teste de gravação", "predicate": "foi bem-sucedido", "object": "true"}
2. "decisions": lista de {"decision": str, "evidencia": str} com decisões duráveis de fato tomadas
3. "action_items": lista de {"task": str, "assignee": str | null, "deadline": str | null, "evidencia": str}
4. "people_notes": lista de {"name": str, "note": str} com contexto relevante sobre os participantes

Retorne EXCLUSIVAMENTE um objeto JSON válido no formato:
{
  "facts": [...],
  "decisions": [...],
  "action_items": [...],
  "people_notes": [...]
}
"""

CHANNEL_GROUNDING = """
ORIGEM DE ÁUDIO NÃO É IDENTIDADE:
- 'Microfone local' e 'Áudio do sistema' identificam canais, não pessoas.
- Não atribua automaticamente o microfone ao Bruno nem o sistema a um único
  participante. Pode haver outras pessoas na sala, eco e sons de outros aplicativos.
- Convite do calendário não prova presença nem identifica a voz. Nome citado
  na fala também não prova que essa pessoa foi quem falou.
- Preserve a origem e os tempos quando disponíveis. Sem evidência explícita
  de quem falou, mantenha autoria desconhecida e responsável null.
"""

# No caminho Hermes a transcrição não é pedida de volta: reescrever 2 h de fala
# são dezenas de milhares de tokens de saída. Ela é anexada localmente, íntegra.
SILVER_NOTES_SYSTEM_PROMPT = SILVER_SYSTEM_PROMPT.replace(
    "## 📝 Transcrição Estruturada\n(Transcrição limpa, com pontuação e agrupada por temas ou falantes)\n",
    "Não inclua a transcrição: ela é anexada depois.\n")

PARTIAL_SYSTEM_PROMPT += CHANNEL_GROUNDING
COMBINE_SYSTEM_PROMPT += CHANNEL_GROUNDING
SILVER_SYSTEM_PROMPT += CHANNEL_GROUNDING
SILVER_NOTES_SYSTEM_PROMPT += CHANNEL_GROUNDING
GOLD_SYSTEM_PROMPT += CHANNEL_GROUNDING


def _grounding_text(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


# Termos de presença também cobrem a atribuição coletiva sem nome próprio.
# Vale só para o Gold (fato durável): o Silver não é retido por citar convidado.
# Em 07/09 a retenção do Silver inteiro descartava todo resumo real da Nora
# Weekly, e o Zinom recebia um stub. O frontmatter já marca presença como
# não verificada; o prompt já proíbe atribuir voz pela agenda.
_PRESENCE = re.compile(r"\b(particip\w*|presen\w*|comparec\w*|attend\w*|joined|spoke|falou|falaram|disse|disseram)\b")


def _ground_gold(data):
    """Sem vínculo de voz comprovado, presença ou fala não vira fato durável.

    Filtra cada item inteiro pelo termo de presença, inclusive decisões/tarefas
    com nomes em campos alternativos. O filtro por nome da agenda saiu: cada
    fato agora cita a passagem e pode ser apagado por origem no Zinom, e ele
    descartava fatos legítimos sobre convidados e empresas ("Nora").
    """
    def sem_citacao(value):
        # A passagem citada pode dizer "os participantes decidiram"; o que
        # não pode é o fato em si afirmar presença ou fala.
        return {k: v for k, v in value.items() if k != "evidencia"} if isinstance(value, dict) else value

    result = {}
    for key in ("facts", "decisions", "action_items", "people_notes"):
        values = data.get(key, [])
        result[key] = [value for value in values
                       if isinstance(value, (dict, str))
                       and not _PRESENCE.search(_grounding_text(json.dumps(sem_citacao(value), ensure_ascii=False)))] if isinstance(values, list) else []
    return result


# ---------------------------------------------------------------- Hermes
# Resumo pela Hermes Agent CLI na VPS, com as assinaturas do Bruno (Claude e
# Codex). Mesmo padrão do ASR remoto (VpsSshTranscriber._transcribe_flac): o
# prompt vai como arquivo, o job roda solto (nohup + flock) e é identificado
# pelo hash do pedido, então repetir a chamada encontra o resultado pronto em
# vez de rodar de novo. `--safe-mode -t none` é obrigatório: sem ferramentas,
# memória ou MCP, quem resume nunca escreve na memória do Bruno.
HERMES_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                   "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1"]
HERMES_POLL_SEC = 5
HERMES_JSON_INSTRUCTION = "Responda SOMENTE com o objeto JSON pedido, sem texto antes ou depois."
# A CLI imprime "session_id: ..." antes OU depois da resposta (varia por versão),
# e avisos como "Warning: Unknown toolsets: none" saem no stdout junto com ela.
_SESSION_LINE = re.compile(r"^\s*(?:session_id:|Warning:)[^\n]*\n?", re.M)
# Caminho ou coisa parecida com chave não entra em mensagem de produto.
_UNSAFE_TOKEN = re.compile(r"\S*/\S*|[A-Za-z0-9_-]{32,}")


class HermesInProgress(LlmUnavailable):
    """O job continua rodando na VPS: a retomada encontra o resultado, sem reserva."""


class _HermesJobFailed(Exception):
    """Um modelo da cadeia falhou de fato; o próximo pode tentar."""


class HermesSshLlm:
    def __init__(self, host: str, models, reasoning: str = "medium", timeout_sec: int = 900,
                 sleep=time.sleep, clock=time.monotonic):
        self.host = host
        self.models = [m for m in (models or [])
                       if isinstance(m, dict) and m.get("provider") and m.get("model")]
        self.reasoning = str(reasoning or "medium")
        self.timeout_sec = int(timeout_sec or 900)
        self.sleep = sleep
        self.clock = clock
        self.last_model: Optional[str] = None  # "anthropic/claude-opus-5"

    def _ssh(self, command: str) -> str:
        try:
            result = subprocess.run(["ssh", *HERMES_SSH_OPTS, self.host, command],
                                    capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LlmUnavailable("SSH da VPS indisponível") from exc
        if result.returncode != 0:
            raise LlmUnavailable("SSH da VPS indisponível")
        return result.stdout.strip()

    def _state(self, remote: str) -> str:
        return self._ssh(f"umask 077; mkdir -p {remote} && if test -f {remote}/result.txt; then echo DONE; "
                         f"elif test -f {remote}/exit.txt; then echo FAILED; "
                         f"elif test -f {remote}/prompt.txt; then echo RUNNING; else echo UPLOAD; fi")

    def _upload(self, prompt: str, remote: str) -> None:
        # O prompt (com a transcrição) só viaja como arquivo, nunca em argv.
        local_dir = get_state_dir() / "llm"
        local_dir.mkdir(parents=True, exist_ok=True)
        fd, local = tempfile.mkstemp(prefix=".hermes-", suffix=".txt", dir=local_dir)
        upload = f"{remote}/upload-{uuid.uuid4().hex}.txt"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(prompt)
            try:
                result = subprocess.run(["scp", *HERMES_SSH_OPTS, local, f"{self.host}:{upload}"],
                                        capture_output=True, text=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise LlmUnavailable("SSH da VPS indisponível") from exc
            if result.returncode != 0:
                raise LlmUnavailable("SSH da VPS indisponível")
        finally:
            os.unlink(local)
        self._ssh(f"chmod 600 {upload} && mv {upload} {remote}/prompt.txt")

    def _launch(self, remote: str, provider: str, model: str) -> None:
        script = (f"if test -f {remote}/result.txt || test -f {remote}/exit.txt; then exit 0; fi; "
                  f"timeout --kill-after=30s {self.timeout_sec}s hermes chat -Q --oneshot --safe-mode -t none "
                  f"--source tool --query-file {remote}/prompt.txt --provider {shlex.quote(provider)} "
                  f"-m {shlex.quote(model)} --reasoning {shlex.quote(self.reasoning)} "
                  f"> {remote}/result.part 2> {remote}/worker.log; rc=$?; "
                  f"if [ $rc -eq 0 ] && [ -s {remote}/result.part ]; then mv {remote}/result.part {remote}/result.txt; "
                  f"else echo $rc > {remote}/exit.txt; fi")
        self._ssh(f"umask 077; nohup flock -n {remote}/job.lock sh -c {shlex.quote(script)} "
                  ">/dev/null 2>&1 </dev/null &")

    def _failure(self, remote: str) -> str:
        """Motivo curto do worker.log, sem caminho nem segredo."""
        try:
            tail = self._ssh(f"cat {remote}/exit.txt 2>/dev/null; tail -n 3 {remote}/worker.log 2>/dev/null")
        except LlmUnavailable:
            return "falhou na VPS"
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        rc = lines.pop(0) if lines and lines[0].isdigit() else "?"
        detail = " | ".join(" ".join(_UNSAFE_TOKEN.sub("", line).split()) for line in lines)
        return f"saída {rc}" + (f": {detail[:160]}" if detail else "")

    @staticmethod
    def _answer(text: str) -> str:
        answer = _SESSION_LINE.sub("", text).strip()
        if not answer:
            raise _HermesJobFailed("resposta vazia")
        return answer

    def _run(self, provider: str, model: str, prompt: str) -> str:
        identity = {"provider": provider, "model": model, "reasoning": self.reasoning, "prompt": prompt}
        canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        job_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        remote = f".local/state/castanha/llm/{job_id}"
        state = self._state(remote)
        if state == "UPLOAD":
            self._upload(prompt, remote)
            state = "RUNNING"
        if state == "RUNNING":
            # O prompt pode ter sobrevivido a uma queda antes do launch ou a
            # um reboot. O flock preserva o worker vivo e retoma o órfão.
            self._launch(remote, provider, model)
        deadline = self.clock() + self.timeout_sec
        while state == "RUNNING":
            if self.clock() >= deadline:
                raise HermesInProgress("Resumo ainda em andamento na VPS; retomada automática")
            self.sleep(HERMES_POLL_SEC)
            state = self._state(remote)
        if state == "DONE":
            return self._answer(self._ssh(f"cat {remote}/result.txt"))
        if state == "FAILED":
            reason = self._failure(remote)
            # Falha transitória (529, timeout) não pode pinar este prompt para
            # sempre: limpa o job, e a próxima retomada sobe de novo.
            try:
                self._ssh(f"rm -f {remote}/exit.txt {remote}/result.part {remote}/prompt.txt")
            except LlmUnavailable:
                pass
            raise _HermesJobFailed(reason)
        raise LlmUnavailable("SSH da VPS indisponível")

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        if not self.host:
            raise LlmUnavailable("Hermes indisponível na VPS: host SSH não configurado")
        if not self.models:
            raise LlmUnavailable("Hermes indisponível na VPS: nenhum modelo configurado")
        # Sem flag de system prompt na CLI: tudo vai no único arquivo de consulta.
        prompt = f"{system_prompt.rstrip()}\n\n{user_prompt.rstrip()}\n"
        if json_mode:
            prompt += f"\n{HERMES_JSON_INSTRUCTION}\n"
        reasons: List[str] = []
        for entry in self.models:
            provider, model = entry["provider"], entry["model"]
            try:
                answer = self._run(provider, model, prompt)
            except _HermesJobFailed as exc:
                print(f"[Castanha] Hermes {provider}/{model} falhou ({exc}); tentando o próximo modelo",
                      file=sys.stderr)
                reasons.append(f"{provider}/{model}: {exc}")
                continue
            self.last_model = f"{provider}/{model}"
            return answer
        raise LlmUnavailable("Hermes indisponível na VPS: " + "; ".join(reasons))


class MeetingSummarizer:
    def __init__(self):
        cfg = load_config()
        llm_cfg = cfg.get("llm", {})
        self.provider = llm_cfg.get("provider", "groq")
        self.api_key = llm_cfg.get("api_key") or os.environ.get("GROQ_API_KEY", "")
        self.model = llm_cfg.get("model", "openai/gpt-oss-120b")
        self.fallback_provider = llm_cfg.get("fallback_provider") or ""
        self.hermes = HermesSshLlm(llm_cfg.get("hermes_ssh_host", ""), llm_cfg.get("hermes_models", []),
                                   reasoning=llm_cfg.get("hermes_reasoning", "medium"),
                                   timeout_sec=llm_cfg.get("hermes_timeout_sec", 900))
        # Quem produziu o último resumo: "hermes:<provider>/<model>" ou "groq".
        self.last_provider: Optional[str] = None

    def _call_llm(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        if self.provider == "hermes_ssh":
            try:
                answer = self.hermes.complete(system_prompt, user_prompt, json_mode)
            except HermesInProgress:
                # Job vivo na VPS: a retomada acha o resultado. Trocar por Groq
                # agora jogaria fora o resumo melhor que está quase pronto.
                raise
            except LlmUnavailable as exc:
                if self.fallback_provider != "groq" or not self.api_key:
                    raise
                print(f"[Castanha] Hermes indisponível ({exc}); usando Groq como reserva", file=sys.stderr)
                return self._call_groq(system_prompt, user_prompt, json_mode)
            self.last_provider = f"hermes:{self.hermes.last_model}"
            return answer
        if self.provider != "groq":
            # Outro provedor configurado nunca vai parar na Groq por engano.
            raise LlmUnavailable(f"provedor de LLM '{self.provider}' não suportado; só 'hermes_ssh' e 'groq'")
        if not self.api_key:
            return ""
        return self._call_groq(system_prompt, user_prompt, json_mode)

    def _call_groq(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
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
                    # Teto no corpo antes do parse: resposta sem fim da LLM
                    # não pode virar consumo de memória ilimitado.
                    res = read_json_bounded(resp)
                    content = res["choices"][0]["message"]["content"]
                    self.last_provider = "groq"
                    return content
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
                if e.code in (401, 403):
                    # Chave recusada: resumo fica pendente até a chave ser corrigida.
                    print(f"[Castanha] Chave da LLM recusada: HTTP {e.code}: {corpo[:200]}", file=sys.stderr)
                    raise LlmUnavailable(f"chave da Groq recusada (HTTP {e.code})")
                if e.code in (408, 429) or e.code >= 500:
                    # 429 aqui é quase sempre a janela de um minuto do plano
                    # gratuito: o Retry-After diz quanto falta para ela abrir.
                    # O corpo vai só ao stderr; no painel entra linguagem de produto.
                    print(f"[Castanha] LLM respondeu HTTP {e.code}: {corpo[:200]}", file=sys.stderr)
                    ultimo = ("cota da Groq esgotada (HTTP 429)" if e.code == 429
                              else f"Groq fora do ar (HTTP {e.code})")
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        espera = float(ra) if ra else espera
                    except ValueError:
                        pass
                else:
                    print(f"[Castanha] Erro na chamada LLM: HTTP {e.code}: {corpo[:200]}", file=sys.stderr)
                    return ""
            except OSError as e:
                print(f"[Castanha] Sem rede para a LLM: {e}", file=sys.stderr)
                ultimo = "sem rede para a Groq"
            except (KeyError, IndexError, ValueError) as e:
                print(f"[Castanha] Resposta inesperada da LLM: {e}", file=sys.stderr)
                return ""
            if tentativa < LLM_ATTEMPTS:
                time.sleep(min(espera, LLM_MAX_WAIT_SEC))
        print(f"[Castanha] Erro na chamada LLM depois de {LLM_ATTEMPTS} tentativas: {ultimo}", file=sys.stderr)
        raise LlmUnavailable(f"{ultimo}, {LLM_ATTEMPTS} tentativas")

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
Convidados (presença não confirmada): {attendees_str}
"""
        prompt = f"""{cabecalho}
Transcrição Bruta:
{raw_transcript}
"""

        audio_status = metadata.get("audio_status", "ok")
        audio_aviso = metadata.get("audio_diagnostico", "")

        # Hermes tem contexto grande: sem partes, e a transcrição íntegra é
        # anexada aqui em vez de pedir ao modelo que a reescreva.
        sem_transcricao = self.provider == "hermes_ssh"
        llm_output = ""
        narrativa = ""  # só o que a LLM escreveu, sem a transcrição anexada
        if raw_transcript.strip():
            try:
                narrativa = llm_output = self._call_llm(
                    SILVER_NOTES_SYSTEM_PROMPT if sem_transcricao else SILVER_SYSTEM_PROMPT, prompt)
                if sem_transcricao and narrativa.strip():
                    llm_output = narrativa.strip() + f"\n\n## 📝 Transcrição Bruta\n{raw_transcript}\n"
            except LlmTooLarge as e:
                print(f"[Castanha] Transcrição grande para uma chamada ({e}); resumindo em partes...", file=sys.stderr)
                narrativa = llm_output = self._silver_em_partes(title, cabecalho, raw_transcript, self._tamanho_da_parte(prompt, e))

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
Sem resumo: {motivo}. Gravada em {date_str}. Convidados (presença não confirmada): {attendees_str}.

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
            frontmatter += f'  - name: {json.dumps(name, ensure_ascii=False)}\n    email: {json.dumps(email, ensure_ascii=False)}\n'
            # RSVP é resposta ao convite, não comparecimento observado.
            rsvp = att.get("response") or att.get("response_status") or att.get("responseStatus") or "unknown"
            frontmatter += (f'    evidence: "calendar_invitation"\n    rsvp: {json.dumps(rsvp, ensure_ascii=False)}\n'
                            '    presence: "unverified"\n    speech: "unverified"\n')
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
{raw_transcript if self.provider == "hermes_ssh" else raw_transcript[:4000]}
"""

        llm_output = ""
        if raw_transcript.strip():
            try:
                llm_output = self._call_llm(GOLD_SYSTEM_PROMPT, prompt, json_mode=True)
                if not llm_output:
                    # O modo JSON da Groq recusa a resposta inteira quando o
                    # modelo tropeça ("json_validate_failed", visto em 05/09
                    # numa reunião de 2h). Sem o modo, o texto vem e o JSON
                    # sai dele.
                    llm_output = self._call_llm(GOLD_SYSTEM_PROMPT, prompt, json_mode=False)
            except LlmTooLarge as e:
                print(f"[Castanha] Fatos: mensagem grande demais ({e}). Fica o fallback estruturado.", file=sys.stderr)
        dados = _extrair_json(llm_output)
        if isinstance(dados, dict):
            return _ground_gold(dados)

        # Convite não prova presença. Sem extração, não há fatos duráveis.
        return {"facts": [], "decisions": [], "action_items": [], "people_notes": []}
