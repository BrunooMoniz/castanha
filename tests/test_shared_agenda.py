"""Agenda compartilhada, token em arquivo e conferência com o Zinom.

Com `calendar.shared_snapshot.path` o daemon lê a agenda que outro coletor da
máquina grava, sem consultar o hub; só cai na consulta direta se o arquivo
sumir ou envelhecer. Nada aqui usa rede.
"""

import datetime
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from castanha.config import load_config, redact_secrets
from castanha.secure_io import InsecureConfigError, read_secret_file
from castanha.shared_agenda import SharedSnapshot
from castanha.zinom_calendar import ZinomCalendar

TOKEN = "acct_teste_nao_vaze_isto_0123456789"


def _iso(dt):
    return dt.isoformat()


def _agora():
    return datetime.datetime.now().astimezone()


def _snapshot(eventos, gerado=None, **extra):
    gerado = gerado or _agora()
    doc = {
        "schema": 2,
        "generated_at": _iso(gerado),
        "ok": True,
        "stale": False,
        "stale_since": None,
        "calendars": [
            {"ref": "ref-trabalho", "summary": "pessoa@example.com", "email": "pessoa@example.com",
             "primary": True, "owner": True, "selected": True, "access_role": "owner"},
            {"ref": "ref-grupo", "summary": "Eventos do time", "email": "pessoa@example.com",
             "primary": False, "owner": False, "selected": True, "access_role": "writer"},
            {"ref": "ref-aniversarios", "summary": "Aniversários", "email": "pessoa@example.com",
             "primary": False, "owner": False, "selected": True, "access_role": "reader"},
        ],
        "events": eventos,
        "tasks": [],
        "stats": {"preserved": []},
    }
    doc.update(extra)
    return doc


def _evento(uid, titulo, em_horas, ref="ref-trabalho", dur_h=1, **extra):
    inicio = _agora() + datetime.timedelta(hours=em_horas)
    ev = {
        "uid": uid, "title": titulo, "all_day": False,
        "start": _iso(inicio), "end": _iso(inicio + datetime.timedelta(hours=dur_h)),
        "calendar": "pessoa@example.com", "calendar_ref": ref, "account": "pessoa@example.com",
        "meet_url": "https://meet.google.com/abc-defg-hij", "conference_provider": "Google Meet",
        "attendees": [
            {"email": "pessoa@example.com", "response": "accepted", "name": "Pessoa Um", "organizer": True},
            {"email": "convidada@example.com", "response": "needsAction", "optional": True},
            {"email": "time@group.calendar.google.com", "response": "accepted"},
        ],
        "organizer": "pessoa@example.com", "description": "pauta", "location": None,
        "html_link": "https://calendar.google.com/event?eid=x",
    }
    ev.update(extra)
    return ev


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.arquivo = self.dir / "snapshot.json"

    def grava(self, doc):
        tmp = self.arquivo.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self.arquivo)

    def cal(self, token=TOKEN, **shared):
        cfg = {"zinom": {"token": token, "endpoint": "https://zinom.ai/mcp"},
               "calendar": {"zinom": {"enabled": True, "poll_interval_sec": 3600},
                            "shared_snapshot": {"path": str(self.arquivo), **shared}}}
        return ZinomCalendar(cfg)


