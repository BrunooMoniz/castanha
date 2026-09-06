"""Gate focado da ponte, com connect/bind proibidos e fixtures sintéticas.

Executar: python3 -B -m tests.verify_bronze_f5b4
O teste HTTP existente exige socket e fica fora desta seleção sem rede.
"""
import socket
import unittest
from unittest.mock import patch


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_bronze_sync", "tests.test_bronze_ingest",
        "tests.test_sync", "tests.test_zinom_adapter",
    ])
    excluded = "tests.test_bronze_ingest.TestBronzeUpload.test_real_http_lost_response_reuses_one_server_job"
    selected = [test for test in flatten(suite) if test.id() != excluded]
    print("Não executado neste gate sem rede: " + excluded, flush=True)
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Rede proibida")), \
         patch.object(socket.socket, "bind", side_effect=AssertionError("Rede proibida")):
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
