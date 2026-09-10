"""Os dois bloqueios de segurança da revisão do marketplace do Omarchy.

1. `~/.config/castanha` e `config.json` guardam credenciais (Groq, Deepgram,
   LLM, VPS, Zinom): o diretório tem que ser 0700, o arquivo 0600, a escrita
   atômica, e um symlink plantado no caminho tem que ser recusado em vez de
   seguido.
2. Corpo de resposta HTTP tem que ser limitado ANTES do parse: um endpoint
   remoto não escolhe quanta memória o Castanha consome.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.secure_io import (
    DIR_MODE,
    FILE_MODE,
    InsecureConfigError,
    MAX_HTTP_BODY_BYTES,
    MAX_HTTP_ERROR_BYTES,
    ResponseTooLarge,
    ensure_private_dir,
    read_bounded,
    read_json_bounded,
    read_private_json,
    write_private_json,
)

REPO = Path(__file__).resolve().parent.parent


class _Resposta:
    """Resposta que entrega `payload` em pedaços, como um socket real."""

    def __init__(self, payload: bytes, content_length=None, chunk=8192):
        self._buf = io.BytesIO(payload)
        self._chunk = chunk
        self.headers = {}
        if content_length is not None:
            self.headers = {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def read(self, n=None):
        # Sem n, devolveria tudo: é exatamente o padrão que o teto substitui.
        return self._buf.read(self._chunk if n is None else min(n, self._chunk))


class _RespostaInfinita:
    """Servidor hostil: nunca termina o corpo."""

    def __init__(self):
        self.headers = {}
        self.entregue = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def read(self, n=None):
        pedaco = 65536 if n is None else n
        self.entregue += pedaco
        # Se o leitor não impuser teto, isto só para quando a memória acaba.
        if self.entregue > 512 * 1024 * 1024:
            raise AssertionError("leitura sem teto: consumiu 512 MB")
        return b"x" * pedaco


def _erro_http(code=429, msg="Too Many Requests", headers=None, fp=None):
    import urllib.error
    h = headers if headers is not None else {}
    stream = fp if fp is not None else io.BytesIO(b"")
    return urllib.error.HTTPError("https://api.test/v1", code, msg, h, stream)


class TestDiretorioPrivado(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_cria_diretorio_0700_mesmo_sob_umask_frouxo(self):
        alvo = self.temp / "castanha"
        umask_antigo = os.umask(0o022)
        try:
            ensure_private_dir(alvo)
        finally:
            os.umask(umask_antigo)
        self.assertEqual(stat.S_IMODE(alvo.lstat().st_mode), DIR_MODE)

    def test_corrige_diretorio_preexistente_frouxo(self):
        alvo = self.temp / "castanha"
        alvo.mkdir(mode=0o755)
        ensure_private_dir(alvo)
        self.assertEqual(stat.S_IMODE(alvo.lstat().st_mode), DIR_MODE)

    def test_symlink_no_lugar_do_diretorio_e_recusado(self):
        fora = self.temp / "fora"
        fora.mkdir()
        link = self.temp / "castanha"
        link.symlink_to(fora, target_is_directory=True)
        with self.assertRaises(InsecureConfigError):
            ensure_private_dir(link)


class TestCredencialEmDisco(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.cfg = self.temp / "castanha" / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_escreve_0600_em_diretorio_0700(self):
        umask_antigo = os.umask(0o022)
        try:
            write_private_json(self.cfg, {"zinom": {"token": "segredo"}})
        finally:
            os.umask(umask_antigo)
        self.assertEqual(stat.S_IMODE(self.cfg.lstat().st_mode), FILE_MODE)
        self.assertEqual(stat.S_IMODE(self.cfg.parent.lstat().st_mode), DIR_MODE)

    def test_ida_e_volta_preserva_o_conteudo(self):
        dados = {"transcription": {"groq_api_key": "gsk-x"}, "lista": [1, 2]}
        write_private_json(self.cfg, dados)
        self.assertEqual(read_private_json(self.cfg), dados)

    def test_nao_deixa_temporario_para_tras(self):
        write_private_json(self.cfg, {"a": 1})
        write_private_json(self.cfg, {"a": 2})
        sobras = [p.name for p in self.cfg.parent.iterdir() if p.name != "config.json"]
        self.assertEqual(sobras, [], f"temporário vazado: {sobras}")

    def test_reescrever_fecha_modo_frouxo_preexistente(self):
        ensure_private_dir(self.cfg.parent)
        self.cfg.write_text("{}", encoding="utf-8")
        os.chmod(self.cfg, 0o644)
        write_private_json(self.cfg, {"zinom": {"token": "t"}})
        self.assertEqual(stat.S_IMODE(self.cfg.lstat().st_mode), FILE_MODE)

    def test_ler_arquivo_0644_fecha_o_modo(self):
        ensure_private_dir(self.cfg.parent)
        self.cfg.write_text(json.dumps({"a": 1}), encoding="utf-8")
        os.chmod(self.cfg, 0o644)
        self.assertEqual(read_private_json(self.cfg), {"a": 1})
        self.assertEqual(stat.S_IMODE(self.cfg.lstat().st_mode), FILE_MODE)

    def test_symlink_no_lugar_do_config_e_recusado_na_leitura(self):
        ensure_private_dir(self.cfg.parent)
        vitima = self.temp / "outro.json"
        vitima.write_text(json.dumps({"roubado": True}), encoding="utf-8")
        self.cfg.symlink_to(vitima)
        with self.assertRaises(InsecureConfigError):
            read_private_json(self.cfg)

    def test_symlink_no_lugar_do_config_nao_e_seguido_na_escrita(self):
        """O alvo do symlink não pode receber as credenciais."""
        ensure_private_dir(self.cfg.parent)
        vitima = self.temp / "vitima.json"
        vitima.write_text("original", encoding="utf-8")
        self.cfg.symlink_to(vitima)

        write_private_json(self.cfg, {"zinom": {"token": "segredo"}})

        # O rename atômico substitui o próprio symlink por um arquivo comum.
        self.assertEqual(vitima.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.cfg.is_symlink())
        self.assertEqual(stat.S_IMODE(self.cfg.lstat().st_mode), FILE_MODE)

    def test_arquivo_ausente_devolve_none(self):
        self.assertIsNone(read_private_json(self.temp / "nao-existe.json"))

    def test_arquivo_gigante_e_recusado(self):
        ensure_private_dir(self.cfg.parent)
        self.cfg.write_text("[" + "0," * 5000 + "0]", encoding="utf-8")
        os.chmod(self.cfg, FILE_MODE)
        with self.assertRaises(InsecureConfigError):
            read_private_json(self.cfg, max_bytes=128)


class TestConfigUsaEscritaSegura(unittest.TestCase):
    """load_config/save_config passam pelo caminho endurecido."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_save_config_publica_0600(self):
        from castanha.config import get_config_file, load_config, save_config

        umask_antigo = os.umask(0o022)
        try:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.temp)}):
                save_config({"zinom": {"enabled": True, "token": "segredo"}})
                destino = get_config_file()
                self.assertEqual(stat.S_IMODE(destino.lstat().st_mode), FILE_MODE)
                self.assertEqual(stat.S_IMODE(destino.parent.lstat().st_mode), DIR_MODE)
                self.assertTrue(load_config()["zinom"]["enabled"])
        finally:
            os.umask(umask_antigo)

    def test_config_como_symlink_nao_vira_fonte_de_configuracao(self):
        from castanha.config import load_config

        plantado = self.temp / "plantado.json"
        plantado.write_text(json.dumps({"zinom": {"endpoint": "https://atacante"}}),
                            encoding="utf-8")
        cfg_dir = self.temp / "castanha"
        cfg_dir.mkdir(mode=DIR_MODE, parents=True)
        (cfg_dir / "config.json").symlink_to(plantado)

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.temp)}):
            cfg = load_config()
        # Cai nos padrões, não no que o symlink aponta.
        self.assertEqual(cfg["zinom"]["endpoint"], "https://zinom.ai/mcp")


