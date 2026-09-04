import json
import shutil
import tempfile
import unittest
from pathlib import Path
from castanha.storage import MeetingStorage

class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.storage = MeetingStorage(base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bronze_silver_gold_pipeline(self):
        slug = "2026-09-04_1000_alinhamento-zinom"
        
        # Cria arquivo de áudio fake
        fake_audio = self.temp_dir / "sample.ogg"
        fake_audio.write_bytes(b"dummy-audio-bytes")

        # 1. Bronze
        metadata = {
            "title": "Alinhamento Zinom",
            "recorded_at": "2026-09-04T10:00:00Z",
            "duration_seconds": 120,
            "mode": "dual",
        }
        bronze_dir = self.storage.save_bronze(
            slug=slug,
            audio_source_path=fake_audio,
            metadata=metadata,
            raw_transcript="Olá mundo da transcrição.",
        )
        self.assertTrue((bronze_dir / "audio.ogg").exists())
        self.assertTrue((bronze_dir / "metadata.json").exists())
        self.assertTrue((bronze_dir / "transcript_raw.txt").exists())

        # 2. Silver
        silver_path = self.storage.save_silver(
            slug=slug,
            markdown_content="# Alinhamento\n\n## Resumo\nReunião produtiva.",
        )
        self.assertTrue(silver_path.exists())
        self.assertIn("Reunião produtiva", silver_path.read_text(encoding="utf-8"))

        # 3. Gold
        gold_path = self.storage.save_gold(
            slug=slug,
            gold_data={"facts": [{"subject": "Bruno", "predicate": "aprovou", "object": "Castanha"}]},
        )
        self.assertTrue(gold_path.exists())
        data = json.loads(gold_path.read_text(encoding="utf-8"))
        self.assertEqual(data["facts"][0]["subject"], "Bruno")

        # 4. Listagem
        recent = self.storage.list_recent_meetings(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["slug"], slug)

    def test_create_meeting_slug_collision(self):
        slug1 = self.storage.create_meeting_slug("Alinhamento")
        (self.storage.bronze_dir / slug1).mkdir(parents=True, exist_ok=True)
        slug2 = self.storage.create_meeting_slug("Alinhamento")
        self.assertEqual(slug2, f"{slug1}-2")
        (self.storage.bronze_dir / slug2).mkdir(parents=True, exist_ok=True)
        slug3 = self.storage.create_meeting_slug("Alinhamento")
        self.assertEqual(slug3, f"{slug1}-3")

if __name__ == "__main__":
    unittest.main()
