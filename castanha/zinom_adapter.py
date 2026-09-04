"""Adaptador de integração com o hub Zinom (memória durável, remember e brain_fact).

Transporte: MCP Streamable HTTP. Três coisas que o endpoint exige e que a
primeira versão não fazia, o que rendia HTTP 406 em toda ingestão:

1. `Accept: application/json, text/event-stream` em toda requisição;
2. handshake `initialize` -> guardar o `mcp-session-id` -> `notifications/initialized`
   antes de qualquer `tools/call`;
3. resposta pode vir como SSE (`event: message` / `data: {...}`), não como JSON puro.

Além disso o schema de `remember` é `additionalProperties: false`: o campo
`source` que era enviado antes seria recusado mesmo com os headers certos.
"""

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from castanha.config import load_config

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "castanha", "version": "0.1.0"}


class ZinomError(RuntimeError):
    pass


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
        return headers

    def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
            raise ZinomError(f"HTTP {e.code}: {_http_error_detail(e)}") from e
        except Exception as e:
            raise ZinomError(str(e)) from e

        return parse_mcp_response(raw, content_type)

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": self._next_id}
        if params is not None:
            payload["params"] = params

        body = self._post(payload)
        if body is None:
            raise ZinomError(f"Resposta vazia do Zinom em {method}")
        if "error" in body:
            err = body["error"]
            raise ZinomError(f"{method}: {err.get('message', err)}")
        return body.get("result", {})

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    # -- protocolo ----------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        if not self.session_id:
            raise ZinomError("O Zinom não devolveu mcp-session-id no initialize")
        self._notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ZinomError(f"{name} devolveu erro: {_tool_text(result)[:300]}")
        return result


def parse_mcp_response(raw: str, content_type: str = "") -> Optional[Dict[str, Any]]:
    """Aceita JSON puro ou um fluxo SSE; devolve o último objeto JSON-RPC."""
    raw = (raw or "").strip()
    if not raw:
        return None

    if "text/event-stream" in content_type or raw.startswith("event:") or "\ndata:" in raw:
        payloads = [
            line[len("data:"):].strip()
            for line in raw.splitlines()
            if line.startswith("data:")
        ]
        for chunk in reversed(payloads):
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _tool_text(result: Dict[str, Any]) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
    return " ".join(p for p in parts if p)


def _tool_json(result: Dict[str, Any]) -> Dict[str, Any]:
    """As tools do Zinom devolvem o payload como JSON dentro do texto do content."""
    try:
        data = json.loads(_tool_text(result))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8")
    except Exception:
        return e.reason or ""
    parsed = parse_mcp_response(body, e.headers.get("Content-Type", "") if e.headers else "")
    if parsed and "error" in parsed:
        return str(parsed["error"].get("message", parsed["error"]))
    return body[:300] or (e.reason or "")


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
    ) -> Dict[str, Any]:
        """Envia fatos e notas da reunião para a memória durável do Zinom."""
        if not self.enabled or not self.token:
            return {"status": "skipped", "reason": "Integração com o Zinom desligada ou sem token"}

        # Nada de alimentar o segundo cérebro com gravação muda ou transcrição falha:
        # o que entra no Zinom é durável e vale mais calar do que gravar lixo.
        audio_status = metadata.get("audio_status", "ok")
        if audio_status == "sem_audio":
            return {"status": "skipped", "reason": "Gravação sem áudio, nada para lembrar"}
        if metadata.get("transcription_provider") == "failed":
            return {"status": "skipped", "reason": "Transcrição falhou, nada para lembrar"}

        title = metadata.get("title", "Reunião")
        date_str = metadata.get("recorded_at", "")
        attendees = (metadata.get("calendar_event") or {}).get("attendees", [])
        attendees_str = ", ".join([a.get("name") or a.get("email", "") for a in attendees])

        note_content = (
            f"# Reunião: {title} ({date_str})\n\n"
            f"Participantes: {attendees_str or 'Não identificados'}\n\n"
            f"{silver_markdown}"
        )

        results: Dict[str, Any] = {
            "status": "ok",
            "remember": None,
            "facts_ingested": 0,
            "errors": [],
        }

        client = ZinomMcpClient(self.endpoint, self.token)
        try:
            client.connect()
        except ZinomError as e:
            results["status"] = "error"
            results["errors"].append(f"Falha ao conectar no Zinom: {e}")
            return results

        # 1. Nota da reunião via 'remember'
        try:
            res = client.call_tool("remember", {
                "text": note_content[:4000],
                "title": f"Reunião: {title} ({date_str[:10]})",
                "tags": ["castanha", "reuniao"],
            })
            # Guardar o id deixa a nota editável e apagável depois (brain_update/brain_delete).
            results["remember"] = {"ok": True, "id": _tool_json(res).get("id")}
        except ZinomError as e:
            results["status"] = "error"
            results["errors"].append(f"Erro no remember: {e}")

        # 2. Fatos atômicos via 'brain_fact'
        for fact in gold_data.get("facts", []):
            subj, pred, obj = fact.get("subject"), fact.get("predicate"), fact.get("object")
            if not (subj and pred and obj):
                continue
            try:
                client.call_tool("brain_fact", {
                    "subject": subj, "predicate": pred, "object": obj,
                })
                results["facts_ingested"] += 1
            except ZinomError as e:
                results["status"] = "error"
                results["errors"].append(f"Erro no brain_fact para {subj}: {e}")

        return results
