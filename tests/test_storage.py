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

    def test_multiple_recordings_and_deletion(self):
        slug = "2026-09-04_1100_planejamento"
        fake_audio1 = self.temp_dir / "sample1.ogg"
        fake_audio1.write_bytes(b"audio-part-1")
        
        metadata = {
            "title": "Planejamento Trimestral",
            "recorded_at": "2026-09-04T11:00:00Z",
            "duration_seconds": 60,
            "attendees": [{"name": "Bruno", "email": "bruno@example.com"}],
        }
        self.storage.save_bronze(
            slug=slug,
            audio_source_path=fake_audio1,
            metadata=metadata,
            raw_transcript="Transcrição parte 1.",
        )
        self.storage.save_silver(
            slug=slug,
            markdown_content="# Planejamento\n\n## 📌 Resumo Executivo\nDefinidas metas do Q4 com sucesso.",
        )
        self.storage.save_gold(slug=slug, gold_data={"facts": []})

        # Deve listar 1 gravação
        recs = self.storage.list_meeting_recordings(slug)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["filename"], "audio.ogg")

        # Adiciona segunda gravação na mesma reunião
        fake_audio2 = self.temp_dir / "sample2.ogg"
        fake_audio2.write_bytes(b"audio-part-2-longer")
        added = self.storage.add_recording(
            slug=slug,
            audio_source_path=fake_audio2,
            metadata_update={"duration_seconds": 120, "audio_status": "ok"},
            raw_transcript="Transcrição parte 2.",
        )
        self.assertEqual(added["filename"], "audio_2.ogg")

        # Agora a reunião tem 2 gravações
        recs = self.storage.list_meeting_recordings(slug)
        self.assertEqual(len(recs), 2)
        m = self.storage.get_meeting(slug)
        self.assertEqual(m["recordings_count"], 2)
        self.assertTrue(m["has_audio"])
        self.assertEqual(len(m["attendees"]), 1)
        self.assertIn("Definidas metas do Q4", m["summary_preview"])

        # Tentar apagar sem especificar arquivo quando há múltiplos deve retornar erro
        err_del = self.storage.delete_recording(slug)
        self.assertEqual(err_del["status"], "error")
        self.assertIn("múltiplas gravações", err_del["message"])

        # Apagar a gravação 1 especificando o nome
        del1 = self.storage.delete_recording(slug, "audio.ogg")
        self.assertEqual(del1["status"], "ok")
        self.assertEqual(del1["deleted_file"], "audio.ogg")
        self.assertFalse((self.storage.bronze_dir / slug / "audio.ogg").exists())
        self.assertTrue((self.storage.bronze_dir / slug / "audio_2.ogg").exists())

        # Notas Silver e Transcrição permanecem intactas
        self.assertTrue((self.storage.silver_dir / f"{slug}.md").exists())
        transcript_text = (self.storage.bronze_dir / slug / "transcript_raw.txt").read_text()
        self.assertIn("Transcrição parte 1.", transcript_text)
        self.assertIn("Transcrição parte 2.", transcript_text)

        # Apagar a gravação restante (agora há só 1, não precisa passar nome)
        del2 = self.storage.delete_recording(slug)
        self.assertEqual(del2["status"], "ok")
        self.assertEqual(del2["deleted_file"], "audio_2.ogg")
        self.assertEqual(del2["remaining_count"], 0)

        # Confirmar que a reunião continua existindo com suas notas
        m_after = self.storage.get_meeting(slug)
        self.assertIsNotNone(m_after)
        self.assertFalse(m_after["has_audio"])
        self.assertEqual(m_after["audio_status"], "audio_apagado")
        self.assertTrue(m_after["has_transcript"])
        self.assertTrue(Path(m_after["silver_path"]).exists())

if __name__ == "__main__":
    unittest.main()


class TestPodeReprocessar(unittest.TestCase):
    """O botão de "tentar de novo" só aparece quando repetir pode render transcrição."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.storage = MeetingStorage(base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _bronze(self, slug, transcript="", **meta):
        d = self.storage.bronze_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "audio.ogg").write_bytes(b"x" * 1024)
        (d / "transcript_raw.txt").write_text(transcript, encoding="utf-8")
        base = {
            "slug": slug, "title": slug, "recorded_at": "2026-09-05T10:00:00", "mode": "dual",
            "recordings": [{"id": "audio.ogg", "filename": "audio.ogg", "path": str(d / "audio.ogg"),
                            "size_bytes": 1024, "size_human": "1 KB", "duration_seconds": 10.0}],
        }
        base.update(meta)
        (d / "metadata.json").write_text(json.dumps(base), encoding="utf-8")
        return slug

    def _flag(self, slug):
        return self.storage.get_meeting(slug)["can_retry"]

    def test_transcricao_falhou_pode(self):
        slug = self._bronze("a", transcription_provider="failed", transcription_error="timeout", audio_status="ok")
        self.assertTrue(self._flag(slug))
        self.assertTrue(any(m["can_retry"] for m in self.storage.list_recent_meetings() if m["slug"] == slug))

    def test_transcrita_nao_pode(self):
        slug = self._bronze("b", transcript="fala transcrita", transcription_provider="groq", audio_status="ok")
        self.assertFalse(self._flag(slug))

    def test_gravacao_muda_nao_ganha_botao(self):
        slug = self._bronze("c", transcription_provider="nenhum (áudio em silêncio)", audio_status="sem_audio")
        self.assertFalse(self._flag(slug))

    def test_sem_audio_no_bronze_nao_pode(self):
        slug = self._bronze("d", transcription_provider="failed", audio_status="ok")
        (self.storage.bronze_dir / slug / "audio.ogg").unlink()
        meta = json.loads((self.storage.bronze_dir / slug / "metadata.json").read_text())
        meta["recordings"] = []
        (self.storage.bronze_dir / slug / "metadata.json").write_text(json.dumps(meta))
        self.assertFalse(self._flag(slug))

    def test_reuniao_so_no_bronze_aparece_na_lista(self):
        """Sem Silver (a esteira morreu antes), a reunião ainda tem que aparecer para dar a segunda chance."""
        slug = self._bronze("e", transcription_provider="failed", audio_status="ok")
        slugs = [m["slug"] for m in self.storage.list_recent_meetings()]
        self.assertIn(slug, slugs)

    def test_envio_pulado_nao_perde_o_id_da_nota(self):
        slug = self._bronze("z", transcript="x", transcription_provider="groq", audio_status="ok",
                            zinom={"status": "ok", "remember_id": "nota-77"})
        self.storage.record_zinom_result(slug, {"status": "skipped", "reason": "hub fora"})
        self.assertEqual(self.storage._read_bronze_metadata(slug)["zinom"]["remember_id"], "nota-77")
        self.storage.record_zinom_result(slug, {"status": "ok", "remember": {"id": "nota-78"}})
        self.assertEqual(self.storage._read_bronze_metadata(slug)["zinom"]["remember_id"], "nota-78")

    def test_uma_gravacao_sem_texto_entre_duas_pede_retry(self):
        slug = self._bronze("m", transcript="primeira", transcription_provider="groq", audio_status="ok")
        d = self.storage.bronze_dir / slug
        (d / "audio_2.ogg").write_bytes(b"x" * 1024)
        self.storage.update_recording(slug, "audio.ogg", transcribed=True)
        self.storage.update_recording(slug, "audio_2.ogg", transcribed=False, transcription_error="offline")
        self.assertTrue(self._flag(slug))
