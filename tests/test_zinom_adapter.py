"""Transporte MCP do Zinom.

Cobre o 406 original e, depois dele, os defeitos que a revisão cruzada de
04/09/2026 achou: erro escalar, falha sem isError, e casamento da resposta
pela posição em vez do id.
"""

import json
import unittest
from unittest.mock import patch

from castanha.zinom_adapter import (
    ZinomAdapter,
    ZinomError,
    ZinomMcpClient,
    ZinomSessionError,
    error_message,
    is_fato_util,
    parse_mcp_messages,
    parse_mcp_response,
)

SSE_INIT = (
    "event: message\n"
    'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"zinom"}}}\n\n'
)


class TestParse(unittest.TestCase):
    def test_json_puro(self):
        m = parse_mcp_messages('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}', "application/json")
        self.assertEqual(m[0]["result"]["ok"], True)

    def test_sse(self):
        m = parse_mcp_messages(SSE_INIT, "text/event-stream")
        self.assertEqual(m[0]["result"]["serverInfo"]["name"], "zinom")

    def test_sse_sem_content_type_declarado(self):
        self.assertEqual(parse_mcp_messages(SSE_INIT, "")[0]["id"], 1)

    def test_sse_devolve_todos_os_eventos_em_ordem(self):
        raw = (
            'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"passo":1}}\n\n'
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
        )
        m = parse_mcp_messages(raw, "text/event-stream")
        self.assertEqual(len(m), 2)
        self.assertEqual(m[0]["id"], 7)

    def test_evento_com_varias_linhas_data_concatena(self):
        # A spec do SSE manda juntar as linhas data: de um mesmo evento.
        raw = 'event: message\ndata: {"jsonrpc":"2.0","id":3,\ndata: "result":{"ok":true}}\n\n'
        m = parse_mcp_messages(raw, "text/event-stream")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["id"], 3)

    def test_corpo_vazio_do_202(self):
        self.assertEqual(parse_mcp_messages("", "application/json"), [])
        self.assertIsNone(parse_mcp_response("", "application/json"))


class TestErrorMessage(unittest.TestCase):
    def test_objeto_jsonrpc(self):
        self.assertEqual(error_message({"code": -32000, "message": "Not Acceptable"}), "Not Acceptable")

    def test_escalar(self):
        # O hub responde 401 como {"error": "Unauthorized"}: string, não objeto.
        self.assertEqual(error_message("Unauthorized"), "Unauthorized")

    def test_objeto_sem_message(self):
        self.assertEqual(error_message({"error": "Session not found"}), "Session not found")


