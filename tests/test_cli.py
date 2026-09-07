import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

class TestCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.cfg_dir = self.temp_dir / "config" / "castanha"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        self.meetings = self.temp_dir / "meetings"
        self.bronze = self.meetings / "bronze"
        self.silver = self.meetings / "silver"
        self.gold = self.meetings / "gold"
        for d in [self.bronze, self.silver, self.gold]:
            d.mkdir(parents=True, exist_ok=True)

        (self.cfg_dir / "config.json").write_text(json.dumps({
            "storage": {
                "base_dir": str(self.meetings),
                "bronze_dir": str(self.bronze),
                "silver_dir": str(self.silver),
                "gold_dir": str(self.gold),
            },
            "llm": {"provider": "groq", "api_key": ""},
            # Mock só por escolha explícita: sem ele, "sem transcritor" é falha declarada.
            "transcription": {"provider": "mock", "groq_api_key": "", "vps_ssh_host": "nonexistent"},
            "zinom": {"enabled": False},
        }), encoding="utf-8")

        self.env = {
            "XDG_CONFIG_HOME": str(self.temp_dir / "config"),
            "XDG_STATE_HOME": str(self.temp_dir / "state"),
            "CASTANHA_LANG": "pt_BR",
            "PATH": sys.executable + ":" + str(Path(sys.executable).parent),
        }
        self.cli_bin = Path(__file__).resolve().parent.parent / "bin" / "castanha"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_cli(self, *args, real_fake=False):
        cmd = [sys.executable, str(self.cli_bin)] + list(args)
        if real_fake:
            # Atestado explícito e exclusivo do teste: representa um provider
            # real com resposta controlada, não promove MockTranscriber.
            # Executa o parser/CLI, engine e armazenamento reais no subprocesso.
            bootstrap = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from castanha.transcription import TranscriptionResult

def transcribe(audio_path, mode="dual"):
    assert Path(audio_path).read_bytes().startswith(b"OggS")
    assert mode == "dual"
    return TranscriptionResult(
        text="Resposta controlada do provider real-fake.", utterances=[],
        provider="real-fake", raw_response={"attested_test_fixture": True},
    )

sys.argv = sys.argv[1:]
with patch("castanha.engine.get_transcriber") as provider:
    provider.return_value.transcribe.side_effect = transcribe
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    finally:
        provider.return_value.transcribe.assert_called_once()
