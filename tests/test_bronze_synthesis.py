"""Silver e fatos citados no Zinom: envelope de síntese, passagens por byte e fluxo com MCP sintético."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from castanha.bronze_ingest import (BronzeIngestError, bronze_needs_sync, build_synthesis_request,
                                    cite_facts, ingest_current_recordings, legacy_origin_ids,
                                    revision_fingerprint, synthesis_text)
from castanha.durability import write_json
from tests.test_bronze_sync import FakeMcp

SILVER = """---
title: "Reunião sintética"
date: "2026-09-05T10:30:00-03:00"
---

# Reunião sintética

## 📌 Resumo Executivo
A Nora fechou a parceria de tesouraria com a Lumix e definiu o valuation em R$ 10 mi.

## 🎯 Decisões Tomadas
- Marco Túlio entrega os relatórios fiscais até 20/09 🌰

## 📝 Transcrição Bruta
Áudio do sistema: blá blá blá, isto não vai ao Zinom de novo.
"""

GOLD = {"facts": [
    {"subject": "Nora", "predicate": "parceria de tesouraria com", "object": "Lumix",
     "evidencia": "A Nora fechou a parceria de tesouraria com a Lumix"},
    {"subject": "Marco Túlio", "predicate": "entrega relatórios fiscais até", "object": "20/09",
     "evidencia": "Marco Túlio entrega os relatórios fiscais até 20/09 🌰"},
    {"subject": "Nora", "predicate": "valuation", "object": "R$ 10 mi",
     "evidencia": "frase que não existe no Silver, inventada"},
    {"subject": "Nora", "predicate": "curta", "object": "x", "evidencia": "curta demais"},
    {"subject": "Microfone", "predicate": "estava mutado", "object": "sim",
     "evidencia": "A Nora fechou a parceria de tesouraria com a Lumix"},
]}

META = {"slug": "fixture", "title": "Reunião sintética", "recorded_at": "2026-09-05T10:30:00-03:00",
        "transcription_provider": "groq", "audio_status": "ok", "processing_status": "complete"}


def _build(silver=SILVER, gold=GOLD, origins=("native-b", "native-a"), **meta):
    return build_synthesis_request("fixture", {**META, **meta}, silver, gold,
                                   captured_at="2026-09-07T18:00:00-03:00", workspace="fixture-workspace",
                                   origin_ids=list(origins))


class TestSynthesisEnvelope(unittest.TestCase):
    def test_texto_keeps_title_and_notes_only(self):
        texto = synthesis_text(SILVER)
        self.assertTrue(texto.startswith("# Reunião sintética\n"))
        self.assertNotIn("title:", texto)
        self.assertNotIn("Transcrição Bruta", texto)
        self.assertNotIn("blá blá", texto)
        self.assertEqual(synthesis_text(""), "")

    def test_excerpts_are_byte_offsets_with_accents_and_emoji(self):
        texto = synthesis_text(SILVER)
        cited, dropped = cite_facts(texto, GOLD["facts"])
        self.assertEqual([f["object"] for f in cited], ["Lumix", "20/09"])
        # Inventada, curta demais e booleana: descartadas.
        self.assertEqual(dropped, 3)
        data = texto.encode("utf-8")
        for fact, quote in zip(cited, [GOLD["facts"][0]["evidencia"], GOLD["facts"][1]["evidencia"]]):
            start, end = fact["excerpt"]["start"], fact["excerpt"]["end"]
            self.assertEqual(data[start:end].decode("utf-8"), quote)
            self.assertEqual(fact["excerpt"]["sha256"], hashlib.sha256(data[start:end]).hexdigest())
            self.assertNotIn("evidencia", fact)
        # O hash é dos bytes exatos: acento e emoji fazem o offset em bytes diferir do de caracteres.
        self.assertGreater(cited[1]["excerpt"]["end"], texto.find(GOLD["facts"][1]["evidencia"]) + len(GOLD["facts"][1]["evidencia"]))

    def test_facts_are_capped_at_the_server_limit(self):
        many = [{"subject": "Nora", "predicate": f"p{i}", "object": "Lumix",
                 "evidencia": "A Nora fechou a parceria de tesouraria com a Lumix"} for i in range(520)]
        cited, dropped = cite_facts(synthesis_text(SILVER), many)
        self.assertEqual((len(cited), dropped), (500, 20))

    def test_envelope_shape_and_stable_identity(self):
        request = _build()
        envelope = request["envelope"]
        self.assertEqual(envelope["fidelidade"], "sintese")
        self.assertEqual(envelope["source_type"], "castanha")
        self.assertEqual(envelope["proveniencia"]["referencia"], "castanha-silver")
        self.assertEqual(envelope["proveniencia"]["origem_id"], envelope["source_id"])
        self.assertEqual(envelope["source_id"], "castanha:" + hashlib.sha256(b"silver|native-a|native-b").hexdigest())
        self.assertEqual(envelope["proveniencia"]["sha256_texto"], hashlib.sha256(envelope["texto"].encode()).hexdigest())
        self.assertEqual(envelope["timestamp"], {"valor": "2026-09-05T10:30:00-03:00", "origem": "fonte"})
        self.assertEqual(len(request["facts"]), 2)
        self.assertEqual(request, _build(origins=("native-a", "native-b")), "ordem das origens não muda a identidade")
        self.assertNotEqual(request["idempotency_key"], _build(silver=SILVER.replace("## 📝", "linha nova\n\n## 📝"))["idempotency_key"])

    def test_fingerprint_includes_facts_only_when_present(self):
        with_facts = _build()
        without = _build(gold={"facts": []})
        self.assertNotEqual(revision_fingerprint(with_facts), revision_fingerprint(without))
        stripped = {**without, "facts": []}
        self.assertEqual(revision_fingerprint(without), revision_fingerprint(stripped))

    def test_refuses_empty_notes_missing_origin_and_deleted_meeting(self):
        with self.assertRaises(BronzeIngestError):
            _build(silver="---\na: 1\n---\n\n## 📝 Transcrição Bruta\nsó transcrição")
        with self.assertRaises(BronzeIngestError):
            _build(origins=())
        with self.assertRaises(BronzeIngestError):
            _build(zinom={"status": "tombstoned"})
        with self.assertRaises(BronzeIngestError):
            _build(processing_status="pending")


class TestSynthesisDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeMcp()

    def native(self, slug="fixture"):
        bronze = self.root / "bronze" / slug
        (bronze / ".jobs").mkdir(parents=True)
        write_json(bronze / ".jobs" / "native-1.json", {
            "id": "native-1", "stage": "done", "transcript": "Transcrição integral 🌰",
            "provider": "groq", "recorded_at": "2026-09-05T10:30:00-03:00"})
        metadata = {**META, "slug": slug, "memory_recording_ids": ["capture.ogg"],
                    "recordings": [{"id": "capture.ogg", "job_id": "native-1", "transcription_provider": "groq"}]}
        write_json(bronze / "metadata.json", metadata)
        return bronze, metadata

    def legacy(self, slug="legado"):
        bronze = self.root / "bronze" / slug
        uploads = bronze / ".legacy-recovery" / "uploads"
        uploads.mkdir(parents=True)
        origin = "castanha:" + "f" * 64
        write_json(uploads / "abc.json", {"status": "ok", "request": {"envelope": {"source_id": origin}}})
        metadata = {**META, "slug": slug, "recordings": [{"id": "audio.ogg", "transcription_provider": "groq"}],
                    "zinom": {"status": "error", "errors": ["HTTP 530"]}}
        write_json(bronze / "metadata.json", metadata)
        return bronze, metadata, origin

    def ingest(self, bronze, metadata, **kwargs):
        return ingest_current_recordings(bronze, bronze.name, metadata, self.client,
                                         workspace="fixture-workspace", **kwargs)

    def test_native_meeting_sends_transcript_then_synthesis(self):
        bronze, metadata = self.native()
        result = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(result["status"], "ok")
        sent = list(self.client.requests.values())
        self.assertEqual([r["envelope"]["fidelidade"] for r in sent], ["projecao", "sintese"])
        self.assertEqual(sent[0]["envelope"]["texto"], "Transcrição integral 🌰")
        self.assertEqual(sent[1]["envelope"]["source_id"],
                         "castanha:" + hashlib.sha256(b"silver|native-1").hexdigest())
        self.assertEqual(len(sent[1]["facts"]), 2)
        self.assertEqual((result["facts_ingested"], result["facts_descartados"], result["facts_status"]), (2, 3, "ok"))
        revisions = result["source"]["revisions"]
        self.assertEqual([r.get("synthesis", False) for r in revisions], [False, True])
        self.assertEqual({r["status"] for r in revisions}, {"ok"})
        # Nada pendente depois; um Silver novo é revisão nova.
        metadata = {**metadata, "zinom": result}
        self.assertFalse(bronze_needs_sync(bronze, bronze.name, metadata, workspace="fixture-workspace",
                                           endpoint=FakeMcp.endpoint, token=FakeMcp.token,
                                           silver_text=SILVER, gold=GOLD))
        self.assertTrue(bronze_needs_sync(bronze, bronze.name, metadata, workspace="fixture-workspace",
                                          endpoint=FakeMcp.endpoint, token=FakeMcp.token,
                                          silver_text=SILVER.replace("## 📝", "corrigido\n\n## 📝"), gold=GOLD))

    def test_retry_reuses_the_frozen_synthesis_request(self):
        bronze, metadata = self.native()
        self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(len(self.client.requests), 2, "mesmo Silver e fatos: mesma chave, sem duplicar")
        self.assertEqual(len([p for p in (bronze / ".brain-ingest").glob("*.json")
                              if p.name != "destination.json"]), 2)

    def test_without_notes_only_the_transcript_goes(self):
        bronze, metadata = self.native()
        result = self.ingest(bronze, metadata)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([r["envelope"]["fidelidade"] for r in self.client.requests.values()], ["projecao"])
        self.assertEqual(result["facts_ingested"], 0)

    def test_legacy_meeting_sends_only_the_synthesis_from_delivered_origins(self):
        bronze, metadata, origin = self.legacy()
        self.assertEqual(legacy_origin_ids(bronze), [origin])
        result = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(result["status"], "ok")
        sent = list(self.client.requests.values())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["envelope"]["fidelidade"], "sintese")
        self.assertEqual(sent[0]["envelope"]["source_id"],
                         "castanha:" + hashlib.sha256(f"silver|{origin}".encode()).hexdigest())
        self.assertEqual(result["facts_ingested"], 2)
        self.assertTrue((bronze / ".brain-ingest" / "destination.json").exists())

    def test_tombstoned_synthesis_does_not_block_the_meeting(self):
        # Refutação de 07/09: apagar a síntese no portal deixava a reunião em erro para sempre.
        bronze, metadata = self.native()
        first = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        key = next(k for k, r in self.client.requests.items() if r["envelope"]["fidelidade"] == "sintese")
        self.client.states[key] = "tombstoned"
        metadata = {**metadata, "zinom": first}
        second = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(second["status"], "tombstoned")
        metadata = {**metadata, "zinom": second}
        third = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(third["status"], "tombstoned", third)
        self.assertNotIn("error", {r["status"] for r in third["source"]["revisions"]})
        # Gravação nova da mesma reunião ainda sobe.
        write_json(bronze / ".jobs" / "native-2.json", {
            "id": "native-2", "stage": "done", "transcript": "Segunda gravação",
            "provider": "groq", "recorded_at": "2026-09-05T11:30:00-03:00"})
        metadata = {**metadata, "zinom": third,
                    "memory_recording_ids": ["capture.ogg", "capture-2.ogg"],
                    "recordings": metadata["recordings"] + [{"id": "capture-2.ogg", "job_id": "native-2",
                                                             "transcription_provider": "groq"}]}
        self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertIn("Segunda gravação", [r["envelope"]["texto"] for r in self.client.requests.values()])
        self.assertEqual(sum(r["envelope"]["fidelidade"] == "sintese"
                             for r in self.client.requests.values()), 1,
                         "origens novas não autorizam recriar síntese esquecida")

    def test_forgotten_transcript_blocks_later_synthesis_but_not_new_recordings(self):
        bronze, metadata = self.native()
        first = self.ingest(bronze, metadata)
        key = next(iter(self.client.requests))
        self.client.states[key] = "tombstoned"
        # A exclusão só é descoberta nesta chamada, quando o Silver fica pronto.
        second = self.ingest(bronze, {**metadata, "zinom": first}, silver_text=SILVER, gold=GOLD)
        self.assertEqual(second["status"], "tombstoned")
        self.assertEqual(len(self.client.requests), 1, "síntese não pode recriar conteúdo esquecido")
        metadata = {**metadata, "zinom": second}
        self.assertFalse(bronze_needs_sync(
            bronze, bronze.name, metadata, workspace="fixture-workspace",
            endpoint=FakeMcp.endpoint, token=FakeMcp.token, silver_text=SILVER, gold=GOLD))
        write_json(bronze / ".jobs" / "native-2.json", {
            "id": "native-2", "stage": "done", "transcript": "Segunda gravação",
            "provider": "groq", "recorded_at": "2026-09-05T11:30:00-03:00"})
        metadata = {**metadata, "memory_recording_ids": ["capture.ogg", "capture-2.ogg"],
                    "recordings": metadata["recordings"] + [{"id": "capture-2.ogg", "job_id": "native-2",
                                                             "transcription_provider": "groq"}]}
        third = self.ingest(bronze, metadata, silver_text=SILVER, gold=GOLD)
        self.assertEqual(third["status"], "tombstoned")
        sent = list(self.client.requests.values())
        self.assertEqual([r["envelope"]["fidelidade"] for r in sent], ["projecao", "projecao"])
        self.assertEqual(sent[-1]["envelope"]["texto"], "Segunda gravação")
        self.assertEqual(third["facts_ingested"], 0)

    def test_legacy_meeting_without_notes_or_delivery_is_refused(self):
        bronze, metadata, _ = self.legacy()
        result = self.ingest(bronze, metadata)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.client.requests, {})
        undelivered = self.root / "bronze" / "pendente" / ".legacy-recovery" / "uploads"
        undelivered.mkdir(parents=True)
        write_json(undelivered / "x.json", {"status": "pending", "request": {"envelope": {"source_id": "castanha:" + "a" * 64}}})
        with self.assertRaises(BronzeIngestError):
            legacy_origin_ids(undelivered.parent.parent)


if __name__ == "__main__":
    unittest.main()


class TestLegacySyncPath(unittest.TestCase):
    """Reunião legada com síntese: a fila re-consulta a síntese e o sync não passa pela recuperação."""

    def setUp(self):
        from unittest.mock import patch
        from castanha.storage import MeetingStorage
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = MeetingStorage(base_dir=Path(self.temp.name))
        self.config = {"zinom": {"enabled": True, "bronze_ingest_enabled": True,
                                  "workspace": "fixture-workspace", "token": FakeMcp.token,
                                  "endpoint": FakeMcp.endpoint},
                       "storage": {"bronze_dir": str(self.storage.bronze_dir)}}
        for target in ("castanha.sync.load_config", "castanha.zinom_adapter.load_config"):
            patcher = patch(target, side_effect=lambda: self.config)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeMcp()
        patcher = patch("castanha.zinom_adapter.ZinomMcpClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_synthesis_attempt_does_not_strand_legacy_recovery(self):
        import subprocess
        from castanha.legacy_recovery import prepare_legacy_manifest, submit_legacy_recovery
        from castanha.sync import pending_candidates, sync_meeting

        slug = "legado-pendente"
        bronze = self.storage.bronze_dir / slug
        bronze.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.2", "-c:a", "libopus",
                        str(bronze / "audio.ogg")], check=True, capture_output=True)
        metadata = {**META, "slug": slug,
                    "recordings": [{"id": "audio.ogg", "transcription_provider": "groq"}]}
        write_json(bronze / "metadata.json", metadata)
        (bronze / "transcript_raw.txt").write_text("Transcrição legada integral", encoding="utf-8")
        original = (bronze / "metadata.json").read_bytes()
        prepare_legacy_manifest(bronze)
        self.client.default_state = "pending"
        first = submit_legacy_recovery(bronze, self.client, workspace="fixture-workspace")
        self.assertEqual(first["status"], "pending")
        failed = ingest_current_recordings(bronze, slug, metadata, self.client,
                                          workspace="fixture-workspace", silver_text=SILVER, gold=GOLD)
        self.assertEqual(failed["status"], "error")
        self.assertTrue((bronze / ".brain-ingest").is_dir())
        self.assertFalse((bronze / ".brain-ingest" / "destination.json").exists())
        self.assertEqual([s for _, s in pending_candidates(self.storage)], [slug])
        self.client.default_state = "completed"
        resumed = sync_meeting(slug, self.storage)
        self.assertEqual(resumed["status"], "ok", resumed)
        self.assertEqual((bronze / "metadata.json").read_bytes(), original)
        self.assertEqual(pending_candidates(self.storage), [])
        self.assertEqual(self.storage.delivery_projection(slug, failed)["status"], "ok")
        self.assertEqual(len(self.client.requests), 1, "recuperação retoma o pedido já enviado")
        # Um checkpoint sem destino não é resíduo vazio e continua bloqueado.
        write_json(bronze / ".brain-ingest" / "checkpoint.json", {})
        with self.assertRaises(BronzeIngestError):
            submit_legacy_recovery(bronze, self.client, workspace="fixture-workspace")
        self.assertEqual((bronze / "metadata.json").read_bytes(), original)

    def test_pending_synthesis_is_requeried_and_settles(self):
        from castanha.sync import pending_candidates, sync_meeting
        slug = "legado"
        bronze = self.storage.bronze_dir / slug
        uploads = bronze / ".legacy-recovery" / "uploads"
        uploads.mkdir(parents=True)
        write_json(uploads / "abc.json", {"status": "ok", "request": {"envelope": {"source_id": "castanha:" + "f" * 64}}})
        metadata = {**META, "slug": slug, "recordings": [{"id": "audio.ogg", "transcription_provider": "groq"}]}
        write_json(bronze / "metadata.json", metadata)
        (self.storage.silver_dir / f"{slug}.md").write_text(SILVER, encoding="utf-8")
        write_json(self.storage.gold_dir / f"{slug}.json", GOLD)
        # Primeira entrega (pelo retry) fica pendente no servidor.
        self.client.default_state = "pending"
        first = ingest_current_recordings(bronze, slug, metadata, self.client, workspace="fixture-workspace",
                                          silver_text=SILVER, gold=GOLD)
        self.assertEqual(first["status"], "pending")
        self.storage.record_zinom_result(slug, first)
        self.assertEqual([s for _, s in pending_candidates(self.storage)], [slug])
        # Servidor concluiu: o sync consulta pela chave e fecha o recibo.
        self.client.default_state = "completed"
        result = sync_meeting(slug, self.storage)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result.get("facts_ingested"), 2)
        self.assertEqual(pending_candidates(self.storage), [])
        self.assertEqual(len(self.client.requests), 1, "só a síntese; a transcrição legada não é reenviada")
        self.assertEqual(self.storage.delivery_projection(slug, {"status": "ok"})["status"], "ok",
                         "com síntese, vale o recibo do metadata, não a projeção legada")