class LeituraDoSnapshotTest(Base):
    def test_le_do_disco_sem_ir_ao_hub_e_converte_igual_a_consulta_direta(self):
        self.grava(_snapshot([_evento("ev1", "Reunião semanal", 1)]))
        cal = self.cal()
        with patch.object(ZinomCalendar, "_call", side_effect=AssertionError("foi ao hub")):
            eventos = cal.upcoming()
        self.assertEqual([e.uid for e in eventos], ["ev1"])
        ev = eventos[0]
        self.assertEqual(ev.title, "Reunião semanal")
        self.assertEqual(ev.conference_url, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(ev.calendar_name, "pessoa@example.com")
        self.assertEqual(ev.source, "zinom")
        # Organizador primeiro, nome de quem não tem nome vem do e-mail, agenda de grupo fora.
        self.assertEqual([(a.name, a.organizer, a.optional) for a in ev.attendees],
                         [("Pessoa Um", True, False), ("Convidada", False, True)])
        self.assertIsNone(cal.last_error)
        self.assertAlmostEqual(cal.stale_since, time.time(), delta=5)

    def test_agenda_writer_entra_e_reader_fica_de_fora_como_na_consulta_direta(self):
        self.grava(_snapshot([_evento("g", "Do grupo", 1, ref="ref-grupo"),
                              _evento("n", "Aniversário", 2, ref="ref-aniversarios"),
                              _evento("x", "Agenda desconhecida", 2, ref="ref-sumiu")]))
        self.assertEqual([e.uid for e in self.cal().upcoming()], ["g"])

    def test_lista_explicita_de_agendas_vale_no_snapshot(self):
        self.grava(_snapshot([_evento("a", "A", 1), _evento("g", "G", 2, ref="ref-grupo")]))
        cal = self.cal()
        cal.wanted = ["Eventos do time"]
        self.assertEqual([e.uid for e in cal.upcoming()], ["g"])

    def test_janela_e_dia_inteiro(self):
        hoje = _agora().replace(hour=0, minute=0, second=0, microsecond=0)
        dia = {"uid": "d", "title": "Feriado local", "all_day": True, "start": _iso(hoje),
               "end": _iso(hoje + datetime.timedelta(days=1)), "calendar_ref": "ref-trabalho",
               "calendar": "pessoa@example.com", "account": "pessoa@example.com", "attendees": []}
        self.grava(_snapshot([_evento("passou", "Já acabou", -3), _evento("agora", "Em andamento", -0.5),
                              _evento("longe", "Daqui a 3 dias", 72), dia]))
        uids = [e.uid for e in self.cal().upcoming()]
        self.assertEqual(sorted(uids), ["agora", "d"])
        cal = self.cal()
        cal.skip_all_day = True
        self.assertEqual([e.uid for e in cal.upcoming()], ["agora"])

    def test_titulo_vazio_do_coletor_vira_reuniao(self):
        self.grava(_snapshot([_evento("s", "(sem título)", 1)]))
        self.assertEqual(self.cal().upcoming()[0].title, "Reunião")

    def test_releitura_so_quando_o_arquivo_muda(self):
        self.grava(_snapshot([_evento("a", "A", 1)]))
        cal = self.cal()
        cal.upcoming()
        with patch("castanha.shared_agenda.json.load", side_effect=AssertionError("releu")):
            self.assertEqual([e.uid for e in cal.upcoming()], ["a"])
        self.grava(_snapshot([_evento("a", "A", 1), _evento("b", "B", 2)]))
        self.assertEqual([e.uid for e in cal.upcoming()], ["a", "b"])

    def test_eventos_preservados_de_coleta_anterior_avisam_e_datam_pela_coleta_boa(self):
        antes = _agora() - datetime.timedelta(hours=2)
        self.grava(_snapshot([_evento("a", "A", 1)], ok=False, stale=True, stale_since=_iso(antes),
                             stats={"preserved": ["week", "window", "accounts", "calendars", "events"]}))
        cal = self.cal()
        self.assertEqual([e.uid for e in cal.upcoming()], ["a"])
        self.assertEqual(cal.last_error, "o coletor da agenda falhou")
        self.assertAlmostEqual(cal.stale_since, antes.timestamp(), delta=1)

    def test_so_tarefas_falhando_nao_envelhece_a_agenda(self):
        antes = _agora() - datetime.timedelta(hours=20)
        self.grava(_snapshot([_evento("a", "A", 1)], ok=False, stale=True, stale_since=_iso(antes),
                             stats={"preserved": ["tasks"]}))
        cal = self.cal()
        with patch.object(ZinomCalendar, "_call", side_effect=AssertionError("foi ao hub")):
            self.assertEqual([e.uid for e in cal.upcoming()], ["a"])
        self.assertIsNone(cal.last_error)


class QuedaParaConsultaDiretaTest(Base):
    def _ao_vivo(self, cal):
        chamadas = []

        def fake(nome, args):
            chamadas.append(nome)
            if nome == "list_calendars":
                return {"calendars": [{"calendar_ref": "ref-trabalho", "summary": "pessoa@example.com",
                                       "email": "pessoa@example.com", "primary": True, "accessRole": "owner"}]}
            inicio = _agora() + datetime.timedelta(hours=1)
            return {"events": [{"id": "vivo", "summary": "Ao vivo",
                                "start": {"dateTime": _iso(inicio)},
                                "end": {"dateTime": _iso(inicio + datetime.timedelta(hours=1))}}]}
        return chamadas, fake

    def test_arquivo_ausente_consulta_direto_na_cadencia_longa(self):
        cal = self.cal()
        chamadas, fake = self._ao_vivo(cal)
        with patch.object(ZinomCalendar, "_call", side_effect=fake), redirect_stderr(io.StringIO()) as err:
            self.assertEqual([e.uid for e in cal.upcoming()], ["vivo"])
            cal.upcoming()  # dentro da cadência: cache, sem nova chamada
        self.assertEqual(chamadas, ["list_calendars", "list_event_details"])
        self.assertIn("ausente", err.getvalue())

    def test_snapshot_velho_demais_consulta_direto(self):
        velho = _agora() - datetime.timedelta(hours=13)
        self.grava(_snapshot([_evento("a", "A", 1)], gerado=velho))
        cal = self.cal(max_age_sec=43200)
        chamadas, fake = self._ao_vivo(cal)
        with patch.object(ZinomCalendar, "_call", side_effect=fake), redirect_stderr(io.StringIO()):
            self.assertEqual([e.uid for e in cal.upcoming()], ["vivo"])
        self.assertTrue(chamadas)

    def test_schema_desconhecido_nao_e_lido(self):
        self.grava(dict(_snapshot([_evento("a", "A", 1)]), schema=3))
        cal = self.cal()
        chamadas, fake = self._ao_vivo(cal)
        with patch.object(ZinomCalendar, "_call", side_effect=fake), redirect_stderr(io.StringIO()) as err:
            self.assertEqual([e.uid for e in cal.upcoming()], ["vivo"])
        self.assertIn("formato", err.getvalue())

    def test_sem_token_e_sem_snapshot_avisa_sem_tentar_o_hub(self):
        cal = self.cal(token="")
        self.assertTrue(cal.enabled)
        with patch.object(ZinomCalendar, "_call", side_effect=AssertionError("foi ao hub")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(cal.upcoming(), [])
        self.assertIn("ausente", cal.last_error)


class AtualizarTest(Base):
    def test_atualizar_pede_coleta_ao_coletor_e_le_o_resultado(self):
        self.grava(_snapshot([_evento("a", "A", 1)]))
        script = self.dir / "coletor.py"
        novo = _snapshot([_evento("a", "A", 1), _evento("b", "Marcada agora", 0.5)])
        script.write_text(
            "import json, os, sys\n"
            f"alvo = {str(self.arquivo)!r}\n"
            f"open(alvo + '.tmp', 'w').write({json.dumps(json.dumps(novo))})\n"
            "os.replace(alvo + '.tmp', alvo)\n")
        cal = self.cal(refresh_command=[sys.executable, str(script)])
        cal.upcoming()
        with patch.object(ZinomCalendar, "_call", side_effect=AssertionError("foi ao hub")):
            self.assertEqual([e.uid for e in cal.upcoming(force=True)], ["b", "a"])

    def test_atualizar_sem_comando_consulta_direto(self):
        self.grava(_snapshot([_evento("a", "A", 1)]))
        cal = self.cal()
        with patch.object(ZinomCalendar, "_call", side_effect=lambda n, a: {"calendars": []}) as chamada:
            cal.upcoming(force=True)
        self.assertTrue(chamada.called)

    def test_comando_que_falha_nao_derruba(self):
        self.grava(_snapshot([_evento("a", "A", 1)]))
        snap = SharedSnapshot({"path": str(self.arquivo), "refresh_command": ["/nao/existe/coletor"]})
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(snap.refresh())
        self.assertIn("não rodou", err.getvalue())


class TokenEmArquivoTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.chmod(self.dir, 0o700)
        self.env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.dir / "cfg"),
                                             "XDG_STATE_HOME": str(self.dir / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.dir / "cfg" / "castanha").mkdir(parents=True, mode=0o700)
        self.tokens = self.dir / "segredos.env"
        self.tokens.write_text(f"# comentário\nOUTRA=valor-que-nao-e-este\nexport CHAVE_TESTE='{TOKEN}'\n")
        os.chmod(self.tokens, 0o600)

    def config(self, zinom):
        alvo = self.dir / "cfg" / "castanha" / "config.json"
        alvo.write_text(json.dumps({"zinom": zinom}))
        os.chmod(alvo, 0o600)

    def test_le_a_chave_do_arquivo_env(self):
        self.config({"enabled": True, "token_file": str(self.tokens), "token_key": "CHAVE_TESTE"})
        self.assertEqual(load_config()["zinom"]["token"], TOKEN)

    def test_arquivo_inteiro_sem_chave(self):
        so = self.dir / "token"
        so.write_text(TOKEN + "\n")
        os.chmod(so, 0o600)
        self.assertEqual(read_secret_file(so), TOKEN)

    def test_token_literal_continua_valendo_primeiro(self):
        self.config({"token": "acct_literal", "token_file": str(self.tokens), "token_key": "CHAVE_TESTE"})
        self.assertEqual(load_config()["zinom"]["token"], "acct_literal")

    def test_recusas_nao_mostram_o_conteudo(self):
        casos = []
        aberto = self.dir / "aberto.env"
        aberto.write_text(f"CHAVE_TESTE={TOKEN}\n")
        os.chmod(aberto, 0o644)
        casos.append((aberto, "CHAVE_TESTE", "chmod 600"))
        link = self.dir / "link.env"
        link.symlink_to(self.tokens)
        casos.append((link, "CHAVE_TESTE", "symlink"))
        casos.append((self.tokens, "FALTA", "ausente"))
        vazio = self.dir / "vazio.env"
        vazio.write_text("CHAVE_TESTE=\n")
        os.chmod(vazio, 0o600)
        casos.append((vazio, "CHAVE_TESTE", "vazia"))
        for caminho, chave, trecho in casos:
            with self.subTest(caminho=caminho.name):
                with self.assertRaises(InsecureConfigError) as ctx:
                    read_secret_file(caminho, chave)
                self.assertIn(trecho, str(ctx.exception))
                self.assertNotIn(TOKEN, str(ctx.exception))

    def test_falha_vira_aviso_e_token_vazio(self):
        self.config({"enabled": True, "token_file": str(self.dir / "nao-existe"), "token_key": "CHAVE_TESTE"})
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(load_config()["zinom"]["token"], "")
        self.assertIn("token_file", err.getvalue())

    def test_config_show_oculta_segredos(self):
        cfg = {"zinom": {"token": TOKEN, "token_file": "/x", "token_key": "CHAVE_TESTE"},
               "transcription": {"groq_api_key": "gsk_x", "vps_auth_token": ""},
               "llm": {"api_key": "k", "hermes_models": [{"provider": "a"}]}}
        texto = json.dumps(redact_secrets(cfg))
        self.assertNotIn(TOKEN, texto)
        self.assertNotIn("gsk_x", texto)
        self.assertIn('"token_file": "/x"', texto)
        self.assertIn('"token_key": "CHAVE_TESTE"', texto)
        self.assertIn('"vps_auth_token": ""', texto)


class ConferenciaComOZinomTest(unittest.TestCase):
    def daemon(self, zinom):
        from castanha.daemon import CastanhaDaemon
        d = CastanhaDaemon.__new__(CastanhaDaemon)
        d.config = {"zinom": zinom}
        d._heartbeat_thread = None
        d._next_heartbeat = 0.0
        return d

    def test_desligada_por_padrao(self):
        self.assertFalse(self.daemon({"enabled": True, "token": TOKEN})._heartbeat_due(time.time()))
        self.assertFalse(self.daemon({"enabled": True, "token": "", "heartbeat_sec": 60})._heartbeat_due(time.time()))

    def test_chama_brain_status_e_espera_o_intervalo(self):
        d = self.daemon({"enabled": True, "token": TOKEN, "heartbeat_sec": 21600})
        agora = time.time()
        self.assertTrue(d._heartbeat_due(agora))
        with patch("castanha.zinom_adapter.ZinomMcpClient") as cliente:
            d._start_heartbeat(agora)
            d._heartbeat_thread.join(5)
        cliente.return_value.call_tool.assert_called_once_with("brain_status", {})
        self.assertFalse(d._heartbeat_due(agora + 3600))
        self.assertTrue(d._heartbeat_due(agora + 21601))

    def test_falha_tenta_de_novo_em_15_min_sem_mostrar_token(self):
        d = self.daemon({"enabled": True, "token": TOKEN, "heartbeat_sec": 21600})
        agora = time.time()
        with patch("castanha.zinom_adapter.ZinomMcpClient") as cliente, redirect_stderr(io.StringIO()) as err:
            cliente.return_value.connect.side_effect = RuntimeError("HTTP 401: token inválido")
            d._start_heartbeat(agora)
            d._heartbeat_thread.join(5)
        self.assertIn("falhou", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertTrue(d._heartbeat_due(agora + 901))


class DataDaAgendaNoEstadoTest(Base):
    def test_agenda_updated_at_e_a_hora_da_coleta(self):
        import castanha.agenda as agenda
        gerado = _agora() - datetime.timedelta(minutes=40)
        self.grava(_snapshot([_evento("a", "A", 1)], gerado=gerado))
        cfg = {"zinom": {"token": TOKEN}, "calendar": {"zinom": {"enabled": True},
                                                       "shared_snapshot": {"path": str(self.arquivo)}}}
        with patch.object(agenda, "_zinom", None), patch.dict("os.environ", {"XDG_STATE_HOME": str(self.dir)}):
            agenda.collect_upcoming(cfg)
            self.assertAlmostEqual(agenda.agenda_data_time(cfg), gerado.timestamp(), delta=1)


if __name__ == "__main__":
    unittest.main()