class TestCorpoRemotoLimitado(unittest.TestCase):
    def test_corpo_normal_passa_inteiro(self):
        payload = json.dumps({"text": "ok"}).encode()
        self.assertEqual(read_json_bounded(_Resposta(payload)), {"text": "ok"})

    def test_exatamente_no_limite_e_aceito(self):
        payload = b"x" * 1000
        self.assertEqual(read_bounded(_Resposta(payload), max_bytes=1000), payload)

    def test_um_byte_acima_do_limite_e_recusado(self):
        with self.assertRaises(ResponseTooLarge):
            read_bounded(_Resposta(b"x" * 1001), max_bytes=1000)

    def test_content_length_mentiroso_nao_burla_o_teto(self):
        """Header pequeno com corpo grande: o teto é de quem lê."""
        with self.assertRaises(ResponseTooLarge):
            read_bounded(_Resposta(b"x" * 5000, content_length=10), max_bytes=1000)

    def test_content_length_grande_e_recusado_sem_ler_o_corpo(self):
        resp = _Resposta(b"", content_length=10 * 1024 * 1024)
        with self.assertRaises(ResponseTooLarge):
            read_bounded(resp, max_bytes=1024)

    def test_stream_infinito_para_no_teto(self):
        resp = _RespostaInfinita()
        with self.assertRaises(ResponseTooLarge):
            read_bounded(resp, max_bytes=1024)
        # Parou perto do teto, não em centenas de MB.
        self.assertLess(resp.entregue, 1024 * 1024)

    def test_json_invalido_falha_como_json_nao_como_estouro(self):
        with self.assertRaises(json.JSONDecodeError):
            read_json_bounded(_Resposta(b"nao e json"))


