"""Adaptador de integração com o hub Zinom (memória durável, remember e brain_fact)."""

import json
import urllib.request
from typing import Any, Dict, List
from castanha.config import load_config

class ZinomAdapter:
    def __init__(self):
        cfg = load_config()
        z_cfg = cfg.get("zinom", {})
        self.enabled = z_cfg.get("enabled", False)
        self.endpoint = z_cfg.get("endpoint", "https://zinom.ai/mcp")
        self.token = z_cfg.get("token", "")

    def ingest_meeting(self, metadata: Dict[str, Any], silver_markdown: str, gold_data: Dict[str, Any]) -> Dict[str, Any]:
        """Envia fatos e notas da reunião para a memória durável do Zinom."""
        if not self.enabled or not self.token:
            return {"status": "skipped", "reason": "Zinom integration disabled or token missing"}

        title = metadata.get("title", "Reunião")
        date_str = metadata.get("recorded_at", "")
        attendees = metadata.get("calendar_event", {}).get("attendees", [])
        attendees_str = ", ".join([a.get("name") or a.get("email", "") for a in attendees])

        note_content = (
            f"# Reunião: {title} ({date_str})\n\n"
            f"Participantes: {attendees_str}\n\n"
            f"{silver_markdown}"
        )

        results = {
            "remember": None,
            "facts_ingested": 0,
            "errors": [],
        }

        # 1. Cria nota via 'remember' no Zinom MCP
        try:
            remember_payload = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "remember",
                    "arguments": {
                        "text": note_content[:4000],
                        "source": "castanha-meeting",
                    }
                },
                "id": 1,
            }
            res = self._post_mcp(remember_payload)
            results["remember"] = res
        except Exception as e:
            results["errors"].append(f"Erro no remember: {e}")

        # 2. Injeta fatos atômicos via 'brain_fact'
        facts: List[Dict[str, str]] = gold_data.get("facts", [])
        for fact in facts:
            subj = fact.get("subject")
            pred = fact.get("predicate")
            obj = fact.get("object")
            if not (subj and pred and obj):
                continue
            try:
                fact_payload = {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": "brain_fact",
                        "arguments": {
                            "subject": subj,
                            "predicate": pred,
                            "object": obj,
                        }
                    },
                    "id": 2,
                }
                self._post_mcp(fact_payload)
                results["facts_ingested"] += 1
            except Exception as e:
                results["errors"].append(f"Erro no brain_fact para {subj}: {e}")

        return results

    def _post_mcp(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "Castanha-Omarchy/0.1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
