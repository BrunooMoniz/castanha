"""Transporte MCP do Zinom: o que rendia HTTP 406 em toda ingestão."""

import json
import unittest
from unittest.mock import patch

from castanha.zinom_adapter import (
    ZinomAdapter,
    ZinomError,
    ZinomMcpClient,
    parse_mcp_response,
)

SSE_BODY = (
    "event: message\n"
    'data: {"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"zinom"}},"jsonrpc":"2.0","id":1}\n\n'
)


class TestParseMcpResponse(unittest.TestCase):
    def test_json_puro(self):
        body = parse_mcp_response('{"result":{"ok":true}}', "application/json")
        self.assertEqual(body["result"]["ok"], True)

    def test_sse(self):
        body = parse_mcp_response(SSE_BODY, "text/event-stream")
        self.assertEqual(body["result"]["serverInfo"]["name"], "zinom")

    def test_sse_sem_content_type_declarado(self):
        body = parse_mcp_response(SSE_BODY, "")
        self.assertEqual(body["id"], 1)

    def test_sse_com_varios_eventos_pega_o_ultimo(self):
        raw = (
            'event: message\ndata: {"result":{"passo":1}}\n\n'
            'event: message\ndata: {"result":{"passo":2}}\n\n'
        )
        self.assertEqual(parse_mcp_response(raw, "text/event-stream")["result"]["passo"], 2)

    def test_corpo_vazio_do_202(self):
        self.assertIsNone(parse_mcp_response("", "application/json"))


class FakeTransport:
    """Grava as requisições e devolve respostas roteirizadas."""

    def __init__(self):
        self.requests = []
        self.headers_seen = []

    def __call__(self, client, payload):
        self.requests.append(payload)
        self.headers_seen.append(client._headers())
        method = payload.get("method")
        if method == "initialize":
            client.session_id = "sessao-de-teste"
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"serverInfo": {"name": "zinom"}}}
        if method == "notifications/initialized":
            return None
        if method == "tools/call":
            name = payload["params"]["name"]
            body = {"ok": True, "id": f"conversation:{name}"}
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(body)}]},
            }
        raise AssertionError(f"método inesperado: {method}")


def make_client(transport):
    client = ZinomMcpClient("https://zinom.test/mcp", "token-de-teste")
    client._post = lambda payload: transport(client, payload)
    return client


