"""A cota do OpenClaw é sanitizada NA VPS: rótulo de perfil nunca volta."""
import json
import subprocess
import unittest

from castanha.openclaw_quota import quota_state, remote_command

PREFIXO = "export HOME=/root; set -a; . /root/.openclaw/gateway.env; set +a; "


def _status(oauth=None, providers=None, missing=None):
    return {"auth": {"missingProvidersInUse": missing or [],
                     "providers": providers if providers is not None else [],
                     "oauth": {"providers": oauth or []}}}


class TestQuotaState(unittest.TestCase):
    def test_provedor_autenticado_nao_bloqueia(self):
        for estado in ("ok", "expiring", "static"):
            self.assertFalse(quota_state(_status([{"provider": "anthropic", "status": estado}]),
                                         "anthropic")["blocked"], estado)

    def test_provedor_expirado_bloqueia(self):
        self.assertTrue(quota_state(_status([{"provider": "anthropic", "status": "expired"}]),
                                    "anthropic")["blocked"])

    def test_um_perfil_bom_entre_ruins_libera(self):
        self.assertFalse(quota_state(_status([{"provider": "anthropic", "status": "expired"},
                                              {"provider": "anthropic", "status": "ok"}]),
                                     "anthropic")["blocked"])

    def test_provedor_so_com_api_key_conta_como_usavel(self):
        self.assertFalse(quota_state(_status([], [{"provider": "zai"}]), "zai")["blocked"])

    def test_provedor_desconhecido_bloqueia(self):
        self.assertTrue(quota_state(_status([{"provider": "openai", "status": "ok"}]),
                                    "anthropic")["blocked"])

    def test_provedor_em_falta_declarada_bloqueia(self):
        status = _status([{"provider": "anthropic", "status": "ok"}], missing=["anthropic"])
        self.assertTrue(quota_state(status, "anthropic")["blocked"])

    def test_runtime_codex_indisponivel_nao_bloqueia_o_anthropic(self):
        status = _status([{"provider": "anthropic", "status": "ok"}])
        status["auth"]["runtimeAuthRoutes"] = [{"provider": "openai", "runtime": "codex",
                                                "status": "unavailable"}]
        self.assertFalse(quota_state(status, "anthropic")["blocked"])

    def test_saida_estranha_bloqueia_em_vez_de_adivinhar(self):
        for ruim in (None, [], {}, {"auth": "?"}):
            self.assertTrue(quota_state(ruim, "anthropic")["blocked"], ruim)


class TestRemoteCommand(unittest.TestCase):
    def comando(self, provider="anthropic"):
        return remote_command(provider, PREFIXO)

    def test_carrega_o_ambiente_do_gateway_e_pede_json(self):
        cmd = self.comando()
        self.assertIn(". /root/.openclaw/gateway.env", cmd)
        self.assertIn("openclaw models status --agent main --json", cmd)

    def test_roda_de_verdade_e_devolve_so_o_estado(self):
        status = _status([{"provider": "anthropic", "status": "ok"}])
        status["auth"]["oauth"]["profiles"] = [{"label": "zai:fixture=abcd1234...wxyz9876"}]
        script = self.comando().split("| ", 1)[1]
        saida = subprocess.run(["sh", "-c", script], input=json.dumps(status),
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(saida.returncode, 0, saida.stderr)
        self.assertEqual(json.loads(saida.stdout), {"blocked": False})
        self.assertNotIn("wxyz9876", saida.stdout)

    def test_saida_nao_json_da_indisponivel_em_vez_de_vazar(self):
        script = self.comando().split("| ", 1)[1]
        saida = subprocess.run(["sh", "-c", script], input="erro: token inválido abc123",
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(saida.stdout), {"unavailable": True})
        self.assertNotIn("abc123", saida.stdout)


if __name__ == "__main__":
    unittest.main()
