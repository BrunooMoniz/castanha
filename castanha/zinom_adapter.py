"""Adaptador de integração com o hub Zinom (memória durável, remember e brain_fact).

Transporte: MCP Streamable HTTP. O que o endpoint exige e a primeira versão não
fazia, o que rendia HTTP 406 em toda ingestão:

1. `Accept: application/json, text/event-stream` em toda requisição;
2. handshake `initialize` -> guardar o `mcp-session-id` -> `notifications/initialized`
   antes de qualquer `tools/call`;
3. resposta que pode chegar como SSE (`event: message` / `data: {...}`).

O que a revisão cruzada de 04/09 encontrou depois, e está consertado aqui:

- o hub responde erro com `error` ESCALAR (`{"error": "Unauthorized"}`), não só
  como objeto. Tratar como objeto estourava AttributeError, que escapava do
  `ingest_meeting` e derrubava a finalização da reunião inteira;
- o `remember` sinaliza falha (quota, erro de indexação) com `{"ok": false}`
  DENTRO do content e **sem** `isError`. Olhar só `isError` fazia o Castanha
  dizer que gravou na memória do Bruno quando não gravou nada;
- a resposta era casada pela posição (o último evento do SSE), não pelo `id` do
  JSON-RPC. Uma notificação do servidor chegando por último virava "sucesso".
"""

import json
import hashlib
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from castanha.config import load_config

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "castanha", "version": "0.1.0"}


class ZinomError(RuntimeError):
    pass


class ZinomSessionError(ZinomError):
    """Sessão morta ou expirada: dá para reabrir e tentar de novo."""


def error_message(err: Any) -> str:
    """O hub manda `error` ora como objeto JSON-RPC, ora como string solta."""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    return str(err)