class TestZinomMcpClient(unittest.TestCase):
    def test_accept_exigido_pelo_endpoint(self):
        client = ZinomMcpClient("https://zinom.test/mcp", "token-de-teste")
        accept = client._headers()["Accept"]
        self.assertIn("application/json", accept)
        self.assertIn("text/event-stream", accept)

    def test_session_id_so_entra_depois_do_initialize(self):
        transport = FakeTransport()
        client = make_client(transport)
        self.assertNotIn("mcp-session-id", client._headers())
        client.connect()
        self.assertEqual(client._headers()["mcp-session-id"], "sessao-de-teste")

    def test_handshake_completo_antes_do_tools_call(self):
        transport = FakeTransport()
        client = make_client(transport)
        client.connect()
        client.call_tool("remember", {"text": "oi"})
        self.assertEqual(
            [r.get("method") for r in transport.requests],
            ["initialize", "notifications/initialized", "tools/call"],
        )

    def test_ids_nao_se_repetem(self):
        transport = FakeTransport()
        client = make_client(transport)
        client.connect()
        client.call_tool("remember", {"text": "a"})
        client.call_tool("brain_fact", {"subject": "a", "predicate": "b", "object": "c"})
        ids = [r["id"] for r in transport.requests if "id" in r]
        self.assertEqual(len(ids), len(set(ids)))

    def test_is_error_vira_excecao(self):
        client = ZinomMcpClient("https://zinom.test/mcp", "t")
        client._post = lambda payload: {
            "jsonrpc": "2.0", "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": "token inválido"}]},
        }
        with self.assertRaises(ZinomError):
            client.call_tool("remember", {"text": "oi"})

    def test_erro_jsonrpc_vira_excecao(self):
        client = ZinomMcpClient("https://zinom.test/mcp", "t")
        client._post = lambda payload: {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Not Acceptable"}}
        with self.assertRaises(ZinomError):
            client.connect()


class TestIngestMeeting(unittest.TestCase):
    def _adapter(self):
        adapter = ZinomAdapter()
        adapter.enabled = True
        adapter.token = "token-de-teste"
        adapter.endpoint = "https://zinom.test/mcp"
        return adapter

    def _run(self, metadata, gold=None):
        transport = FakeTransport()
        adapter = self._adapter()
        with patch("castanha.zinom_adapter.ZinomMcpClient", side_effect=lambda *a, **k: make_client(transport)):
            res = adapter.ingest_meeting(metadata, "# Notas", gold or {"facts": []})
        return res, transport

    def test_remember_sem_campo_source(self):
        # O schema do remember é additionalProperties: false; 'source' era recusado.
        res, transport = self._run({"title": "Reunião", "recorded_at": "2026-09-04T10:42:00"})
        call = next(r for r in transport.requests if r.get("method") == "tools/call")
        args = call["params"]["arguments"]
        self.assertEqual(set(args), {"text", "title", "tags"})
        self.assertEqual(res["remember"]["id"], "conversation:remember")

    def test_fatos_viram_brain_fact(self):
        gold = {"facts": [
            {"subject": "Bruno", "predicate": "testou", "object": "Castanha"},
            {"subject": "", "predicate": "x", "object": "y"},  # incompleto, ignorado
        ]}
        res, transport = self._run({"title": "R", "recorded_at": "2026-09-04"}, gold)
        nomes = [r["params"]["name"] for r in transport.requests if r.get("method") == "tools/call"]
        self.assertEqual(nomes, ["remember", "brain_fact"])
        self.assertEqual(res["facts_ingested"], 1)

    def test_gravacao_muda_nao_entra_no_cerebro(self):
        res, transport = self._run({"title": "R", "recorded_at": "2026-09-04", "audio_status": "sem_audio"})
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(transport.requests, [])

    def test_transcricao_falha_nao_entra_no_cerebro(self):
        res, transport = self._run({"title": "R", "recorded_at": "2026-09-04", "transcription_provider": "failed"})
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(transport.requests, [])

    def test_mic_mudo_ainda_entra(self):
        # Só o mic ficou mudo: o áudio da chamada é conteúdo legítimo.
        res, transport = self._run({"title": "R", "recorded_at": "2026-09-04", "audio_status": "mic_mudo"})
        self.assertEqual(res["status"], "ok")
        self.assertTrue(transport.requests)


if __name__ == "__main__":
    unittest.main()


class TestFiltroDeFatos(unittest.TestCase):
    """O que o Gold extrai é gravado para sempre e supersede o anterior."""

    def test_aceita_fato_real(self):
        from castanha.zinom_adapter import is_fato_util
        self.assertTrue(is_fato_util(
            {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"}))

    def test_descarta_objeto_booleano(self):
        from castanha.zinom_adapter import is_fato_util
        for obj in ("sim", "true", "False", "não", "n/a"):
            self.assertFalse(is_fato_util(
                {"subject": "Teste de gravação", "predicate": "deu certo", "object": obj}), obj)

    def test_descarta_ruido_de_instrumentacao(self):
        from castanha.zinom_adapter import is_fato_util
        self.assertFalse(is_fato_util(
            {"subject": "Microfone", "predicate": "estava mutado no teclado", "object": "Dell"}))

    def test_descarta_trio_incompleto(self):
        from castanha.zinom_adapter import is_fato_util
        self.assertFalse(is_fato_util({"subject": "Bruno", "predicate": "", "object": "x"}))

    def test_fatos_descartados_nao_chegam_no_zinom(self):
        transport = FakeTransport()
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token, adapter.endpoint = True, "t", "https://zinom.test/mcp"
        gold = {"facts": [
            {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"},
            {"subject": "Microfone", "predicate": "estava mutado", "object": "sim"},
        ]}
        with patch("castanha.zinom_adapter.ZinomMcpClient", side_effect=lambda *a, **k: make_client(transport)):
            res = adapter.ingest_meeting({"title": "R", "recorded_at": "2026-09-04"}, "# Notas", gold)
        self.assertEqual(res["facts_ingested"], 1)
        self.assertEqual(len(res["facts_descartados"]), 1)
        enviados = [r["params"]["arguments"] for r in transport.requests
                    if r.get("method") == "tools/call" and r["params"]["name"] == "brain_fact"]
        self.assertEqual(enviados, [{"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"}])
