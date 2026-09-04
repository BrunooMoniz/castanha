"""Reenviar uma reunião ao Zinom.

Existe porque a ingestão original roda uma vez só: se o hub estava fora do ar,
a reunião ficava em disco e a memória durável nunca recebia nada.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.storage import MeetingStorage
from castanha.sync import meeting_needs_sync, sync_meeting, sync_pending


class TestPrecisaSync(unittest.TestCase):
    def test_nunca_enviada(self):
        self.assertTrue(meeting_needs_sync({}))

    def test_falhou(self):
        self.assertTrue(meeting_needs_sync({"zinom": {"status": "error"}}))

    def test_ja_esta_la(self):
        self.assertFalse(meeting_needs_sync({"zinom": {"status": "ok"}}))

    def test_pulada_de_proposito(self):
        # Gravação muda não é pendência: é decisão.
        self.assertFalse(meeting_needs_sync({"zinom": {"status": "skipped"}}))


class TestSyncMeeting(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.storage = MeetingStorage(base_dir=self.temp)
        self.slug = "2026-09-04_1042_reuniao"
        bronze = self.storage.bronze_dir / self.slug
        bronze.mkdir(parents=True, exist_ok=True)
        (bronze / "metadata.json").write_text(json.dumps({
            "slug": self.slug, "title": "Reunião", "recorded_at": "2026-09-04T10:42:00",
            "audio_status": "ok", "transcription_provider": "groq",
        }), encoding="utf-8")
        (self.storage.silver_dir / f"{self.slug}.md").write_text("# Notas", encoding="utf-8")
        (self.storage.gold_dir / f"{self.slug}.json").write_text(
            json.dumps({"facts": [{"subject": "Bruno", "predicate": "testou", "object": "Castanha"}]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _metadata(self):
        return json.loads((self.storage.bronze_dir / self.slug / "metadata.json").read_text(encoding="utf-8"))

    def test_grava_o_resultado_no_metadata(self):
        resultado = {"status": "ok", "remember": {"ok": True, "id": "conversation:abc"},
                     "facts_ingested": 1, "errors": []}
        with patch("castanha.sync.ZinomAdapter") as adapter:
            adapter.return_value.ingest_meeting.return_value = resultado
            saida = sync_meeting(self.slug, self.storage)

        self.assertEqual(saida["status"], "ok")
        self.assertEqual(self._metadata()["zinom"]["remember_id"], "conversation:abc")
        self.assertEqual(self._metadata()["zinom"]["facts_ingested"], 1)

    def test_reenvio_edita_a_nota_em_vez_de_criar_outra(self):
        with patch("castanha.sync.ZinomAdapter") as adapter:
            adapter.return_value.ingest_meeting.return_value = {
                "status": "ok", "remember": {"ok": True, "id": "conversation:abc"},
                "facts_ingested": 0, "errors": []}
            sync_meeting(self.slug, self.storage)
            sync_meeting(self.slug, self.storage)

            # Na segunda vez o id anterior TEM que ir junto, senão vira cópia.
            segunda = adapter.return_value.ingest_meeting.call_args_list[1]
            self.assertEqual(segunda.kwargs["previous_remember_id"], "conversation:abc")

    def test_falha_fica_registrada_e_continua_pendente(self):
        with patch("castanha.sync.ZinomAdapter") as adapter:
            adapter.return_value.ingest_meeting.return_value = {
                "status": "error", "remember": None, "facts_ingested": 0,
                "errors": ["Erro no remember: HTTP 500"]}
            saida = sync_meeting(self.slug, self.storage)

        self.assertEqual(saida["status"], "error")
        self.assertTrue(meeting_needs_sync(self._metadata()))
        self.assertIn("HTTP 500", self._metadata()["zinom"]["errors"][0])

    def test_reuniao_que_nao_existe(self):
        saida = sync_meeting("nao-existe", self.storage)
        self.assertEqual(saida["status"], "error")

    def test_sync_pending_pula_o_que_ja_esta_la(self):
        meta = self._metadata()
        meta["zinom"] = {"status": "ok", "remember_id": "conversation:abc"}
        (self.storage.bronze_dir / self.slug / "metadata.json").write_text(
            json.dumps(meta), encoding="utf-8")

        with patch("castanha.sync.ZinomAdapter") as adapter:
            resultados = sync_pending(storage=self.storage)
        self.assertEqual(resultados, [])
        adapter.return_value.ingest_meeting.assert_not_called()

    def test_reuniao_legada_sem_audio_status_classifica_antes_de_enviar(self):
        # Remove audio_status do metadata
        meta = self._metadata()
        del meta["audio_status"]
        (self.storage.bronze_dir / self.slug / "metadata.json").write_text(
            json.dumps(meta), encoding="utf-8")

        fake_audio = self.storage.bronze_dir / self.slug / "audio.ogg"
        fake_audio.write_bytes(b"dummy")

        with patch("castanha.audio.measure_channel_levels") as mock_levels, \
             patch("castanha.audio.classify_audio", return_value="sem_audio"), \
             patch("castanha.sync.ZinomAdapter") as adapter:
            adapter.return_value.ingest_meeting.return_value = {
                "status": "skipped", "reason": "Gravação sem áudio, nada para lembrar"}
            saida = sync_meeting(self.slug, self.storage)

        self.assertEqual(saida["status"], "skipped")
        self.assertEqual(self._metadata()["audio_status"], "sem_audio")


if __name__ == "__main__":
    unittest.main()
