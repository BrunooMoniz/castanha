"""Estado projetado na leitura: última entrega cuja pasta Bronze sumiu não é mais a última."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.state import StateManager

ROOT = Path(__file__).resolve().parents[1]


class TestLastResultOrfao(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.env = patch.dict("os.environ", {"XDG_STATE_HOME": str(self.temp / "state"),
                                             "XDG_CONFIG_HOME": str(self.temp / "config")})
        self.env.start()
        self.bronze = self.temp / "meetings" / "bronze" / "reuniao-teste"

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _grava(self, last_result):
        mgr = StateManager()
        mgr.state_file.write_text(json.dumps({"status": "idle", "last_result": last_result}), encoding="utf-8")
        return mgr

    def test_pasta_na_lixeira_some_da_leitura_sem_reescrever_o_arquivo(self):
        mgr = self._grava({"slug": "reuniao-teste", "title": "Teste", "bronze_dir": str(self.bronze)})
        antes = mgr.state_file.read_bytes()
        self.assertIsNone(mgr.read()["last_result"])
        self.assertEqual(mgr.state_file.read_bytes(), antes)

    def test_pasta_existente_continua_sendo_a_ultima_entrega(self):
        self.bronze.mkdir(parents=True)
        mgr = self._grava({"slug": "reuniao-teste", "title": "Teste", "bronze_dir": str(self.bronze)})
        self.assertEqual(mgr.read()["last_result"]["slug"], "reuniao-teste")

    def test_sem_bronze_dir_fica_como_esta(self):
        mgr = self._grava({"slug": "reuniao-teste", "title": "Teste"})
        self.assertEqual(mgr.read()["last_result"]["title"], "Teste")

    def test_a_proxima_escrita_persiste_o_none(self):
        mgr = self._grava({"slug": "reuniao-teste", "title": "Teste", "bronze_dir": str(self.bronze)})
        mgr.write({"elapsed_seconds": 0})
        self.assertIsNone(json.loads(mgr.state_file.read_text(encoding="utf-8"))["last_result"])

    def test_status_json_do_cli_nao_mostra_a_entrega_orfa(self):
        mgr = self._grava({"slug": "reuniao-teste", "title": "Teste", "bronze_dir": str(self.bronze),
                           "zinom": {"status": "ok"}})
        antes = mgr.state_file.read_bytes()
        env = {**os.environ, "XDG_STATE_HOME": str(self.temp / "state"), "XDG_CONFIG_HOME": str(self.temp / "config")}
        res = subprocess.run([sys.executable, "-B", str(ROOT / "bin/castanha"), "status", "--json"],
                             env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIsNone(json.loads(res.stdout)["last_result"])
        self.assertEqual(mgr.state_file.read_bytes(), antes)


if __name__ == "__main__":
    unittest.main()
