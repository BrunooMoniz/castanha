"""Aceite do cérebro: falha não vira fato, perda nem ressurreição.

Testes independentes da implementação das correções. Nenhuma credencial,
gravação, serviço ou diretório pessoal é acessado.
"""

import json
import unittest
from unittest.mock import patch

from castanha.summarizer import MeetingSummarizer
from castanha.sync import meeting_needs_sync
from castanha.zinom_adapter import ZinomAdapter, ZinomError


def response(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class TestBrainIntegrityContract(unittest.TestCase):
    def setUp(self):
        self.config = patch("castanha.zinom_adapter.load_config", return_value={
            "zinom": {"enabled": True, "token": "fixture-token", "endpoint": "https://zinom.test/mcp"},
        })
        self.config.start()
        self.addCleanup(self.config.stop)
        self.adapter = ZinomAdapter()
        self.metadata = {
            "title": "Reunião sintética de aceite",
            "recorded_at": "2026-09-05T12:00:00Z",
            "audio_status": "ok",
            "transcription_provider": "groq",
        }

    def test_simulated_transcript_never_reaches_memory(self):
        self.metadata["transcription_provider"] = "mock"
        with patch("castanha.zinom_adapter.ZinomMcpClient") as client:
            client.return_value.call_tool.return_value = response({"ok": True, "source_id": "conversation:fixture"})
            result = self.adapter.ingest_meeting(self.metadata, "Uma decisão simulada.", {"facts": []})
        client.return_value.call_tool.assert_not_called()
        self.assertNotEqual(result["status"], "ok")

    def test_llm_failure_does_not_turn_invite_into_attendance(self):
        metadata = dict(self.metadata, calendar_event={"attendees": [{"name": "Convidada Teste"}]})
        with patch("castanha.summarizer.load_config", return_value={}), \
             patch.object(MeetingSummarizer, "_call_llm", return_value=""):
            result = MeetingSummarizer().generate_gold(metadata, "Resumo indisponível.", "Sem evidência de participantes.")
        self.assertEqual(result.get("facts", []), [])
        self.assertEqual(result.get("people_notes", []), [])

    def test_deleted_memory_is_not_recreated_by_sync(self):
        calls = []

        def call_tool(name, arguments):
            calls.append(name)
            if name == "brain_update":
                raise ZinomError("Memory not found")
            return response({"ok": True, "source_id": "conversation:replacement"})

        with patch("castanha.zinom_adapter.ZinomMcpClient") as client:
            client.return_value.call_tool.side_effect = call_tool
            self.adapter.ingest_meeting(self.metadata, "Nota apagada.", {"facts": []},
                                       previous_remember_id="conversation:deleted")
        self.assertNotIn("remember", calls)

    def test_long_note_cannot_report_success_after_silent_truncation(self):
        marker = "DECISAO_FINAL_QUE_PRECISA_SER_PRESERVADA"
        note = "Contexto inicial. " * 400 + marker
        submitted = []

        def call_tool(name, arguments):
            submitted.append(arguments)
            return response({"ok": True, "source_id": "conversation:fixture"})

        with patch("castanha.zinom_adapter.ZinomMcpClient") as client:
            client.return_value.call_tool.side_effect = call_tool
            result = self.adapter.ingest_meeting(self.metadata, note, {"facts": []})
        if result["status"] == "ok":
            self.assertTrue(marker in json.dumps(submitted, ensure_ascii=False),
                            "Entrega declarou sucesso sem preservar o final da nota")

    def test_missing_credentials_leave_delivery_pending(self):
        self.assertTrue(meeting_needs_sync({
            "audio_status": "ok", "transcription_provider": "groq",
            "zinom": {"status": "skipped", "reason": "Integração desligada ou sem token"},
        }))


if __name__ == "__main__":
    unittest.main()
