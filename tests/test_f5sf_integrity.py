"""Regressões F5SS: legado simulado e morte após o último checkpoint.

Só fixtures temporárias, áudio fictício e transporte substituído.
"""
import fcntl
import json
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests import test_durable_jobs as fixtures
from tests.test_durable_jobs import ProcessDeath
from castanha.durability import meeting_lock
from castanha.retry import drain_queue
from castanha.sync import sync_meeting, sync_pending


class TestF5SFIntegrity(unittest.TestCase):
    setUp = fixtures.TestDurableJobs.setUp
    transcription = fixtures.TestDurableJobs.transcription

    def legacy(self, provider='mock', text='SYNTHETIC_MOCK_MUST_NOT_BE_SENT'):
        s = self.engine.storage
        slug = 'legacy-fixture'
        s.save_bronze(slug, self.source, {'slug': slug, 'title': 'Fixture', 'audio_status': 'ok',
                                         'transcription_provider': provider}, text)
        s.save_silver(slug, text)
        s.save_gold(slug, {'summary': text, 'facts': []})
        s.add_recording(slug, self.source, metadata_update={'transcribed': False})
        self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture'
        return slug

    def client(self):
        mocked = patch('castanha.zinom_adapter.ZinomMcpClient')
        client = mocked.start()
        self.addCleanup(mocked.stop)
        client.return_value.call_tool.return_value = {'content': [{'type': 'text', 'text':
            '{"ok":true,"source_id":"conversation:fixture"}'}]}
        return client

    def test_legacy_mock_then_real_never_leaks_and_preserves_provenance(self):
        slug = self.legacy()
        s = self.engine.storage
        bronze = s.bronze_dir / slug
        originals = {p.name: p.read_bytes() for p in bronze.glob('*.ogg')}
        client = self.client()
        with patch.object(self.engine, '_transcribe_with_fallback', return_value=('Texto real', 'groq', None)) as transcribe:
            result = self.engine.reprocess_meeting(slug)
            self.engine.reprocess_meeting(slug)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(result['result']['zinom']['status'], 'ok')
        for value in [s.read_transcript(slug), (s.silver_dir / f'{slug}.md').read_text(),
                      (s.gold_dir / f'{slug}.json').read_text(), str(client.return_value.call_tool.call_args_list)]:
            self.assertNotIn('SYNTHETIC_MOCK_MUST_NOT_BE_SENT', value)
        self.assertEqual(s.read_transcript(slug).count('Texto real'), 1)
        archive = bronze / '.mock-history'
        self.assertEqual((archive / 'transcript_raw.txt').read_text(), 'SYNTHETIC_MOCK_MUST_NOT_BE_SENT')
        self.assertIn('SYNTHETIC_MOCK_MUST_NOT_BE_SENT', (archive / 'silver.md').read_text())
        self.assertEqual(json.loads((archive / 'metadata.json').read_text())['transcription_provider'], 'mock')
        meta = s._read_bronze_metadata(slug)
        self.assertEqual([r['transcription_provider'] for r in meta['recordings']], ['mock', 'groq'])
        self.assertEqual(meta['memory_recording_ids'], ['audio_2.ogg'])
        self.assertEqual({p.name: p.read_bytes() for p in bronze.glob('*.ogg')}, originals)
        self.assertEqual([c.args[0] for c in client.return_value.call_tool.call_args_list], ['remember', 'brain_update'])

    def test_new_mock_retry_cannot_contaminate_or_erase_existing_real_text(self):
        slug = self.legacy('groq', 'Texto real anterior')
        client = self.client()
        with patch.object(self.engine, '_transcribe_with_fallback', return_value=('NOVO_MOCK', 'mock', None)):
            self.engine.reprocess_meeting(slug)
            self.engine.reprocess_meeting(slug)
        s = self.engine.storage
        self.assertEqual(s.read_transcript(slug), 'Texto real anterior')
        self.assertNotIn('NOVO_MOCK', str(client.return_value.call_tool.call_args_list))
        self.assertIn('NOVO_MOCK', (s.bronze_dir / slug / '.mock-history/audio_2.ogg.json').read_text())
        self.assertEqual([r['transcription_provider'] for r in s._read_bronze_metadata(slug)['recordings']], ['groq', 'mock'])

    def test_legacy_mock_failed_real_retry_keeps_only_isolated_mock(self):
        slug = self.legacy()
        client = self.client()
        with patch.object(self.engine, '_transcribe_with_fallback', return_value=('', 'failed', 'fixture unavailable')):
            self.engine.reprocess_meeting(slug)
        s = self.engine.storage
        self.assertEqual(s.read_transcript(slug), '')
        self.assertEqual((s.silver_dir / f'{slug}.md').read_text(), '')
        self.assertTrue((s.bronze_dir / slug / '.mock-history/transcript_raw.txt').exists())
        client.assert_not_called()

    def test_death_before_new_silver_cannot_expose_old_mock_via_sync(self):
        slug = self.legacy()
        client = self.client()
        with patch.object(self.engine, '_transcribe_with_fallback', return_value=('Texto real', 'groq', None)), \
             patch.object(self.engine.summarizer, 'generate_silver', side_effect=ProcessDeath):
            with self.assertRaises(ProcessDeath):
                self.engine.reprocess_meeting(slug)
        with patch('castanha.sync.ZinomAdapter', return_value=self.engine.zinom):
            sync_meeting(slug, self.engine.storage)
        self.assertNotIn('SYNTHETIC_MOCK_MUST_NOT_BE_SENT', str(client.return_value.call_tool.call_args_list))
        with patch.object(self.engine, '_transcribe_with_fallback') as transcribe:
            self.engine.reprocess_meeting(slug)
            transcribe.assert_not_called()
        self.assertIn('Texto real', (self.engine.storage.silver_dir / f'{slug}.md').read_text())

    def finished_orphan(self, delivered=False):
        if delivered:
            self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture'
        actual = self.engine.state_mgr.write
        def die(updates):
            if updates.get('status') == 'idle':
                raise ProcessDeath()
            return actual(updates)
        with patch('castanha.engine.get_transcriber') as provider, \
             patch.object(self.engine.state_mgr, 'write', side_effect=die):
            provider.return_value.transcribe.return_value = self.transcription()
            with self.assertRaises(ProcessDeath):
                self.engine.stop_recording()
        state = self.engine.state_mgr.read()
        slug = state['capture_slug']
        jobs = list((self.engine.storage.bronze_dir / slug / '.jobs').glob('*.json'))
        self.assertEqual([json.loads(p.read_text())['stage'] for p in jobs], ['done'])
        # PID de processo real já recolhido, não ausência presumida de identidade.
        dead = subprocess.Popen(['true'])
        dead.wait()
        self.engine.state_mgr.write({'processing_pid': dead.pid})
        return slug

    def assert_capture_available(self):
        self.assertEqual(self.engine.state_mgr.read()['status'], 'idle')
        with patch.object(self.engine.recorder, 'start') as start, \
             patch('castanha.engine.is_default_source_muted', return_value=False):
            start.return_value.pid = 12345
            self.assertEqual(self.engine.start_recording()['status'], 'recording')

    def test_finished_orphan_reconciles_via_each_entrypoint_without_resending(self):
        client = self.client()
        for delivered in (False, True):
            for route in ('sync', 'all', 'auto'):
                with self.subTest(delivered=delivered, route=route):
                    # Cada cenário começa sem pendências deixadas por outro cenário.
                    self.setUp()
                    self.engine.state_mgr.write({'status': 'recording', 'audio_path': str(self.source),
                                                 'capture_slug': None, 'capture_job_id': None,
                                                 'target_meeting_slug': None, 'current_meeting': {'title': 'Fixture'}})
                    self.engine.zinom.enabled = delivered
                    slug = self.finished_orphan(delivered)
                    client.reset_mock()
                    with patch('castanha.sync.ZinomAdapter', return_value=self.engine.zinom), \
                         patch('castanha.engine.get_transcriber') as provider:
                        if route == 'sync':
                            sync_meeting(slug, self.engine.storage)
                        elif route == 'all':
                            sync_pending(storage=self.engine.storage)
                        else:
                            drain_queue(self.engine.storage, now=100)
                        provider.assert_not_called()
                    client.assert_not_called()
                    self.assert_capture_available()
                    self.engine.state_mgr.write({'status': 'idle'})

    def test_alive_or_unreadable_identity_is_not_reconciled_or_resent(self):
        client = self.client()
        slug = self.finished_orphan(True)
        for pid in (os.getpid(), None, '123', True, -1, 10 ** 100):
            with self.subTest(pid=pid):
                self.engine.state_mgr.write({'processing_pid': pid})
                state_before = self.engine.state_mgr.state_file.read_bytes()
                client.reset_mock()
                sync_meeting(slug, self.engine.storage)
                sync_pending(storage=self.engine.storage)
                drain_queue(self.engine.storage, now=100)
                self.assertEqual(self.engine.state_mgr.state_file.read_bytes(), state_before)
                client.assert_not_called()

    def test_missing_groq_chunk_keeps_job_pending_and_retries_whole_audio(self):
        from castanha.transcription import GroqTranscriber, TranscriptionPending
        client = self.client()
        groq = GroqTranscriber('fixture')
        self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture'
        for invalid in ({}, None, [], {'text': None}, {'text': 42}):
            with self.subTest(payload=invalid):
                self.setUp()
                self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture'
                # Groq agora é secundário explícito, inclusive no replay persistido.
                self.engine.config['transcription'].update(provider='groq', provider_revision=1,
                    fallback_from='vps_ssh', groq_api_key='fixture')
                config_patch = patch('castanha.transcription.load_config', return_value=self.engine.config)
                config_patch.start()
                self.addCleanup(config_patch.stop)
                with patch('castanha.engine.get_transcriber', return_value=groq), \
                     patch('castanha.transcription.GROQ_MAX_FILE_BYTES', 1), \
                     patch('castanha.transcription.chunk_audio', side_effect=lambda path, **kw: [(path, 0), (path, 30)]), \
                     patch.object(GroqTranscriber, '_registrar_consumo'), \
                     patch.object(GroqTranscriber, '_request_groq_with_retry', side_effect=[{'text': 'FIRST_CHUNK'}, invalid]), \
                     patch('castanha.transcription.VpsSshTranscriber.transcribe', side_effect=TranscriptionPending('fixture')):
                    result = self.engine.stop_recording()
                slug = result['result']['slug']
                bronze = self.engine.storage.bronze_dir / slug
                job_file = next((bronze / '.jobs').glob('*.json'))
                job = json.loads(job_file.read_text())
                self.assertEqual(job['stage'], 'pending')
                self.assertEqual(Path(job['audio_path']).read_bytes(), self.source.read_bytes())
                self.assertEqual(result['status'], 'partial')
                client.assert_not_called()
                with patch('castanha.engine.get_transcriber', return_value=groq), \
                     patch('castanha.transcription.GROQ_MAX_FILE_BYTES', 1), \
                     patch('castanha.transcription.chunk_audio', side_effect=lambda path, **kw: [(path, 0), (path, 30)]), \
                     patch.object(GroqTranscriber, '_registrar_consumo'), \
                     patch.object(GroqTranscriber, '_request_groq_with_retry', side_effect=[{'text': 'FIRST_CHUNK'}, {'text': 'SECOND_CHUNK'}]):
                    sync_meeting(slug, self.engine.storage)
                self.assertEqual(json.loads(job_file.read_text())['stage'], 'done')
                self.assertEqual(self.engine.storage.read_transcript(slug), 'FIRST_CHUNK SECOND_CHUNK')

    def test_old_receipt_does_not_suppress_new_content_after_missing_token(self):
        client = self.client()
        self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture'
        fact = {'subject': 'Projeto', 'predicate': 'usa', 'object': 'Python'}
        with patch('castanha.engine.get_transcriber') as provider, \
             patch.object(self.engine.summarizer, 'generate_gold', return_value={'facts': [fact]}):
            provider.return_value.transcribe.return_value = self.transcription('Texto inicial')
            slug = self.engine.stop_recording()['result']['slug']
            old = self.engine.storage._read_bronze_metadata(slug)['zinom']
            self.assertEqual(old['note_status'], 'ok')
            self.assertEqual(len(old['local_content_sha256']), 64)
            self.engine.state_mgr.write({'status': 'recording', 'audio_path': str(self.source),
                                         'target_meeting_slug': slug, 'current_meeting': {'title': 'Fixture'}})
            self.engine.zinom.token = ''
            provider.return_value.transcribe.return_value = self.transcription('Conteúdo novo')
            self.engine.stop_recording()
        pending = self.engine.storage._read_bronze_metadata(slug)['zinom']
        self.assertEqual(pending['remember_id'], old['remember_id'])
        self.assertEqual(pending['facts_pending'], [fact])
        self.assertEqual(pending['note_status'], 'pending')
        self.assertNotEqual(pending['local_content_sha256'], self.engine.storage.delivery_content_sha256(slug))
        # Estado já produzido pela versão anterior: recibo sem hash e status
        # herdado, apesar de a tentativa mais recente ter ficado sem token.
        legacy = self.engine.storage._read_bronze_metadata(slug)
        legacy['zinom'].pop('local_content_sha256')
        legacy['zinom']['note_status'] = 'ok'
        self.engine.storage.write_bronze_metadata(slug, legacy)
        self.engine.zinom.token = 'fixture'
        client.reset_mock()
        with patch('castanha.sync.ZinomAdapter', return_value=self.engine.zinom):
            drain_queue(self.engine.storage, now=100)
            drain_queue(self.engine.storage, now=1000)
            calls = client.return_value.call_tool.call_args_list
            self.assertEqual([call.args[0] for call in calls], ['brain_update'])
            self.assertIn('Conteúdo novo', calls[0].args[1]['text'])
            # Mesmo status de fatos, mas texto alterado: o hash impede suprimir.
            self.engine.storage.save_silver(slug, 'Conteúdo revisado')
            drain_queue(self.engine.storage, now=2000)
            self.assertEqual(client.return_value.call_tool.call_count, 2)
            self.assertIn('Conteúdo revisado', client.return_value.call_tool.call_args.args[1]['text'])

    def test_finished_orphan_with_pending_facts_or_tombstone_never_resends(self):
        client = self.client()
        fact = {'subject': 'Projeto', 'predicate': 'usa', 'object': 'Python'}
        with patch.object(self.engine.summarizer, 'generate_gold', return_value={'facts': [fact]}):
            slug = self.finished_orphan(True)
        client.reset_mock()
        with patch('castanha.sync.ZinomAdapter', return_value=self.engine.zinom):
            sync_meeting(slug, self.engine.storage)
        self.assertEqual(self.engine.state_mgr.read()['status'], 'idle')
        client.assert_not_called()
        self.engine.state_mgr.write({'status': 'processing', 'capture_slug': slug})
        dead = subprocess.Popen(['true'])
        dead.wait()
        self.engine.state_mgr.write({'processing_pid': dead.pid})
        self.engine.storage.record_zinom_result(slug, {'status': 'tombstoned'})
        sync_pending(storage=self.engine.storage)
        self.assertEqual(self.engine.state_mgr.read()['status'], 'idle')
        client.assert_not_called()

    def test_reconciliation_writes_idle_while_holding_meeting_lock(self):
        self.client()
        slug = self.finished_orphan(True)
        bronze = self.engine.storage.bronze_dir / slug
        actual = self.engine.state_mgr.__class__.write
        observed = []
        def checked(manager, updates):
            if updates.get('status') == 'idle':
                with (bronze / '.processing.lock').open('a') as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(True)
            return actual(manager, updates)
        with patch('castanha.state.StateManager.write', new=checked):
            sync_pending(storage=self.engine.storage)
        self.assertEqual(observed, [True])

    def test_identity_rechecked_after_acquiring_lock(self):
        self.client()
        slug = self.finished_orphan(True)
        @contextmanager
        def changed_identity(bronze):
            with meeting_lock(bronze):
                self.engine.state_mgr.write({'processing_pid': os.getpid()})
                yield
        with patch('castanha.sync.meeting_lock', changed_identity):
            sync_pending(storage=self.engine.storage)
        self.assertEqual(self.engine.state_mgr.read()['status'], 'processing')


if __name__ == '__main__':
    unittest.main()