def parse_mcp_messages(raw: str, content_type: str = "") -> List[Dict[str, Any]]:
    """Todos os objetos JSON-RPC da resposta, em ordem.

    Aceita JSON puro, lista de JSON, ou um fluxo SSE. Num evento SSE com várias
    linhas `data:`, o corpo é a concatenação delas, como manda a spec.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    if "text/event-stream" in content_type or raw.startswith("event:") or "\ndata:" in raw:
        eventos: List[str] = []
        buffer: List[str] = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                buffer.append(line[len("data:"):].lstrip())
            elif line.strip() == "":
                if buffer:
                    eventos.append("\n".join(buffer))
                    buffer = []
        if buffer:
            eventos.append("\n".join(buffer))

        saida = []
        for chunk in eventos:
            try:
                saida.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
        return saida

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return [data] if isinstance(data, dict) else []


def parse_mcp_response(raw: str, content_type: str = "") -> Optional[Dict[str, Any]]:
    """A última mensagem da resposta. Só para diagnóstico de erro HTTP."""
    mensagens = parse_mcp_messages(raw, content_type)
    return mensagens[-1] if mensagens else None


class ZinomMcpClient:
    """Cliente mínimo de MCP Streamable HTTP, só o necessário para o Castanha."""

    def __init__(self, endpoint: str, token: str, timeout: int = 30):
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._next_id = 0

    # -- transporte ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Castanha-Omarchy/0.1.0",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    def _post(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                session = resp.headers.get("mcp-session-id")
                if session:
                    self.session_id = session
                raw = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detalhe = _http_error_detail(e)
            if e.code in (400, 404) and "session" in detalhe.lower():
                raise ZinomSessionError(f"HTTP {e.code}: {detalhe}") from e
            raise ZinomError(f"HTTP {e.code}: {detalhe}") from e
        except Exception as e:
            raise ZinomError(str(e)) from e

        return parse_mcp_messages(raw, content_type)

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        pedido_id = self._next_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": pedido_id}
        if params is not None:
            payload["params"] = params

        mensagens = self._post(payload)
        # A resposta é a que traz o MEU id. O servidor pode intercalar
        # notificação (sem id) e log; pegar "a última" aceitava qualquer coisa.
        resposta = next((m for m in mensagens if m.get("id") == pedido_id), None)
        if resposta is None:
            raise ZinomError(f"O Zinom não respondeu ao {method} (id {pedido_id})")
        if "error" in resposta:
            raise ZinomError(f"{method}: {error_message(resposta['error'])}")
        result = resposta.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    # -- protocolo ----------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        self.session_id = None
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        if not self.session_id:
            raise ZinomError("O Zinom não devolveu mcp-session-id no initialize")
        self._notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: Dict[str, Any], _retry: bool = True) -> Dict[str, Any]:
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        except ZinomSessionError:
            if not _retry:
                raise
            self.connect()
            return self.call_tool(name, arguments, _retry=False)

        if result.get("isError"):
            raise ZinomError(f"{name} devolveu erro: {tool_text(result)[:300]}")

        # O hub reporta falha de negócio dentro do content, sem isError.
        payload = tool_json(result)
        if payload.get("ok") is False:
            motivo = payload.get("message") or payload.get("error") or "falhou sem dizer por quê"
            raise ZinomError(f"{name}: {motivo}")

        return result


def tool_text(result: Dict[str, Any]) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
    return " ".join(p for p in parts if p)


def tool_json(result: Dict[str, Any]) -> Dict[str, Any]:
    """As tools do Zinom devolvem o payload como JSON dentro do texto do content."""
    try:
        data = json.loads(tool_text(result))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# Nomes antigos, ainda usados por castanha.zinom_calendar.
_tool_text = tool_text
_tool_json = tool_json


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8")
    except Exception:
        return str(e.reason or "")
    parsed = parse_mcp_response(body, e.headers.get("Content-Type", "") if e.headers else "")
    if parsed and "error" in parsed:
        return error_message(parsed["error"])
    return body[:300] or str(e.reason or "")


# Objeto booleano é sinal de trio que não é fato ("Microfone / estava mutado / sim").
_OBJETOS_INVALIDOS = {"sim", "nao", "não", "true", "false", "n/a", "nenhum", "-", ""}
# Ruído de instrumentação: a memória do Bruno não guarda o funcionamento do gravador.
# Casa por palavra contida, e não por igualdade, senão "Microfone Dell" passava.
_SUJEITOS_DE_RUIDO = (
    "microfone", "áudio", "audio", "gravação", "gravacao", "gravador",
    "transcrição", "transcricao", "whisper", "esta reunião", "esta reuniao",
)
# O projeto Castanha É sujeito legítimo ("Castanha usa captura PipeWire"), então
# ele não entra na lista acima. O que se barra é o fato SOBRE a gravação.


def _texto(valor: Any) -> str:
    """A LLM devolve booleano e número em json_mode; .strip() neles estoura."""
    if valor is None or isinstance(valor, bool):
        return str(valor).lower() if isinstance(valor, bool) else ""
    if isinstance(valor, (int, float)):
        return str(valor)
    if not isinstance(valor, str):
        return ""
    return valor.strip()


def is_fato_util(fact: Any) -> bool:
    if not isinstance(fact, dict):
        return False
    subj = _texto(fact.get("subject"))
    pred = _texto(fact.get("predicate"))
    obj = _texto(fact.get("object"))
    if not (subj and pred and obj):
        return False
    if obj.lower() in _OBJETOS_INVALIDOS:
        return False
    baixo = subj.lower()
    if any(ruido in baixo for ruido in _SUJEITOS_DE_RUIDO):
        return False
    return True


def fato_normalizado(fact: Dict[str, Any]) -> Dict[str, str]:
    return {
        "subject": _texto(fact.get("subject")),
        "predicate": _texto(fact.get("predicate")),
        "object": _texto(fact.get("object")),
    }


class ZinomAdapter:
    def __init__(self):
        cfg = load_config()
        z_cfg = cfg.get("zinom", {})
        self.enabled = z_cfg.get("enabled", False)
        self.endpoint = z_cfg.get("endpoint", "https://zinom.ai/mcp")
        self.token = z_cfg.get("token", "")

    def ingest_meeting(
        self,
        metadata: Dict[str, Any],
        silver_markdown: str,
        gold_data: Dict[str, Any],
        previous_remember_id: Optional[str] = None,
        on_remember=None,
    ) -> Dict[str, Any]:
        """Envia fatos e notas da reunião para a memória durável do Zinom."""
        # Nada de alimentar o segundo cérebro com gravação muda ou transcrição
        # falha: o que entra no Zinom é durável, e vale mais calar do que gravar lixo.
        previous = metadata.get("zinom") or {}
        if previous.get("status") == "tombstoned":
            return {"status": "tombstoned", "reason": "Nota removida no Zinom; exclusão preservada"}
        previous_remember_id = previous_remember_id or previous.get("remember_id")
        memory_ids = metadata.get("memory_recording_ids")
        providers = [metadata.get("transcription_provider")] + [
            r.get("transcription_provider") for r in metadata.get("recordings", [])
            if memory_ids is None or r.get("id") in memory_ids
        ]
        if "mock" in providers:
            return {"status": "skipped", "reason": "Transcrição simulada, proibida na memória real"}
        if metadata.get("processing_status") == "pending":
            return {"status": "pending", "reason": "Processamento da gravação pendente"}
        audio_status = metadata.get("audio_status", "ok")
        if audio_status == "sem_audio":
            return {"status": "skipped", "reason": "Gravação sem áudio, nada para lembrar"}
        if metadata.get("transcription_provider") == "failed":
            return {"status": "skipped", "reason": "Transcrição falhou, nada para lembrar"}
        if not self.enabled or not self.token:
            return {"status": "pending", "reason": "Integração com o Zinom desligada ou sem token"}

        title = metadata.get("title", "Reunião")
        date_str = metadata.get("recorded_at", "")
        attendees = (metadata.get("calendar_event") or {}).get("attendees", [])
        attendees_str = ", ".join([a.get("name") or a.get("email", "") for a in attendees])

        note_content = (
            f"# Reunião: {title} ({date_str})\n\n"
            f"Convidados do calendário (presença não confirmada): {attendees_str or 'Não identificados'}\n\n"
            f"{silver_markdown}"
        )

        source = {
            "slug": metadata.get("slug"),
            "recordings": [{"id": r.get("id") or r.get("filename"), "sha256": r.get("sha256"),
                            "provider": r.get("transcription_provider")}
                           for r in metadata.get("recordings", [])
                           if memory_ids is None or r.get("id") in memory_ids],
        }
        note_content += "\n\nOrigem Castanha: " + json.dumps(source, ensure_ascii=False)
        source["note_sha256"] = hashlib.sha256(note_content.encode("utf-8")).hexdigest()
        pending_facts = [fato_normalizado(f) for f in gold_data.get("facts", []) if is_fato_util(f)]

        results: Dict[str, Any] = {
            "status": "ok",
            "remember": None,
            "facts_ingested": 0,
            "facts_descartados": [f for f in gold_data.get("facts", []) if not is_fato_util(f)],
            "facts_status": "pending_lineage" if pending_facts else "none",
            "facts_pending": pending_facts,
            "source": source,
            "errors": [],
        }

        client = ZinomMcpClient(self.endpoint, self.token)
        try:
            client.connect()
        except ZinomError as e:
            results["status"] = "error"
            results["errors"].append(f"Falha ao conectar no Zinom: {e}")
            return results

        # 1. Nota da reunião. Reenvio EDITA a nota que já existe: rodar o
        # sync duas vezes não pode encher o cérebro de cópias da mesma reunião.
        nota = {
            "text": note_content,
            "title": f"Reunião: {title} ({date_str[:10]})",
            "tags": ["castanha", "reuniao"],
        }
        try:
            if previous_remember_id:
                res = client.call_tool("brain_update", dict(nota, id=previous_remember_id))
            else:
                res = client.call_tool("remember", nota)
            payload = tool_json(res)
            # O hub devolve as duas chaves; `source_id` é a documentada.
            if not (payload.get("source_id") or payload.get("id") or previous_remember_id):
                raise ZinomError("Resposta sem identificador durável da nota")
            results["remember"] = {
                "ok": True,
                "id": payload.get("source_id") or payload.get("id") or previous_remember_id,
                "updated": bool(previous_remember_id),
            }
            results["source"]["remember_id"] = results["remember"]["id"]
            if on_remember:
                on_remember({**results, "status": "pending", "note_status": "ok"})
        except ZinomError as e:
            if previous_remember_id and "not found" in str(e).lower():
                results["status"] = "tombstoned"
                results["reason"] = "Nota removida no Zinom; exclusão preservada"
            else:
                results["status"] = "error"
                results["errors"].append(f"Erro no remember: {e}")
            return results

        # O schema atual de brain_fact não aceita linhagem/idempotency key.
        # Fatos ficam no Bronze até existir endpoint que preserve a origem no
        # servidor. Nunca criar um fato solto que a exclusão da fonte não alcança.
        results["source"]["remember_id"] = results["remember"]["id"]
        results["note_status"] = "ok"
        if pending_facts:
            results["status"] = "pending"
            results["reason"] = "Nota entregue; fatos aguardam suporte de origem no servidor"
        return results
