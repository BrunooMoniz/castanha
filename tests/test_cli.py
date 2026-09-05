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
            "llm": {"api_key": ""},
            "transcription": {"groq_api_key": "", "vps_ssh_host": "nonexistent"},
            "zinom": {"enabled": False},
        }), encoding="utf-8")

        self.env = {
            "XDG_CONFIG_HOME": str(self.temp_dir / "config"),
            "XDG_STATE_HOME": str(self.temp_dir / "state"),
            "PATH": sys.executable + ":" + str(Path(sys.executable).parent),
        }
        self.cli_bin = Path(__file__).resolve().parent.parent / "bin" / "castanha"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_cli(self, *args):
        cmd = [sys.executable, str(self.cli_bin)] + list(args)
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

    def test_cli_retry_reprocess(self):
        slug = "2026-09-05_1300_falha-teste"
        m_bronze = self.bronze / slug
        m_bronze.mkdir(parents=True, exist_ok=True)
        # Gera áudio de teste com ffmpeg para passar na verificação de canais
        audio_file = m_bronze / "audio.ogg"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1", "-c:a", "libopus", "-b:a", "64k", str(audio_file)],
            capture_output=True, check=True
        )
        (m_bronze / "transcript_raw.txt").write_text("", encoding="utf-8")
        (m_bronze / "metadata.json").write_text(json.dumps({
            "title": "Reunião Falha Inicial",
            "slug": slug,
            "recorded_at": "2026-09-05T13:00:00",
            "duration_seconds": 1.0,
            "transcription_provider": "failed",
            "transcription_error": "Connection timed out",
            "recordings": [{
                "id": "audio.ogg",
                "filename": "audio.ogg",
                "path": str(audio_file),
                "size_bytes": audio_file.stat().st_size,
                "size_human": "10 KB",
                "duration_seconds": 1.0,
            }],
        }), encoding="utf-8")

        # 1. notes --json mostra can_retry = True
        res_notes = self._run_cli("notes", "--json")
        notes_data = json.loads(res_notes.stdout)
        matching = [n for n in notes_data["notes"] if n["slug"] == slug]
        self.assertTrue(len(matching) == 1)
        self.assertTrue(matching[0]["can_retry"])

        # 2. Executa retry
        res_retry = self._run_cli("retry", slug, "--json")
        self.assertEqual(res_retry.returncode, 0)
        retry_data = json.loads(res_retry.stdout)
        self.assertTrue(len(retry_data["results"]) > 0)
        self.assertIn(retry_data["results"][0]["status"], ("success", "partial"))

        # 3. Transcrição e notas foram geradas
        self.assertTrue((self.silver / f"{slug}.md").exists())
        raw_text = (m_bronze / "transcript_raw.txt").read_text(encoding="utf-8")
        self.assertTrue(len(raw_text) > 0)

        # 4. can_retry agora é False
        res_notes2 = self._run_cli("notes", "--json")
        notes_data2 = json.loads(res_notes2.stdout)
        matching2 = [n for n in notes_data2["notes"] if n["slug"] == slug]
        self.assertFalse(matching2[0]["can_retry"])

if __name__ == "__main__":
    unittest.main()