class TestTranscricaoLimitaProvedores(unittest.TestCase):
    """Groq e Deepgram: os pontos citados na revisão (linhas 190-191, 345-346)."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.audio = self.temp / "audio.ogg"
        self.audio.write_bytes(b"OggS" + b"\0" * 64)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _env(self):
        return patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.temp / "config"),
            "XDG_STATE_HOME": str(self.temp / "state"),
        })

    def test_groq_recusa_resposta_sem_fim(self):
        from castanha.transcription import GroqTranscriber

        with self._env(), patch("castanha.transcription.urllib.request.urlopen",
                                lambda req, timeout=None: _RespostaInfinita()):
            with self.assertRaises(ResponseTooLarge):
                GroqTranscriber("chave")._request_groq(self.audio)

    def test_deepgram_recusa_resposta_sem_fim(self):
        from castanha.transcription import DeepgramTranscriber

        with self._env(), patch("castanha.transcription.urllib.request.urlopen",
                                lambda req, timeout=None: _RespostaInfinita()):
            with self.assertRaises(ResponseTooLarge):
                DeepgramTranscriber("chave").transcribe(self.audio)

    def test_nenhuma_leitura_remota_sem_teto_no_pacote(self):
        """Regressão: leituras cruas de respostas e erros HTTP não voltam a aparecer."""
        import ast
        import re

        pattern = re.compile(r"\b(resp|response|res|r|e|err|error|exc)\.read\s*\(")
        ofensores = []

        for py in (REPO / "castanha").glob("*.py"):
            if py.name == "secure_io.py":
                continue
            texto = py.read_text(encoding="utf-8")
            linhas = texto.splitlines()

            # 1. Checagem léxica: chamadas a .read() em variáveis de resposta ou erro
            for n, linha in enumerate(linhas, 1):
                if pattern.search(linha) and not linha.strip().startswith("#"):
                    ofensores.append(f"{py.name}:{n} (léxico): {linha.strip()}")

            # 2. Checagem sintática (AST): qualquer .read() no nome capturado em except
            tree = ast.parse(texto, filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.name:
                    err_var = node.name
                    for sub in ast.walk(node):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "read"
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == err_var
                        ):
                            ofensores.append(f"{py.name}:{sub.lineno} (AST except {err_var}.read())")

        self.assertEqual(ofensores, [], f"leitura remota sem teto encontrada: {ofensores}")


class TestHTTPErrorLimitado(unittest.TestCase):
    """Corpos de erro HTTP remotos devem ser limitados com teto seguro antes de decodificar."""

    def test_http_error_corpo_normal_lido_com_sucesso(self):
        payload = b'{"error": "rate limit"}'
        erro = _erro_http(429, "Too Many Requests", {"Content-Length": str(len(payload))}, io.BytesIO(payload))
        corpo = read_bounded(erro, max_bytes=MAX_HTTP_ERROR_BYTES)
        self.assertEqual(corpo, payload)

    def test_http_error_content_length_grande_recusado_sem_ler(self):
        inf = _RespostaInfinita()
        erro = _erro_http(500, "Internal Server Error", {"Content-Length": "10000000"}, inf)
        with self.assertRaises(ResponseTooLarge):
            read_bounded(erro, max_bytes=MAX_HTTP_ERROR_BYTES)
        # Rejeitado no pré-check sem ler nenhum byte da stream
        self.assertEqual(inf.entregue, 0)

    def test_http_error_content_length_mentiroso_recusado_no_teto(self):
        payload = b"x" * (MAX_HTTP_ERROR_BYTES + 500)
        erro = _erro_http(502, "Bad Gateway", {"Content-Length": "10"}, _Resposta(payload))
        with self.assertRaises(ResponseTooLarge):
            read_bounded(erro, max_bytes=MAX_HTTP_ERROR_BYTES)

    def test_http_error_stream_infinito_para_no_teto(self):
        inf = _RespostaInfinita()
        erro = _erro_http(500, "Internal Server Error", {}, inf)
        with self.assertRaises(ResponseTooLarge):
            read_bounded(erro, max_bytes=MAX_HTTP_ERROR_BYTES)
        # Parou em MAX_HTTP_ERROR_BYTES + 1, sem ler centenas de MB
        self.assertEqual(inf.entregue, MAX_HTTP_ERROR_BYTES + 1)

    def test_groq_retry_limita_leitura_de_http_error_infinito(self):
        from castanha.transcription import GroqTranscriber

        inf = _RespostaInfinita()
        erro = _erro_http(429, "Too Many Requests", {}, inf)

        temp = Path(tempfile.mkdtemp())
        try:
            audio = temp / "audio.ogg"
            audio.write_bytes(b"OggS" + b"\0" * 64)
            gt = GroqTranscriber("chave")
            with patch.object(gt, "_request_groq", side_effect=erro), \
                 patch("time.sleep"):
                with self.assertRaises(RuntimeError):
                    gt._request_groq_with_retry(audio, attempts=1)
            # A stream infinita de erro foi limitada em 64 KiB + 1
            self.assertEqual(inf.entregue, MAX_HTTP_ERROR_BYTES + 1)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_summarizer_limita_leitura_de_http_error_infinito(self):
        from castanha.summarizer import MeetingSummarizer

        inf = _RespostaInfinita()
        erro = _erro_http(400, "Bad Request", {}, inf)

        sm = MeetingSummarizer.__new__(MeetingSummarizer)
        sm.model = "test-model"
        sm.api_key = "test-key"
        with patch("castanha.summarizer.urllib.request.urlopen", side_effect=erro):
            res = sm._call_groq("sys", "user")
            self.assertEqual(res, "")
            self.assertEqual(inf.entregue, MAX_HTTP_ERROR_BYTES + 1)

    def test_zinom_adapter_limita_leitura_de_http_error_infinito(self):
        from castanha.zinom_adapter import _http_error_detail

        inf = _RespostaInfinita()
        erro = _erro_http(500, "Server Error", {}, inf)
        detalhe = _http_error_detail(erro)
        self.assertEqual(detalhe, "Server Error")
        self.assertEqual(inf.entregue, MAX_HTTP_ERROR_BYTES + 1)


class TestSetupEndurecido(unittest.TestCase):
    """O instalador cria a configuração já privada."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _rodar_setup(self):
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
        }
        return subprocess.run(["bash", str(REPO / "setup")], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_setup_cria_config_0600_em_dir_0700(self):
        res = self._rodar_setup()
        self.assertEqual(res.returncode, 0, res.stderr)
        cfg_dir = self.home / ".config" / "castanha"
        cfg = cfg_dir / "config.json"
        self.assertTrue(cfg.is_file(), res.stdout + res.stderr)
        self.assertEqual(stat.S_IMODE(cfg_dir.lstat().st_mode), DIR_MODE)
        self.assertEqual(stat.S_IMODE(cfg.lstat().st_mode), FILE_MODE)
        # O conteúdo é o exemplo, não um arquivo vazio.
        self.assertIn("storage", json.loads(cfg.read_text(encoding="utf-8")))

    def test_setup_fecha_modo_de_instalacao_antiga(self):
        cfg_dir = self.home / ".config" / "castanha"
        cfg_dir.mkdir(parents=True, mode=0o755)
        cfg = cfg_dir / "config.json"
        cfg.write_text(json.dumps({"zinom": {"token": "antigo"}}), encoding="utf-8")
        os.chmod(cfg, 0o644)

        res = self._rodar_setup()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(stat.S_IMODE(cfg_dir.lstat().st_mode), DIR_MODE)
        self.assertEqual(stat.S_IMODE(cfg.lstat().st_mode), FILE_MODE)
        # E não sobrescreve o que o usuário já tinha.
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["zinom"]["token"],
                         "antigo")

    def test_setup_nao_escreve_atraves_de_symlink(self):
        cfg_dir = self.home / ".config" / "castanha"
        cfg_dir.mkdir(parents=True, mode=DIR_MODE)
        vitima = self.home / "vitima.json"
        vitima.write_text("original", encoding="utf-8")
        (cfg_dir / "config.json").symlink_to(vitima)

        res = self._rodar_setup()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(vitima.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