"""
            cmd = [sys.executable, "-c", bootstrap, str(self.cli_bin)] + list(args)
        import os
        full_env = dict(os.environ)
        full_env.update(self.env)
        res = subprocess.run(cmd, capture_output=True, text=True, env=full_env)
        return res

    def test_cli_notes_and_delete_recording(self):
        slug = "2026-09-04_1200_reuniao-teste"
        m_bronze = self.bronze / slug
        m_bronze.mkdir(parents=True, exist_ok=True)
        
        # Cria arquivos dummy
        (m_bronze / "audio.ogg").write_bytes(b"dummy audio data")
        (m_bronze / "transcript_raw.txt").write_text("Transcrição do teste.", encoding="utf-8")
        (m_bronze / "metadata.json").write_text(json.dumps({
            "title": "Reunião de Teste CLI",
            "recorded_at": "2026-09-04T12:00:00Z",
            "duration_seconds": 45,
            "attendees": [{"name": "Alice", "email": "alice@example.com"}],
            "recordings": [{
                "filename": "audio.ogg",
                "path": str(m_bronze / "audio.ogg"),
                "size_bytes": 16,
                "size_human": "16 B",
                "duration_seconds": 45,
                "audio_status": "ok",
            }],
        }), encoding="utf-8")

        (self.silver / f"{slug}.md").write_text(
            "# Reunião de Teste CLI\n\n## 📌 Resumo Executivo\nDiscussão sobre a CLI.\n",
            encoding="utf-8"
        )

        # 1. notes --json lista com detalhes
        res = self._run_cli("notes", "--json")
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["notes"]), 1)
        n = data["notes"][0]
        self.assertEqual(n["slug"], slug)
        self.assertEqual(len(n["attendees"]), 1)
        self.assertEqual(n["recordings_count"], 1)
        self.assertTrue(n["has_audio"])
        self.assertIn("Discussão sobre a CLI", n["summary_preview"])

        # 2. notes <slug> --json retorna detalhes individuais
        res_slug = self._run_cli("notes", slug, "--json")
        self.assertEqual(res_slug.returncode, 0)
        details = json.loads(res_slug.stdout)
        self.assertEqual(details["title"], "Reunião de Teste CLI")
        self.assertEqual(details["attendees"][0]["name"], "Alice")

        # 3. recordings list
        res_rec = self._run_cli("recordings", "list", slug, "--json")
        self.assertEqual(res_rec.returncode, 0)
        recs = json.loads(res_rec.stdout)["recordings"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["filename"], "audio.ogg")

        # 4. delete-recording
        res_del = self._run_cli("delete-recording", slug, "--json")
        self.assertEqual(res_del.returncode, 0)
        del_data = json.loads(res_del.stdout)
        self.assertEqual(del_data["status"], "ok")
        self.assertEqual(del_data["deleted_file"], "audio.ogg")
        self.assertFalse((m_bronze / "audio.ogg").exists())

        # Notas e transcrição continuam existindo
        self.assertTrue((self.silver / f"{slug}.md").exists())
        self.assertTrue((m_bronze / "transcript_raw.txt").exists())

        # 5. notes <slug> pós exclusão do áudio mostra has_audio = False
        res_slug2 = self._run_cli("notes", slug, "--json")
        details2 = json.loads(res_slug2.stdout)
        self.assertFalse(details2["has_audio"])
        self.assertEqual(details2["audio_status"], "audio_apagado")

    def test_agenda_refresh_cli(self):
        res = self._run_cli("agenda", "refresh", "--json")
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertIn("meetings", data)

        res_alias = self._run_cli("refresh-agenda", "--json")
        self.assertEqual(res_alias.returncode, 0)
        data_alias = json.loads(res_alias.stdout)
        self.assertEqual(data_alias["status"], "ok")

    def test_cli_retry_mock_quarantined_not_completed(self):
        slug = "2026-09-05_1300_mock-isolado"
        m_bronze = self._bronze_falho(slug)
        audio_before = (m_bronze / "audio.ogg").read_bytes()

        res = self._run_cli("retry", slug, "--json")
        self.assertEqual(res.returncode, 2, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["result"]["transcription_provider"], "failed")
        self.assertIn("simulada", result["result"]["transcription_error"])
        self.assertEqual((m_bronze / "transcript_raw.txt").read_text(), "")
        self.assertFalse((self.silver / f"{slug}.md").exists())
        self.assertFalse((self.gold / f"{slug}.json").exists())

        quarantine = json.loads((m_bronze / ".mock-history" / "audio.ogg.json").read_text())
        self.assertEqual(quarantine["provider"], "mock")
        self.assertIn("Transcrição Simulada", quarantine["transcript"])
        self.assertEqual(quarantine["recording"]["filename"], "audio.ogg")
        meta = json.loads((m_bronze / "metadata.json").read_text())
        self.assertEqual(meta["recordings"][0]["transcription_provider"], "mock")
        self.assertEqual(meta["memory_recording_ids"], [])
        self.assertFalse(meta.get("zinom"))
        self.assertEqual((m_bronze / "audio.ogg").read_bytes(), audio_before)
        notes = self._run_cli("notes", slug, "--json")
        self.assertEqual(notes.returncode, 0, notes.stderr)
        self.assertTrue(json.loads(notes.stdout)["can_retry"])

    def test_cli_retry_reprocess(self):
        slug = "2026-09-05_1300_falha-teste"
        m_bronze = self._bronze_falho(slug)
        audio_before = (m_bronze / "audio.ogg").read_bytes()
        self._sem_transcritor()

        # Falha real da tentativa mantém o áudio e a segunda chance.
        failed = self._run_cli("retry", slug, "--json")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(json.loads(failed.stdout)["results"][0]["result"]["transcription_provider"], "failed")
        notes = self._run_cli("notes", slug, "--json")
        self.assertEqual(notes.returncode, 0, notes.stderr)
        self.assertTrue(json.loads(notes.stdout)["can_retry"])

        # Sem slug, seleciona a pendência e só substitui o provider externo.
        res = self._run_cli("retry", "--json", real_fake=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        results = json.loads(res.stdout)["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["slug"], slug)
        self.assertIn(results[0]["status"], ("success", "partial"))
        self.assertEqual(results[0]["result"]["transcription_provider"], "real-fake")
        self.assertIsNone(results[0]["result"]["transcription_error"])
        raw_text = (m_bronze / "transcript_raw.txt").read_text()
        self.assertEqual(raw_text.count("Resposta controlada do provider real-fake."), 1)
        self.assertNotIn("Simulada", raw_text)
        self.assertTrue((self.silver / f"{slug}.md").read_text().strip())
        self.assertIsInstance(json.loads((self.gold / f"{slug}.json").read_text()), dict)
        meta = json.loads((m_bronze / "metadata.json").read_text())
        self.assertEqual(meta["recordings"][0]["transcription_provider"], "real-fake")
        self.assertTrue(meta["recordings"][0]["transcribed"])
        self.assertEqual(meta["memory_recording_ids"], ["audio.ogg"])
        self.assertEqual((m_bronze / "audio.ogg").read_bytes(), audio_before)
        notes = self._run_cli("notes", slug, "--json")
        self.assertEqual(notes.returncode, 0, notes.stderr)
        self.assertFalse(json.loads(notes.stdout)["can_retry"])

        # Outra chamada sem slug não refaz uma reunião já resolvida.
        paths = [m_bronze / "metadata.json", m_bronze / "transcript_raw.txt",
                 self.silver / f"{slug}.md", self.gold / f"{slug}.json"]
        before = {path: path.read_bytes() for path in paths}
        repeated = self._run_cli("retry", "--json")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["results"], [])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def _bronze_falho(self, slug, transcript=""):
        m_bronze = self.bronze / slug
        m_bronze.mkdir(parents=True, exist_ok=True)
        audio_file = m_bronze / "audio.ogg"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1", "-c:a", "libopus", "-b:a", "64k", str(audio_file)],
            capture_output=True, check=True
        )
        (m_bronze / "transcript_raw.txt").write_text(transcript, encoding="utf-8")
        (m_bronze / "metadata.json").write_text(json.dumps({
            "title": "Reunião", "slug": slug, "recorded_at": "2026-09-05T13:00:00", "duration_seconds": 1.0,
            "transcription_provider": "failed", "transcription_error": "timeout",
            "recordings": [{"id": "audio.ogg", "filename": "audio.ogg", "path": str(audio_file),
                            "size_bytes": audio_file.stat().st_size, "size_human": "10 KB", "duration_seconds": 1.0}],
        }), encoding="utf-8")
        return m_bronze

    def _sem_transcritor(self):
        cfg = json.loads((self.cfg_dir / "config.json").read_text())
        cfg["transcription"] = {"groq_api_key": "", "vps_ssh_host": "nonexistent"}
        (self.cfg_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def test_retry_sem_transcritor_falha_honesto_e_mantem_a_segunda_chance(self):
        slug = "2026-09-05_1300_offline"
        m_bronze = self._bronze_falho(slug)
        self._sem_transcritor()
        res = self._run_cli("retry", slug, "--json")
        self.assertEqual(res.returncode, 2, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["results"][0]["result"]["transcription_provider"], "failed")
        self.assertNotIn("Simulada", (m_bronze / "transcript_raw.txt").read_text(encoding="utf-8"))
        notes = json.loads(self._run_cli("notes", "--json").stdout)["notes"]
        self.assertTrue([n for n in notes if n["slug"] == slug][0]["can_retry"])
        self.assertFalse((self.silver / f"{slug}.md").exists(), "sem texto não se gera nota")

    def test_retry_sem_argumento_nao_toca_na_reuniao_boa(self):
        slug = "2026-09-05_1200_boa"
        m_bronze = self._bronze_falho(slug, transcript="Texto certo.")
        meta = json.loads((m_bronze / "metadata.json").read_text())
        meta["transcription_provider"] = "groq"
        meta["transcription_error"] = None
        meta["recordings"][0]["transcribed"] = True
        (m_bronze / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.silver / f"{slug}.md").write_text("# nota boa", encoding="utf-8")

        res = self._run_cli("retry", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["results"], [])
        self.assertEqual((m_bronze / "transcript_raw.txt").read_text(encoding="utf-8"), "Texto certo.")
        self.assertEqual((self.silver / f"{slug}.md").read_text(encoding="utf-8"), "# nota boa")

    def test_esconder_a_ultima_com_hora_nao_promove_dia_inteiro(self):
        from castanha.state import StateManager
        import os
        env_backup = dict(os.environ)
        os.environ.update({"XDG_STATE_HOME": self.env["XDG_STATE_HOME"], "XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]})
        try:
            StateManager().write({
                "upcoming_meetings": [
                    {"uid": "reuniao_20260905T130000Z", "series_key": "reuniao", "title": "Nora", "start": "2026-09-05T13:00:00-03:00", "end": "2026-09-05T14:00:00-03:00"},
                    {"uid": "lembrete_20260905", "series_key": "lembrete", "title": "Pagar", "start": "2026-09-05T00:00:00-03:00", "end": "2026-09-06T00:00:00-03:00", "all_day": True},
                ],
                "next_meeting": {"uid": "reuniao_20260905T130000Z", "title": "Nora"},
            })
        finally:
            os.environ.clear(); os.environ.update(env_backup)
        res = self._run_cli("agenda", "hide", "reuniao", "--title", "Nora")
        self.assertEqual(res.returncode, 0, res.stderr)
        os.environ.update({"XDG_STATE_HOME": self.env["XDG_STATE_HOME"], "XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]})
        try:
            estado = StateManager().read()
        finally:
            os.environ.clear(); os.environ.update(env_backup)
        self.assertEqual([m["uid"] for m in estado["upcoming_meetings"]], ["lembrete_20260905"])
        self.assertIsNone(estado["next_meeting"])


if __name__ == "__main__":
    unittest.main()