class FakeTransport:
    """Grava as requisições e devolve respostas roteirizadas."""

    def __init__(self, remember_payload=None, ruido_no_fim=False):
        self.requests = []
        self.headers_seen = []
        self.remember_payload = remember_payload
        self.ruido_no_fim = ruido_no_fim
        self.conexoes = 0

    def __call__(self, client, payload):
        self.requests.append(payload)
        self.headers_seen.append(client._headers())
        method = payload.get("method")

        if method == "initialize":
            self.conexoes += 1
            client.session_id = f"sessao-{self.conexoes}"
            return [{"jsonrpc": "2.0", "id": payload["id"], "result": {"serverInfo": {"name": "zinom"}}}]
        if method == "notifications/initialized":
            return []
        if method == "tools/call":
            name = payload["params"]["name"]
            corpo = self.remember_payload if (name == "remember" and self.remember_payload is not None) \
                else {"ok": True, "source_id": f"conversation:{name}", "id": f"conversation:{name}"}
            msgs = [{
                "jsonrpc": "2.0", "id": payload["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(corpo)}]},
            }]
            if self.ruido_no_fim:
                # Notificação chegando DEPOIS da resposta: o "último ganha" antigo
                # engolia isto como se fosse o resultado.
                msgs.append({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
            return msgs
        raise AssertionError(f"método inesperado: {method}")


def make_client(transport):
    client = ZinomMcpClient("https://zinom.test/mcp", "token-de-teste")
    client._post = lambda payload: transport(client, payload)
    return client


class TestZinomMcpClient(unittest.TestCase):
    def test_accept_exigido_pelo_endpoint(self):
        accept = ZinomMcpClient("https://zinom.test/mcp", "t")._headers()["Accept"]
        self.assertIn("application/json", accept)
        self.assertIn("text/event-stream", accept)

    def test_session_id_so_entra_depois_do_initialize(self):
        client = make_client(FakeTransport())
        self.assertNotIn("mcp-session-id", client._headers())
        client.connect()
        self.assertEqual(client._headers()["mcp-session-id"], "sessao-1")
        self.assertEqual(client._headers()["MCP-Protocol-Version"], "2025-06-18")

    def test_handshake_completo_antes_do_tools_call(self):
        t = FakeTransport()
        c = make_client(t)
        c.connect()
        c.call_tool("remember", {"text": "oi"})
        self.assertEqual(
            [r.get("method") for r in t.requests],
            ["initialize", "notifications/initialized", "tools/call"],
        )

    def test_resposta_e_casada_pelo_id_nao_pela_posicao(self):
        t = FakeTransport(ruido_no_fim=True)
        c = make_client(t)
        c.connect()
        res = c.call_tool("remember", {"text": "oi"})
        self.assertIn("content", res)

    def test_resposta_sem_o_id_pedido_e_erro(self):
        c = ZinomMcpClient("https://zinom.test/mcp", "t")
        c._post = lambda payload: [{"jsonrpc": "2.0", "method": "notifications/message"}]
        with self.assertRaises(ZinomError):
            c._rpc("tools/call", {"name": "x", "arguments": {}})

    def test_is_error_vira_excecao(self):
        c = ZinomMcpClient("https://zinom.test/mcp", "t")
        c._post = lambda p: [{"jsonrpc": "2.0", "id": p["id"],
                              "result": {"isError": True, "content": [{"type": "text", "text": "token inválido"}]}}]
        with self.assertRaises(ZinomError):
            c.call_tool("remember", {"text": "oi"})

    def test_ok_false_sem_is_error_tambem_vira_excecao(self):
        # O remember do hub sinaliza quota estourada assim, e sem isError.
        c = ZinomMcpClient("https://zinom.test/mcp", "t")
        corpo = json.dumps({"ok": False, "error": "quota_exceeded", "message": "Cota mensal estourada"})
        c._post = lambda p: [{"jsonrpc": "2.0", "id": p["id"],
                              "result": {"content": [{"type": "text", "text": corpo}]}}]
        with self.assertRaises(ZinomError) as ctx:
            c.call_tool("remember", {"text": "oi"})
        self.assertIn("Cota mensal estourada", str(ctx.exception))

    def test_erro_jsonrpc_escalar_nao_estoura_attribute_error(self):
        c = ZinomMcpClient("https://zinom.test/mcp", "t")
        c._post = lambda p: [{"jsonrpc": "2.0", "id": p["id"], "error": "Unauthorized"}]
        with self.assertRaises(ZinomError) as ctx:
            c.connect()
        self.assertIn("Unauthorized", str(ctx.exception))

    def test_sessao_expirada_reabre_e_repete(self):
        estado = {"primeira": True}
        t = FakeTransport()
        c = make_client(t)
        c.connect()
        original = c._rpc

        def rpc(method, params=None):
            if method == "tools/call" and estado["primeira"]:
                estado["primeira"] = False
                raise ZinomSessionError("HTTP 404: Session not found or expired.")
            return original(method, params)

        c._rpc = rpc
        c.call_tool("remember", {"text": "oi"})
        self.assertEqual(t.conexoes, 2)


class TestIngestMeeting(unittest.TestCase):
    def _adapter(self):
        a = ZinomAdapter()
        a.enabled, a.token, a.endpoint = True, "token-de-teste", "https://zinom.test/mcp"
        return a

    def _run(self, metadata, gold=None, transport=None):
        t = transport or FakeTransport()
        with patch("castanha.zinom_adapter.ZinomMcpClient", side_effect=lambda *a, **k: make_client(t)):
            res = self._adapter().ingest_meeting(metadata, "# Notas", gold or {"facts": []})
        return res, t

    def test_remember_sem_campo_source(self):
        # O schema do remember é additionalProperties: false; 'source' era recusado.
        res, t = self._run({"title": "Reunião", "recorded_at": "2026-09-04T10:42:00"})
        call = next(r for r in t.requests if r.get("method") == "tools/call")
        self.assertEqual(set(call["params"]["arguments"]), {"text", "title", "tags"})
        self.assertEqual(res["remember"]["id"], "conversation:remember")

    def test_id_da_nota_vem_do_source_id(self):
        # A chave documentada é source_id; o hub manda as duas.
        t = FakeTransport(remember_payload={"ok": True, "source_id": "conversation:abc"})
        res, _ = self._run({"title": "R", "recorded_at": "2026-09-04"}, transport=t)
        self.assertEqual(res["remember"]["id"], "conversation:abc")

    def test_remember_que_falhou_nao_vira_sucesso(self):
        t = FakeTransport(remember_payload={"ok": False, "message": "Cota estourada"})
        res, _ = self._run({"title": "R", "recorded_at": "2026-09-04"}, transport=t)
        self.assertEqual(res["status"], "error")
        self.assertIsNone(res["remember"])
        self.assertTrue(any("Cota estourada" in e for e in res["errors"]))

    def test_fatos_viram_brain_fact(self):
        gold = {"facts": [
            {"subject": "Bruno", "predicate": "testou", "object": "Castanha"},
            {"subject": "", "predicate": "x", "object": "y"},
        ]}
        res, t = self._run({"title": "R", "recorded_at": "2026-09-04"}, gold)
        nomes = [r["params"]["name"] for r in t.requests if r.get("method") == "tools/call"]
        self.assertEqual(nomes, ["remember", "brain_fact"])
        self.assertEqual(res["facts_ingested"], 1)

    def test_gravacao_muda_nao_entra_no_cerebro(self):
        res, t = self._run({"title": "R", "recorded_at": "2026-09-04", "audio_status": "sem_audio"})
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(t.requests, [])

    def test_transcricao_falha_nao_entra_no_cerebro(self):
        res, t = self._run({"title": "R", "recorded_at": "2026-09-04", "transcription_provider": "failed"})
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(t.requests, [])

    def test_mic_mudo_ainda_entra(self):
        res, t = self._run({"title": "R", "recorded_at": "2026-09-04", "audio_status": "mic_mudo"})
        self.assertEqual(res["status"], "ok")
        self.assertTrue(t.requests)


class TestFiltroDeFatos(unittest.TestCase):
    """O que o Gold extrai é gravado para sempre e supersede o anterior."""

    def test_aceita_fato_real(self):
        self.assertTrue(is_fato_util(
            {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"}))

    def test_aceita_fato_sobre_o_projeto_castanha(self):
        # O projeto é sujeito legítimo; o que se barra é o fato sobre a gravação.
        self.assertTrue(is_fato_util(
            {"subject": "Castanha", "predicate": "usa", "object": "captura PipeWire em dois canais"}))

    def test_descarta_objeto_booleano_de_verdade(self):
        # json_mode devolve o booleano JSON, não a string "true".
        self.assertFalse(is_fato_util(
            {"subject": "Teste", "predicate": "deu certo", "object": True}))
        self.assertFalse(is_fato_util(
            {"subject": "Teste", "predicate": "deu certo", "object": False}))

    def test_descarta_objeto_booleano_em_texto(self):
        for obj in ("sim", "true", "False", "não", "n/a"):
            self.assertFalse(is_fato_util(
                {"subject": "Teste de gravação", "predicate": "deu certo", "object": obj}), obj)

    def test_nao_estoura_com_tipo_estranho(self):
        for fact in ({"subject": 42, "predicate": None, "object": []}, {"subject": None}, "não é dict", None):
            self.assertFalse(is_fato_util(fact))

    def test_numero_como_objeto_e_fato_valido(self):
        self.assertTrue(is_fato_util({"subject": "Bruno Moniz", "predicate": "idade", "object": 25}))

    def test_descarta_ruido_de_instrumentacao(self):
        for subj in ("Microfone", "Microfone Dell", "Teste da gravação", "Transcrição"):
            self.assertFalse(is_fato_util(
                {"subject": subj, "predicate": "estava", "object": "mudo no teclado"}), subj)

    def test_descarta_trio_incompleto(self):
        self.assertFalse(is_fato_util({"subject": "Bruno", "predicate": "", "object": "x"}))

    def test_fatos_descartados_nao_chegam_no_zinom(self):
        t = FakeTransport()
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token, adapter.endpoint = True, "t", "https://zinom.test/mcp"
        gold = {"facts": [
            {"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"},
            {"subject": "Microfone", "predicate": "estava mutado", "object": True},
        ]}
        with patch("castanha.zinom_adapter.ZinomMcpClient", side_effect=lambda *a, **k: make_client(t)):
            res = adapter.ingest_meeting({"title": "R", "recorded_at": "2026-09-04"}, "# Notas", gold)
        self.assertEqual(res["facts_ingested"], 1)
        self.assertEqual(len(res["facts_descartados"]), 1)
        enviados = [r["params"]["arguments"] for r in t.requests
                    if r.get("method") == "tools/call" and r["params"]["name"] == "brain_fact"]
        self.assertEqual(enviados, [{"subject": "Bruno Moniz", "predicate": "cofundador de", "object": "Nora Finance"}])


if __name__ == "__main__":
    unittest.main()
