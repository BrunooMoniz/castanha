"""Realocar gravações: vincular a evento da agenda e mover áudio entre reuniões, com CLI isolada."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha.bronze_ingest import (build_transcript_request, build_synthesis_request, revision_fingerprint,
                                    frozen_destination, bronze_needs_sync)
from castanha.durability import write_json, file_sha256
from castanha.engine import CastanhaEngine
from castanha.library import MeetingLibrary
from castanha.recording_exclusion import ARCHIVE, POINTER, ExclusionError, exclude_recording, restore_recording, _load
from castanha.relocation import (RelocationConflict, RelocationError, event_record, link_meeting_to_event,
                                 move_recording, relink_silver)
from castanha.storage import MeetingStorage
from castanha.summarizer import silver_frontmatter
from castanha.sync import pending_candidates

GOLD = {'facts': [], 'decisions': [], 'action_items': [], 'people_notes': []}
EVENT = {"uid": "weekly_20260912T133000Z", "title": "Weekly do time",
         "start": "2026-09-12T10:30:00-03:00", "end": "2026-09-12T11:30:00-03:00",
         "attendees": [{"name": "Ana", "email": "ana@example.com", "response": "accepted", "organizer": True},
                       {"name": "Bruno", "email": "bruno@example.com", "response": "accepted"}],
         "organizer": "ana@example.com", "conference_url": "https://meet.google.com/abc-defg-hij",
         "calendar_name": "bruno@example.com", "account": "bruno@example.com"}
CLI = str(Path(__file__).resolve().parents[1] / 'bin/castanha')


class RelocationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.base = self.root / 'meetings'
        config = self.root / 'config/castanha'; config.mkdir(parents=True)
        self.zcfg = {'enabled': True, 'token': 'fixture-token', 'bronze_ingest_enabled': True,
                     'endpoint': 'https://fixture.invalid/mcp', 'workspace': 'personal', 'account_id': None}
        (config / 'config.json').write_text(json.dumps({
            'storage': {'base_dir': str(self.base), 'bronze_dir': str(self.base / 'bronze'),
                        'silver_dir': str(self.base / 'silver'), 'gold_dir': str(self.base / 'gold')},
            'llm': {'provider': 'groq', 'api_key': ''}, 'zinom': self.zcfg}))
        for guard in (patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                              'XDG_STATE_HOME': str(self.root / 'state')}),
                      patch('urllib.request.urlopen', side_effect=AssertionError('No live network')),
                      patch('castanha.engine.get_transcriber', side_effect=AssertionError('No ASR for moved audio'))):
            guard.start(); self.addCleanup(guard.stop)
        self.storage = MeetingStorage(self.base)
        self.a = self.meeting('origem', ['job1', 'job2'], title='Origem', minute=1)
        self.b = self.meeting('destino', ['job3'], title='Destino', minute=5)
        self.engine = CastanhaEngine(); self.engine.storage = self.storage
        self.engine.summarizer.generate_silver = Mock(side_effect=lambda meta, text: 'RESUMO NOVO ' + text)
        self.engine.summarizer.generate_gold = Mock(return_value=GOLD)
        self.engine.zinom.ingest_meeting = Mock(return_value={'status': 'ok'})

    def meeting(self, slug, job_ids, *, title, minute):
        bronze = self.storage.bronze_dir / slug; bronze.mkdir(); (bronze / '.jobs').mkdir()
        records = []
        for offset, job_id in enumerate(job_ids):
            audio = bronze / f'capture_{job_id}.ogg'; audio.write_bytes(f'original audio {job_id}'.encode())
            sha = file_sha256(audio)
            recorded = f'2026-09-11T10:{minute + offset:02d}:00+00:00'
            records.append({'id': audio.name, 'filename': audio.name, 'job_id': job_id, 'sha256': sha,
                            'transcribed': True, 'transcription_provider': 'fixture', 'audio_status': 'ok',
                            'duration_seconds': 10, 'recorded_at': recorded})
            write_json(bronze / '.jobs' / f'{job_id}.json', {
                'id': job_id, 'audio_path': str(audio), 'sha256': sha, 'stage': 'done', 'recorded_at': recorded,
                'provider': 'fixture', 'transcript': f'CONTEUDO {job_id}', 'duration_seconds': 10, 'audio_status': 'ok',
                'utterances': [], 'state': {'mode': 'dual', 'current_meeting': {'title': title}}})
            (bronze / '.channels' / sha).mkdir(parents=True)
            (bronze / '.channels' / sha / 'channel-0.json').write_text('{"fixture": true}')
            (bronze / '.providers').mkdir(exist_ok=True)
            (bronze / '.providers' / f'{sha}.json').write_text('{"selected_provider": "fixture"}')
        metadata = {'slug': slug, 'title': title, 'recorded_at': f'2026-09-11T10:{minute:02d}:00+00:00',
                    'recordings': records, 'recording_revision': 0, 'mode': 'dual', 'audio_status': 'ok',
                    'processing_status': 'complete', 'transcription_provider': 'fixture',
                    'memory_recording_ids': [r['id'] for r in records],
                    'calendar_event': {'title': title, 'start': f'2026-09-11T10:{minute:02d}:00+00:00', 'attendees': []},
                    'zinom': {'status': 'ok'}}
        write_json(bronze / 'metadata.json', metadata)
        (bronze / 'transcript_raw.txt').write_text('\n\n'.join(f'CONTEUDO {j}' for j in job_ids))
        (bronze / '.jobs' / 'base_transcript.txt').write_text('')
        self.storage.save_silver(slug, silver_frontmatter(metadata) + f'# {title}\n\n## 📌 Resumo Executivo\n'
                                 f'Sem resumo: teste. Convidados (presença não confirmada): Não identificados.\n\nRESUMO ANTIGO {slug}\n')
        self.storage.save_gold(slug, {'title': title, **GOLD})
        return slug

    def receipts(self, slug):
        """Recibos de entrega ao Zinom, como os que uma reunião já sincronizada tem."""
        bronze = self.storage.bronze_dir / slug
        metadata = json.loads((bronze / 'metadata.json').read_text())
        directory = bronze / '.brain-ingest'; directory.mkdir()
        write_json(directory / 'destination.json', frozen_destination(endpoint=self.zcfg['endpoint'],
                   token=self.zcfg['token'], workspace='personal'))
        for rec in metadata['recordings']:
            request = build_transcript_request(slug, {**metadata, 'recordings': [rec]}, 'CONTEUDO ' + rec['job_id'],
                                               captured_at='2026-09-11T12:00:00+00:00', workspace='personal',
                                               recording_id=rec['job_id'])
            self.receipt(directory, request)
        synth = build_synthesis_request(slug, metadata, 'RESUMO ANTIGO', GOLD, captured_at='2026-09-11T12:00:00+00:00',
                                        workspace='personal', origin_ids=[r['job_id'] for r in metadata['recordings']])
        self.receipt(directory, synth)

    @staticmethod
    def receipt(directory, request):
        write_json(directory / (revision_fingerprint(request) + '.json'), {
            'request': request, 'status': 'ok', 'attempted': True,
            'remote_identity': {'job_id': 10, 'revision_id': 11}, 'result': {'status': 'ok'}})

    def metadata(self, slug):
        return json.loads((self.storage.bronze_dir / slug / 'metadata.json').read_text())

    def jobs(self, slug):
        return {p.stem: json.loads(p.read_text()) for p in (self.storage.bronze_dir / slug / '.jobs').glob('*.json')}

    def operation(self, slug):
        from castanha.annotations import _directory
        with _directory(self.storage, slug) as fd:
            return _load(fd)

    def move(self, **kwargs):
        return move_recording(self.a, 'capture_job1.ogg', storage=self.storage, **kwargs)


class TestMoveRecording(RelocationFixture):
    def test_move_keeps_bytes_and_transcript_with_new_identity_in_destination(self):
        original_sha = file_sha256(self.storage.bronze_dir / self.a / 'capture_job1.ogg')
        result = self.move(to=self.b, expected_revision=0)
        self.assertEqual(result['status'], 'ok')
        dest = result['destination']
        self.assertEqual(dest['slug'], self.b); self.assertFalse(dest['created'])
        new_id = dest['job_id']
        self.assertNotEqual(new_id, 'job1')
        jobs = self.jobs(self.b)
        moved = jobs[new_id]
        self.assertEqual(moved['stage'], 'transcribed')  # o daemon refaz o conteúdo sem novo ASR
        self.assertEqual(moved['transcript'], 'CONTEUDO job1')
        self.assertEqual(Path(moved['audio_path']).parent, self.storage.bronze_dir / self.b)
        self.assertEqual(file_sha256(Path(moved['audio_path'])), original_sha)
        self.assertEqual(moved['moved_from'], {**moved['moved_from'], 'slug': self.a, 'job_id': 'job1', 'filename': 'capture_job1.ogg'})
        self.assertEqual(moved['state']['current_meeting']['title'], 'Destino')
        self.assertEqual(jobs['job3']['stage'], 'done')
        self.assertTrue((self.storage.bronze_dir / self.b / '.channels' / original_sha / 'channel-0.json').exists())
        self.assertTrue((self.storage.bronze_dir / self.b / '.providers' / f'{original_sha}.json').exists())
        meta_b = self.metadata(self.b)
        self.assertEqual([r['job_id'] for r in meta_b['recordings']], ['job3', new_id])
        self.assertEqual(meta_b['processing_status'], 'pending')
        self.assertEqual(meta_b['recording_revision'], 1)
        # Origem: quarentena, sem desfazer, reprocessamento pedido.
        self.assertFalse((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['slug'], self.b); self.assertEqual(op['moved_to']['job_id'], new_id)
        self.assertTrue(op['reprocess_requested'])
        self.assertEqual(file_sha256(self.storage.bronze_dir / self.a / ARCHIVE / op['id'] / 'audio'), original_sha)
        meta_a = self.metadata(self.a)
        self.assertFalse(meta_a['can_restore']); self.assertIn('movida', meta_a['restore_reason'])
        self.assertEqual(meta_a['content_status'], 'rebuilding')
        self.assertEqual([r['job_id'] for r in meta_a['recordings']], ['job2'])
        self.assertFalse(result['can_restore'])

    def test_after_move_daemon_rebuilds_destination_with_moved_text_and_origin_without_it(self):
        self.move(to=self.b)
        candidates = [slug for _, slug in pending_candidates(self.storage)]
        self.assertEqual(sorted(candidates), [self.b, self.a])
        rebuilt = self.engine.process_pending(self.b)
        self.assertEqual(rebuilt['status'], 'success', rebuilt)
        self.assertEqual(self.storage.read_transcript(self.b), 'CONTEUDO job1\n\nCONTEUDO job3')  # ordem de gravação
        self.assertTrue(all(job['stage'] == 'done' for job in self.jobs(self.b).values()))
        self.assertIn('RESUMO NOVO CONTEUDO job1', (self.storage.silver_dir / f'{self.b}.md').read_text())
        self.assertEqual(self.metadata(self.b)['processing_status'], 'complete')
        origin = self.engine.process_pending(self.a)
        self.assertEqual(origin['status'], 'success', origin)
        self.assertEqual(self.storage.read_transcript(self.a), 'CONTEUDO job2')
        self.assertEqual(self.metadata(self.a)['content_status'], 'current')
        self.assertNotIn('CONTEUDO job1', (self.storage.silver_dir / f'{self.a}.md').read_text())
        # Nenhuma das duas continua devendo exclusão ou job: o que sobra na fila
        # é só a entrega (o ingest aqui é simulado e não deixa recibo).
        from castanha.recording_exclusion import needs_resume
        self.assertFalse(needs_resume(self.a, self.storage))
        self.assertTrue(all(job['stage'] == 'done' for job in self.jobs(self.a).values()))

    def test_move_with_receipts_leaves_origin_cleanup_pending_and_destination_needing_sync(self):
        self.receipts(self.a); self.receipts(self.b)
        result = self.move(to=self.b)
        self.assertEqual(result['cleanup_status'], 'pending')
        op = self.operation(self.a)
        sources = {t['source_id'] for t in op['targets']}
        self.assertEqual(len(sources), 2)  # transcrição do job movido + síntese antiga da origem
        meta_b = self.metadata(self.b)
        self.assertTrue(bronze_needs_sync(self.storage.bronze_dir / self.b, self.b, meta_b, workspace='personal',
                                          endpoint=self.zcfg['endpoint'], token=self.zcfg['token'],
                                          silver_text='RESUMO ANTIGO', gold=GOLD))
        # A identidade nova do job não é nenhuma fonte tombstonada da origem.
        new_id = result['destination']['job_id']
        new_source = build_transcript_request(self.b, {**meta_b, 'recordings': [meta_b['recordings'][-1]],
                                                       'processing_status': 'complete'}, 'CONTEUDO job1',
                                              captured_at='2026-09-12T12:00:00+00:00', workspace='personal',
                                              recording_id=new_id)['envelope']['source_id']
        self.assertNotIn(new_source, sources)

    def test_restore_is_refused_after_move_and_quarantine_stays(self):
        result = self.move(to=self.b)
        with self.assertRaisesRegex(ExclusionError, 'movida'):
            restore_recording(self.a, result['exclusion_id'], self.storage)
        self.assertTrue((self.storage.bronze_dir / self.a / ARCHIVE / result['exclusion_id'] / 'audio').exists())
        self.assertFalse((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_moving_the_only_recording_leaves_origin_empty_without_reprocess(self):
        only = self.meeting('solo', ['job9'], title='Solo', minute=20)
        result = move_recording(only, 'capture_job9.ogg', to=self.b, storage=self.storage)
        self.assertEqual(result['remaining_count'], 0)
        self.assertEqual(self.metadata(only)['content_status'], 'empty')
        self.assertFalse(self.operation(only).get('reprocess_requested'))
        self.assertEqual(len(self.metadata(self.b)['recordings']), 2)

    def test_move_to_new_meeting_creates_it_with_title_and_agenda_event(self):
        result = self.move(new_title='Weekly do time', event=EVENT)
        dest = result['destination']
        self.assertTrue(dest['created'])
        self.assertTrue(dest['slug'].startswith('2026-09-11_'), dest['slug'])  # dia da gravação, não de hoje
        self.assertIn('weekly-do-time', dest['slug'])
        meta = self.metadata(dest['slug'])
        self.assertEqual(meta['title'], 'Weekly do time')
        self.assertEqual(meta['calendar_event']['uid'], EVENT['uid'])
        self.assertEqual([a['name'] for a in meta['calendar_event']['attendees']], ['Ana', 'Bruno'])
        self.assertEqual(meta['processing_status'], 'pending')
        self.assertEqual(len(meta['recordings']), 1)
        rebuilt = self.engine.process_pending(dest['slug'])
        self.assertEqual(rebuilt['status'], 'success', rebuilt)
        self.assertEqual(self.storage.read_transcript(dest['slug']), 'CONTEUDO job1')

    def test_guards_refuse_without_touching_anything(self):
        def listing():  # a trava da pasta pode ser criada por quem só conferiu e recusou
            return sorted(p.name for p in (self.storage.bronze_dir / self.a).iterdir() if p.name != '.processing.lock')
        before = listing()
        cases = [
            (dict(to=self.a), 'mesma reunião'),
            (dict(to='inexistente'), 'destino não encontrada'),
            (dict(), 'Informe a reunião de destino'),
            (dict(to=self.b, new_title='x'), 'Informe a reunião de destino'),
            (dict(new_title='   '), 'Título da nova reunião vazio'),
            (dict(to='../fora'), 'Identidade da reunião inválida'),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(RelocationError, message):
                self.move(**kwargs)
        with self.assertRaisesRegex(RelocationError, 'Nome da gravação inválido'):
            move_recording(self.a, '../capture_job1.ogg', to=self.b, storage=self.storage)
        with self.assertRaisesRegex(RelocationError, 'não encontrada ou ambígua'):
            move_recording(self.a, 'capture_nada.ogg', to=self.b, storage=self.storage)
        with self.assertRaisesRegex(RelocationConflict, 'recarregue'):
            self.move(to=self.b, expected_revision=7)
        self.assertEqual(listing(), before)
        self.assertFalse((self.storage.bronze_dir / self.a / POINTER).exists())
        self.assertEqual(len(self.metadata(self.b)['recordings']), 1)

    def test_untranscribed_recording_cannot_move_yet(self):
        job = self.jobs(self.a)['job1']; job.update(stage='pending', transcript='')
        write_json(self.storage.bronze_dir / self.a / '.jobs' / 'job1.json', job)
        with self.assertRaisesRegex(RelocationError, 'Aguarde a transcrição'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_destination_with_pending_remote_cleanup_is_refused(self):
        self.receipts(self.b)
        self.b = self.meeting('destino2', ['job7', 'job8'], title='Destino 2', minute=30)
        self.receipts(self.b)
        exclude_recording(self.b, 'capture_job7.ogg', self.storage)
        self.assertEqual(self.metadata(self.b)['cleanup_status'], 'pending')
        with self.assertRaisesRegex(RelocationError, 'reunião de destino ainda tem uma alteração'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_capture_in_progress_blocks_origin_and_destination(self):
        from castanha.state import StateManager
        StateManager().write({'status': 'recording', 'capture_slug': self.b})
        with self.assertRaisesRegex(RelocationError, 'Aguarde a gravação'):
            self.move(to=self.b)
        StateManager().write({'status': 'recording', 'capture_slug': self.a})
        with self.assertRaisesRegex(RelocationError, 'Aguarde a gravação'):
            self.move(to=self.b)

    def test_legacy_destination_is_refused_because_bronze_delivery_would_block(self):
        legado = self.storage.bronze_dir / 'legado'; legado.mkdir()
        (legado / 'audio.ogg').write_bytes(b'legacy audio')
        (legado / 'transcript_raw.txt').write_text('TEXTO LEGADO DO DESTINO')
        write_json(legado / 'metadata.json', {'slug': 'legado', 'title': 'Legado', 'recorded_at': '2026-09-01T10:00:00+00:00',
            'recordings': [{'id': 'audio.ogg', 'filename': 'audio.ogg', 'transcribed': True, 'transcription_provider': 'fixture',
                            'audio_status': 'ok', 'duration_seconds': 5}],
            'recording_revision': 0, 'mode': 'dual', 'audio_status': 'ok', 'processing_status': 'complete',
            'transcription_provider': 'fixture', 'zinom': {'status': 'ok'}})
        self.storage.save_silver('legado', 'RESUMO LEGADO'); self.storage.save_gold('legado', {'title': 'Legado', **GOLD})
        with self.assertRaisesRegex(RelocationError, 'legada'):
            self.move(to='legado')
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())
        self.assertEqual((legado / 'transcript_raw.txt').read_text(), 'TEXTO LEGADO DO DESTINO')

    def test_destination_symlinked_to_origin_is_refused_before_any_lock(self):
        (self.storage.bronze_dir / 'zalias').symlink_to(self.storage.bronze_dir / self.a)
        with self.assertRaisesRegex(RelocationError, 'destino não encontrada'):
            self.move(to='zalias')
        (self.storage.bronze_dir / 'zalias').unlink()
        (self.storage.bronze_dir / 'zalias').mkdir()
        (self.storage.bronze_dir / 'zalias' / 'x').symlink_to(self.storage.bronze_dir / self.a)  # pasta real, sem metadata
        with self.assertRaisesRegex(RelocationError, 'Metadados da reunião de destino'):
            self.move(to='zalias')
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_destination_retired_from_zinom_is_refused(self):
        meta = self.metadata(self.b); meta['zinom'] = {'status': 'tombstoned'}
        write_json(self.storage.bronze_dir / self.b / 'metadata.json', meta)
        with self.assertRaisesRegex(RelocationError, 'retirada do Zinom'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_new_destination_starts_with_empty_base_transcript(self):
        dest = self.move(new_title='Conversa avulsa')['destination']['slug']
        self.assertEqual((self.storage.bronze_dir / dest / '.jobs' / 'base_transcript.txt').read_text(), '')
        self.engine.process_pending(dest)
        self.assertEqual(self.storage.read_transcript(dest), 'CONTEUDO job1')

    def test_move_interrupted_before_attach_refuses_restore_and_is_finished_by_the_daemon(self):
        from castanha.recording_exclusion import needs_resume
        with patch('castanha.relocation._attach', side_effect=RuntimeError('queda de energia')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['phase'], 'attaching')
        self.assertEqual(len(self.metadata(self.b)['recordings']), 1)  # destino intocado
        with self.assertRaisesRegex(ExclusionError, 'movida'):
            restore_recording(self.a, op['id'], self.storage)
        self.assertTrue(needs_resume(self.a, self.storage))
        self.assertIn(self.a, [slug for _, slug in pending_candidates(self.storage)])
        resumed = self.engine.process_pending(self.a)  # o que o daemon faz na fila
        self.assertEqual(resumed['status'], 'success', resumed)
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['phase'], 'attached'); self.assertTrue(op['reprocess_requested'])
        jobs_b = self.jobs(self.b)
        self.assertIn(op['moved_to']['job_id'], jobs_b)
        self.assertEqual(jobs_b[op['moved_to']['job_id']]['transcript'], 'CONTEUDO job1')
        self.assertEqual([r['job_id'] for r in self.metadata(self.b)['recordings']], ['job3', op['moved_to']['job_id']])
        self.assertEqual(self.storage.read_transcript(self.a), 'CONTEUDO job2')
        self.assertFalse(self.metadata(self.a)['can_restore'])

    def test_move_interrupted_after_attach_is_finished_without_duplicating(self):
        from castanha.relocation import resume_move
        with patch('castanha.relocation._finish_origin', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        self.assertEqual(len(self.metadata(self.b)['recordings']), 2)
        self.assertEqual(self.operation(self.a)['moved_to']['phase'], 'attaching')
        self.assertTrue(resume_move(self.a, self.storage))
        self.assertFalse(resume_move(self.a, self.storage))  # nada mais a concluir
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['phase'], 'attached'); self.assertTrue(op['reprocess_requested'])
        self.assertEqual(len(self.metadata(self.b)['recordings']), 2)
        self.assertEqual(len(list((self.storage.bronze_dir / self.b).glob('capture_*.ogg'))), 2)
        self.assertFalse(self.metadata(self.a)['can_restore'])
        self.assertEqual(self.metadata(self.a)['content_status'], 'rebuilding')

    def test_destination_changed_between_check_and_attach_is_refused_under_its_lock(self):
        from castanha import recording_exclusion
        # Destino que ordena DEPOIS da origem: a trava dele só é tomada após a exclusão.
        dest = self.meeting('zdestino', ['job7', 'job8'], title='Z Destino', minute=40)
        self.receipts(dest)
        real = recording_exclusion._exclude_locked
        def racy(fd, storage, slug, filename, expected_revision=None):
            if slug == self.a:
                # A trava global de mutação já impede outra sessão de excluir no destino
                # durante o movimento (ela receberia "outra captura em andamento"); o
                # cenário aqui usa o núcleo interno para provar a revalidação sob a trava.
                from castanha.annotations import _directory
                with _directory(self.storage, dest, lock=True) as dfd:
                    real(dfd, self.storage, dest, 'capture_job7.ogg', None)
            return real(fd, storage, slug, filename, expected_revision)
        with patch('castanha.recording_exclusion._exclude_locked', side_effect=racy):
            with self.assertRaisesRegex(RelocationError, 'reunião de destino ainda tem uma alteração'):
                self.move(to=dest)
        op = self.operation(self.a)
        self.assertNotIn('moved_to', op)  # origem excluída, mas ainda restaurável
        self.assertEqual(self.metadata(dest)['cleanup_status'], 'pending')
        self.assertEqual([r['job_id'] for r in self.metadata(dest)['recordings']], ['job8'])  # nada anexado
        restored = restore_recording(self.a, op['id'], self.storage)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_resume_refuses_destination_that_changed_meanwhile(self):
        from castanha.recording_exclusion import applicable
        from castanha.relocation import resume_move
        self.receipts(self.b)
        with patch('castanha.relocation._attach', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        exclude_recording(self.b, 'capture_job3.ogg', self.storage)  # limpeza remota pendente no destino
        revisao = self.metadata(self.b)['recording_revision']
        with self.assertRaisesRegex(RelocationError, 'reunião de destino ainda tem uma alteração'):
            resume_move(self.a, self.storage)
        self.assertEqual(self.metadata(self.b)['recording_revision'], revisao)  # destino intocado
        self.assertTrue(applicable(self.b, self.storage))  # a exclusão do destino continua retomável
        self.assertEqual(self.operation(self.a)['moved_to']['phase'], 'attaching')  # e o movimento, pendente

    def test_pending_move_blocks_new_capture_and_survives_a_revision_bump(self):
        from castanha.recording_exclusion import applicable, capture_blocked
        with patch('castanha.relocation._attach', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        self.assertTrue(capture_blocked(self.a, self.storage))
        meta = self.metadata(self.a); meta['recording_revision'] += 1  # como uma captura nova faria
        write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        self.assertTrue(applicable(self.a, self.storage))
        self.assertIn(self.a, [slug for _, slug in pending_candidates(self.storage)])
        self.engine.process_pending(self.a)
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['phase'], 'attached')
        self.assertIn(op['moved_to']['job_id'], self.jobs(self.b))

    def test_move_waits_for_the_global_mutation_gate(self):
        import fcntl
        from castanha.capture_gate import open_lock
        from castanha.config import get_state_dir
        get_state_dir().mkdir(parents=True, exist_ok=True)
        with open_lock(get_state_dir()) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.move(to=self.b)
        self.assertEqual(result['status'], 'error'); self.assertIn('outra captura', result['message'])
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_resume_does_not_resurrect_audio_the_user_excluded_from_the_destination(self):
        from castanha.relocation import resume_move
        with patch('castanha.relocation._finish_origin', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        new_id = self.operation(self.a)['moved_to']['job_id']
        exclude_recording(self.b, f'capture_{new_id}.ogg', self.storage)  # ele tirou o áudio movido do destino
        self.assertIn(new_id, self.metadata(self.b)['removed_job_ids'])
        self.assertTrue(resume_move(self.a, self.storage))
        self.assertFalse((self.storage.bronze_dir / self.b / f'capture_{new_id}.ogg').exists())
        self.assertNotIn(new_id, self.jobs(self.b))
        op = self.operation(self.a)
        self.assertEqual(op['moved_to']['phase'], 'attached'); self.assertTrue(op['moved_to']['removed_at_destination'])
        self.assertFalse(self.metadata(self.a)['can_restore'])

    def test_origin_with_legacy_zinom_note_cannot_be_moved(self):
        meta = self.metadata(self.a); meta['zinom'] = {'status': 'ok', 'remember_id': 'nota-1'}
        write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        with self.assertRaisesRegex(RelocationError, 'nota legada'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())
        self.assertFalse((self.storage.bronze_dir / self.a / POINTER).exists())

    def test_crash_between_job_and_destination_metadata_still_bumps_revision_on_resume(self):
        from castanha.relocation import resume_move
        real = self.storage.write_bronze_metadata
        def falha_no_destino(slug, meta):
            if slug == self.b:
                raise RuntimeError('queda depois do job')
            return real(slug, meta)
        with patch.object(self.storage, 'write_bronze_metadata', side_effect=falha_no_destino):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        new_id = self.operation(self.a)['moved_to']['job_id']
        self.assertIn(new_id, self.jobs(self.b)); self.assertFalse(self.jobs(self.b)[new_id]['moved_from'].get('committed'))
        self.assertEqual(self.metadata(self.b)['recording_revision'], 0)
        # O daemon processa o destino antes da retomada: `recordings` ganha o job, a revisão não.
        self.engine.process_pending(self.b)  # o engine ordena por hora de gravação
        self.assertEqual(sorted(r['job_id'] for r in self.metadata(self.b)['recordings']), sorted(['job3', new_id]))
        self.assertEqual(self.metadata(self.b)['recording_revision'], 0)
        self.assertEqual(self.jobs(self.b)[new_id]['stage'], 'done')  # o daemon já consolidou o job
        self.assertTrue(resume_move(self.a, self.storage))
        meta_b = self.metadata(self.b)
        self.assertEqual(meta_b['recording_revision'], 1)
        self.assertEqual(sorted(r['job_id'] for r in meta_b['recordings']), sorted(['job3', new_id]))  # sem duplicar
        self.assertTrue(self.jobs(self.b)[new_id]['moved_from']['committed'])
        # Processamento já concluído não é reaberto: senão a fila não reprocessa
        # (jobs todos done) e a entrega recusa o metadata pendente, para sempre.
        from castanha.sync import meeting_needs_sync
        self.assertEqual(meta_b['processing_status'], 'complete')
        self.assertFalse(meeting_needs_sync(meta_b))
        self.assertTrue(all(job['stage'] == 'done' for job in self.jobs(self.b).values()))

    def test_content_retired_from_zinom_cannot_be_moved_under_a_new_identity(self):
        import hashlib
        meta = self.metadata(self.a); meta['zinom'] = {'status': 'tombstoned'}
        write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        with self.assertRaisesRegex(RelocationError, 'retirado do Zinom'):
            self.move(to=self.b)
        meta['zinom'] = {'status': 'ok'}; write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        self.receipts(self.a)
        source = 'castanha:' + hashlib.sha256(b'job1').hexdigest()
        for path in (self.storage.bronze_dir / self.a / '.brain-ingest').glob('*.json'):
            if path.name == 'destination.json': continue
            saved = json.loads(path.read_text())
            if saved['request']['envelope']['source_id'] == source:
                saved['status'] = 'tombstoned'; saved['result'] = {'status': 'tombstoned', 'error_type': 'ZinomError'}; write_json(path, saved)
        with self.assertRaisesRegex(RelocationError, 'retirada do Zinom'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())
        move_recording(self.a, 'capture_job2.ogg', to=self.b, storage=self.storage)  # a outra gravação, viva, move

    def test_terminal_evidence_recorded_before_checkpoint_update_still_blocks_the_move(self):
        import hashlib
        from castanha.bronze_ingest import TERMINAL_EVIDENCE
        self.receipts(self.a)
        source = 'castanha:' + hashlib.sha256(b'job1').hexdigest()
        directory = self.storage.bronze_dir / self.a / '.brain-ingest'
        for path in directory.glob('*.json'):
            if path.name == 'destination.json': continue
            saved = json.loads(path.read_text())
            if saved['request']['envelope']['source_id'] == source:
                # Tombstone já na evidência independente, checkpoint ainda "ok": estado inconsistente.
                write_json(directory / TERMINAL_EVIDENCE, {path.name: {'source_id': source,
                           'idempotency_key': saved['request']['idempotency_key'], 'result': {'status': 'tombstoned'}}})
        with self.assertRaisesRegex(RelocationError, 'inconsistentes'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_pending_move_blocks_another_exclusion_in_the_origin(self):
        with patch('castanha.relocation._attach', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        antes = self.operation(self.a)['id']
        with self.assertRaisesRegex(ExclusionError, 'movimento pendente'):
            exclude_recording(self.a, 'capture_job2.ogg', self.storage)
        self.assertEqual(self.operation(self.a)['id'], antes)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job2.ogg').exists())

    def test_crash_after_job_before_audio_leaves_nothing_excludable_and_resume_completes(self):
        from castanha import relocation
        from castanha.relocation import resume_move
        real = relocation.atomic_write
        def sem_audio(path, content):
            if Path(path).name.startswith('capture_') and Path(path).parent.name == self.b:
                raise RuntimeError('queda antes do áudio')
            return real(path, content)
        with patch('castanha.relocation.atomic_write', side_effect=sem_audio):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        new_id = self.operation(self.a)['moved_to']['job_id']
        self.assertIn(new_id, self.jobs(self.b))
        self.assertFalse((self.storage.bronze_dir / self.b / f'capture_{new_id}.ogg').exists())
        with self.assertRaises((ExclusionError, FileNotFoundError, OSError)):
            exclude_recording(self.b, f'capture_{new_id}.ogg', self.storage)  # nada a excluir sem o arquivo
        self.assertTrue(resume_move(self.a, self.storage))
        self.assertTrue((self.storage.bronze_dir / self.b / f'capture_{new_id}.ogg').exists())
        self.assertTrue(self.jobs(self.b)[new_id]['moved_from']['committed'])
        self.assertEqual([r['job_id'] for r in self.metadata(self.b)['recordings']].count(new_id), 1)

    def test_resume_restarts_when_the_journal_changed_while_waiting_for_locks(self):
        from castanha import recording_exclusion
        from castanha.relocation import resume_move
        outro = self.meeting('zoutro', ['job9'], title='Outro', minute=50)
        with patch('castanha.relocation._attach', side_effect=RuntimeError('queda')):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        real_load = recording_exclusion._load
        chamadas = {'n': 0}
        def stale_then_fresh(fd):
            op = real_load(fd)
            chamadas['n'] += 1
            if chamadas['n'] == 1 and op:  # a leitura sem trava viu um journal antigo, apontando para outro destino
                return {**op, 'moved_to': {**op['moved_to'], 'slug': outro}}
            return op
        with patch('castanha.recording_exclusion._load', side_effect=stale_then_fresh):
            self.assertTrue(resume_move(self.a, self.storage))
        new_id = self.operation(self.a)['moved_to']['job_id']
        self.assertIn(new_id, self.jobs(self.b))          # anexou no destino do journal real
        self.assertNotIn(new_id, self.jobs(outro))        # e não no destino da leitura antiga
        self.assertEqual(len(self.metadata(outro)['recordings']), 1)

    def test_resume_after_daemon_consolidation_keeps_destination_content_current(self):
        from castanha.relocation import resume_move
        # Destino com uma exclusão anterior já concluída e conteúdo atual.
        self.b = self.meeting('destino3', ['job5', 'job6'], title='Destino 3', minute=45)
        exclude_recording(self.b, 'capture_job5.ogg', self.storage)
        self.assertEqual(self.engine.reprocess_meeting(self.b)['status'], 'success')
        self.assertEqual(self.metadata(self.b)['content_status'], 'current')
        real = self.storage.write_bronze_metadata
        def falha_no_destino(slug, meta):
            if slug == self.b: raise RuntimeError('queda depois do job')
            return real(slug, meta)
        with patch.object(self.storage, 'write_bronze_metadata', side_effect=falha_no_destino):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        self.engine.process_pending(self.b)  # o daemon consolida antes da retomada
        self.assertTrue(resume_move(self.a, self.storage))
        meta_b = self.metadata(self.b)
        self.assertEqual(meta_b['content_status'], 'current'); self.assertEqual(meta_b['processing_status'], 'complete')
        self.assertTrue(MeetingLibrary(self.storage).detail(self.b)['meeting']['has_summary'])

    def test_excluding_another_recording_waits_for_the_pending_rebuild(self):
        self.move(to=self.b)  # origem fica 'rebuilding' até o daemon reprocessar
        with self.assertRaisesRegex(ExclusionError, 'reprocessamento pendente'):
            exclude_recording(self.a, 'capture_job2.ogg', self.storage)
        self.engine.process_pending(self.a)
        self.assertEqual(self.metadata(self.a)['content_status'], 'current')
        exclude_recording(self.a, 'capture_job2.ogg', self.storage)  # agora pode

    def test_restorable_exclusion_blocks_move_and_link_until_decided(self):
        exclude_recording(self.a, 'capture_job2.ogg', self.storage)  # sem recibos: restaurável
        self.assertTrue(self.metadata(self.a)['can_restore'])
        with self.assertRaisesRegex(RelocationError, 'ainda restaurável'):
            self.move(to=self.b)
        with self.assertRaisesRegex(RelocationError, 'ainda restaurável'):
            link_meeting_to_event(self.a, EVENT, self.storage)
        restore_recording(self.a, self.operation(self.a)['id'], self.storage)  # o desfazer continua válido
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job2.ogg').exists())
        self.move(to=self.b)

    def test_finish_origin_persists_metadata_before_closing_the_journal(self):
        from castanha import recording_exclusion
        from castanha.relocation import resume_move
        real_save = recording_exclusion._save
        estado = {'attached': 0}
        def falha_ao_fechar(fd, op):
            if (op.get('moved_to') or {}).get('phase') == 'attached':
                estado['attached'] += 1
                if estado['attached'] == 1: raise RuntimeError('queda antes do journal')
            return real_save(fd, op)
        with patch('castanha.recording_exclusion._save', side_effect=falha_ao_fechar):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        self.assertFalse(self.metadata(self.a)['can_restore'])          # metadata já sem "desfazer"
        self.assertEqual(self.operation(self.a)['moved_to']['phase'], 'attaching')  # journal ainda por concluir
        self.assertTrue(resume_move(self.a, self.storage))
        self.assertEqual(self.operation(self.a)['moved_to']['phase'], 'attached')

    def test_moving_the_last_audio_file_still_rebuilds_origin_from_preserved_transcripts(self):
        # `delete-recording` apaga o áudio e guarda a transcrição: a origem não fica vazia.
        self.assertEqual(self.storage.delete_recording(self.a, 'capture_job1.ogg')['status'], 'ok')
        self.assertIn('job1', self.jobs(self.a))
        result = move_recording(self.a, 'capture_job2.ogg', to=self.b, storage=self.storage)
        self.assertEqual(result['remaining_count'], 0)
        op = self.operation(self.a)
        self.assertTrue(op['reprocess_requested']); self.assertEqual(self.metadata(self.a)['content_status'], 'rebuilding')
        self.assertTrue(self.metadata(self.a)['can_reprocess'])  # o botão Reprocessar continua disponível
        rebuilt = self.engine.process_pending(self.a)
        self.assertEqual(rebuilt['status'], 'success', rebuilt)
        self.assertEqual(self.storage.read_transcript(self.a), 'CONTEUDO job1')
        self.assertEqual(self.metadata(self.a)['content_status'], 'current')
        self.assertIn('RESUMO NOVO CONTEUDO job1', (self.storage.silver_dir / f'{self.a}.md').read_text())

    def test_last_job_of_a_meeting_with_legacy_base_text_cannot_be_moved(self):
        (self.storage.bronze_dir / self.a / '.jobs' / 'base_transcript.txt').write_text('TEXTO LEGADO DA ORIGEM')
        self.move(to=self.b)  # ainda sobra job2: pode
        self.engine.process_pending(self.a)
        with self.assertRaisesRegex(RelocationError, 'transcrição legada'):
            move_recording(self.a, 'capture_job2.ogg', to=self.b, storage=self.storage)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job2.ogg').exists())
        self.assertIn('TEXTO LEGADO DA ORIGEM', self.storage.read_transcript(self.a))

    def test_legacy_delete_refuses_an_audio_whose_attach_is_not_committed(self):
        from castanha import relocation
        from castanha.relocation import resume_move
        real = relocation.write_json
        def falha_no_committed(path, value):
            if isinstance(value, dict) and (value.get('moved_from') or {}).get('committed') is True:
                raise RuntimeError('queda antes do committed')
            return real(path, value)
        with patch('castanha.relocation.write_json', side_effect=falha_no_committed):
            with self.assertRaises(RuntimeError):
                self.move(to=self.b)
        new_id = self.operation(self.a)['moved_to']['job_id']
        self.assertEqual(self.jobs(self.b)[new_id]['moved_from']['phase'], 'audio')
        # Apagar agora seria ambíguo para a retomada: recusado, arquivo preservado.
        recusa = self.storage.delete_recording(self.b, f'capture_{new_id}.ogg')
        self.assertEqual(recusa['status'], 'error'); self.assertIn('anexada', recusa['message'])
        self.assertTrue((self.storage.bronze_dir / self.b / f'capture_{new_id}.ogg').exists())
        self.assertTrue(resume_move(self.a, self.storage))
        self.assertTrue(self.jobs(self.b)[new_id]['moved_from']['committed'])
        # Concluído, o apagamento legado vale, e nada o ressuscita.
        self.assertEqual(self.storage.delete_recording(self.b, f'capture_{new_id}.ogg')['status'], 'ok')
        self.assertFalse(resume_move(self.a, self.storage))
        self.assertFalse((self.storage.bronze_dir / self.b / f'capture_{new_id}.ogg').exists())

    def test_move_message_tells_the_truth_about_automatic_resume(self):
        self.assertIn('castanha sync', self.move(to=self.b)['message'])  # fixture sem retry automático
        cfg_path = self.root / 'config/castanha/config.json'
        cfg = json.loads(cfg_path.read_text()); cfg['sync'] = {'auto_retry_enabled': True}; cfg_path.write_text(json.dumps(cfg))
        outro = self.meeting('zorigem2', ['job8'], title='Origem 2', minute=55)
        result = move_recording(outro, 'capture_job8.ogg', to=self.b, storage=self.storage)
        self.assertTrue(result['auto_resume']); self.assertIn('automaticamente', result['message'])

    def test_destination_with_pending_summary_regeneration_is_refused(self):
        write_json(self.storage.bronze_dir / self.b / '.annotations-regeneration.json', {'status': 'pending'})
        with self.assertRaisesRegex(RelocationError, 'resumo em regeneração'):
            self.move(to=self.b)
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job1.ogg').exists())

    def test_legacy_base_transcript_stays_in_origin_and_is_not_copied(self):
        (self.storage.bronze_dir / self.a / '.jobs' / 'base_transcript.txt').write_text('TEXTO LEGADO DA ORIGEM')
        self.move(to=self.b)
        self.assertFalse((self.storage.bronze_dir / self.b / '.jobs' / 'base_transcript.txt').read_text()
                         if (self.storage.bronze_dir / self.b / '.jobs' / 'base_transcript.txt').exists() else False)
        self.engine.process_pending(self.b)
        self.assertNotIn('TEXTO LEGADO', self.storage.read_transcript(self.b))
        self.engine.process_pending(self.a)
        origin_text = self.storage.read_transcript(self.a)
        self.assertIn('TEXTO LEGADO DA ORIGEM', origin_text)
        self.assertNotIn('CONTEUDO job1', origin_text)


class TestLinkMeetingToEvent(RelocationFixture):
    def test_link_updates_metadata_silver_and_gold_without_bumping_revision(self):
        self.receipts(self.a)
        result = link_meeting_to_event(self.a, EVENT, self.storage)
        self.assertEqual(result['status'], 'ok'); self.assertEqual(result['attendees'], 2)
        meta = self.metadata(self.a)
        self.assertEqual(meta['title'], 'Weekly do time')
        self.assertEqual(meta['calendar_event']['uid'], EVENT['uid'])
        self.assertEqual(meta['calendar_event']['conference_url'], EVENT['conference_url'])
        self.assertEqual([a['email'] for a in meta['calendar_event']['attendees']], ['ana@example.com', 'bruno@example.com'])
        self.assertEqual(meta['event_link_history'][0]['previous_title'], 'Origem')
        self.assertEqual(meta['recording_revision'], 0)  # gravações não mudaram
        self.assertEqual(meta['recorded_at'], '2026-09-11T10:01:00+00:00')  # a gravação continua datada de quando foi
        silver = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertTrue(silver.startswith('---\ntitle: "Weekly do time"\n'))
        self.assertIn('  - name: "Ana"\n    email: "ana@example.com"\n', silver)
        self.assertIn('rsvp: "accepted"', silver)
        self.assertEqual(silver.count('\n---\n'), 1)
        self.assertIn('\n# Weekly do time\n', silver)
        self.assertIn('Convidados (presença não confirmada): Ana, Bruno.', silver)
        self.assertIn('RESUMO ANTIGO origem', silver)  # o resumo não é refeito aqui
        self.assertEqual(json.loads((self.storage.gold_dir / f'{self.a}.json').read_text())['title'], 'Weekly do time')
        self.assertTrue(bronze_needs_sync(self.storage.bronze_dir / self.a, self.a, meta, workspace='personal',
                                          endpoint=self.zcfg['endpoint'], token=self.zcfg['token'],
                                          silver_text=silver, gold=GOLD))

    def test_link_marks_legacy_note_pending_but_keeps_its_id(self):
        from castanha.sync import meeting_needs_sync
        meta = self.metadata(self.a); meta['zinom'] = {'status': 'ok', 'remember_id': 'nota-1', 'note_status': 'ok'}
        write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        link_meeting_to_event(self.a, EVENT, self.storage)
        depois = self.metadata(self.a)['zinom']
        self.assertEqual(depois['status'], 'pending'); self.assertEqual(depois['remember_id'], 'nota-1')
        self.assertTrue(meeting_needs_sync(self.metadata(self.a)))

    def test_link_keeps_backslashes_in_title_literal(self):
        link_meeting_to_event(self.a, {**EVENT, 'title': 'Suporte C:\\Windows \\1'}, self.storage)
        self.assertEqual(self.metadata(self.a)['title'], 'Suporte C:\\Windows \\1')
        silver = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertIn('\n# Suporte C:\\Windows \\1\n', silver)
        self.assertIn('title: "Suporte C:\\\\Windows \\\\1"', silver)

    def test_link_revalidates_under_the_lock(self):
        from castanha import relocation
        real_lock = relocation.meeting_lock
        from contextlib import contextmanager
        @contextmanager
        def lock_then_regeneration(bronze):
            with real_lock(bronze):
                write_json(bronze / '.annotations-regeneration.json', {'status': 'pending'})  # outra sessão, antes da trava
                yield
        with patch('castanha.relocation.meeting_lock', lock_then_regeneration):
            with self.assertRaisesRegex(RelocationError, 'resumo em regeneração'):
                link_meeting_to_event(self.a, EVENT, self.storage)
        self.assertEqual(self.metadata(self.a)['title'], 'Origem')

    def test_link_puts_guests_in_the_note_body_so_zinom_sees_the_change(self):
        from castanha.bronze_ingest import synthesis_text
        meta = self.metadata(self.a)
        self.storage.save_silver(self.a, silver_frontmatter(meta) + '# Origem\n\nData: 11/09/2026\n\n## Resumo\nTexto do resumo.\n')
        antes = synthesis_text((self.storage.silver_dir / f'{self.a}.md').read_text())
        link_meeting_to_event(self.a, {**EVENT, 'title': 'Origem'}, self.storage)  # mesmo título: só convidados mudam
        silver = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertIn('# Origem\n\nConvidados (presença não confirmada): Ana, Bruno.\nLink da chamada: https://meet.google.com/abc-defg-hij\n\nData: 11/09/2026', silver)
        self.assertNotEqual(synthesis_text(silver), antes)

    def test_link_puts_call_link_in_the_note_body_and_changes_projection(self):
        from castanha.bronze_ingest import synthesis_text
        link_meeting_to_event(self.a, EVENT, self.storage)
        silver = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertIn('Convidados (presença não confirmada): Ana, Bruno.\nLink da chamada: https://meet.google.com/abc-defg-hij\n', silver)
        antes = synthesis_text(silver)
        link_meeting_to_event(self.a, {**EVENT, 'conference_url': 'https://meet.google.com/xyz-uvwx-rst'}, self.storage)
        depois = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertEqual(depois.count('Link da chamada:'), 1)
        self.assertIn('Link da chamada: https://meet.google.com/xyz-uvwx-rst', depois)
        self.assertNotEqual(synthesis_text(depois), antes)

    def test_relink_silver_never_touches_the_transcript_section(self):
        meta = {'title': 'Novo', 'recorded_at': 'x', 'calendar_event': {'attendees': [{'name': 'Ana'}], 'conference_url': 'https://meet.google.com/abc-defg-hij'}}
        corpo = ('# Velho\n\n## Resumo\nTexto.\n\n## 📝 Transcrição Bruta\n'
                 'Alguém disse: Convidados (presença não confirmada): Zé.\nLink da chamada: https://antigo.example/x\nfim\n')
        out = relink_silver(corpo, meta, silver_frontmatter(meta))
        self.assertIn('# Novo\n\nConvidados (presença não confirmada): Ana.\nLink da chamada: https://meet.google.com/abc-defg-hij\n', out)
        self.assertTrue(out.endswith('## 📝 Transcrição Bruta\nAlguém disse: Convidados (presença não confirmada): Zé.\nLink da chamada: https://antigo.example/x\nfim\n'))

    def test_relink_silver_never_touches_manual_annotations(self):
        meta = {'title': 'Novo', 'recorded_at': 'x', 'calendar_event': {'attendees': [{'name': 'Ana'}]}}
        corpo = ('# Velho\n\n## Resumo\nTexto.\n\n## Anotações manuais do usuário\n'
                 '> Convidados (presença não confirmada): Carlos. Confirmar o orçamento antes de sexta.\n')
        out = relink_silver(corpo, meta, silver_frontmatter(meta))
        self.assertIn('# Novo\n\nConvidados (presença não confirmada): Ana.\n\n## Resumo', out)
        self.assertTrue(out.endswith('## Anotações manuais do usuário\n> Convidados (presença não confirmada): Carlos. Confirmar o orçamento antes de sexta.\n'))

    def test_link_is_reapplicable_when_the_metadata_write_fails(self):
        with patch.object(self.storage, 'write_bronze_metadata', side_effect=RuntimeError('disco cheio')):
            with self.assertRaises(RuntimeError):
                link_meeting_to_event(self.a, EVENT, self.storage)
        self.assertEqual(self.metadata(self.a)['title'], 'Origem')  # metadata intacto: o vínculo não consta
        link_meeting_to_event(self.a, EVENT, self.storage)
        self.assertEqual(self.metadata(self.a)['title'], 'Weekly do time')
        silver = (self.storage.silver_dir / f'{self.a}.md').read_text()
        self.assertEqual(silver.count('Convidados (presença não confirmada): Ana, Bruno.'), 1)
        self.assertEqual(silver.count('Link da chamada:'), 1)

    def test_relink_silver_without_frontmatter_or_guest_line(self):
        meta = {'title': 'Novo', 'recorded_at': 'x', 'calendar_event': {'attendees': [{'name': 'Ana'}]}}
        out = relink_silver('# Velho\n\nTexto.\n', meta, silver_frontmatter(meta))
        self.assertTrue(out.startswith('---\ntitle: "Novo"'))
        self.assertIn('\n# Novo\n\nConvidados (presença não confirmada): Ana.\n\nTexto.\n', out)

    def test_link_refused_during_pending_cleanup_or_capture(self):
        from castanha.state import StateManager
        StateManager().write({'status': 'recording', 'capture_slug': self.a})
        with self.assertRaisesRegex(RelocationError, 'Aguarde a gravação'):
            link_meeting_to_event(self.a, EVENT, self.storage)
        StateManager().write({'status': 'idle', 'capture_slug': None})
        self.receipts(self.a)
        exclude_recording(self.a, 'capture_job1.ogg', self.storage)
        with self.assertRaisesRegex(RelocationError, 'alteração de áudios em andamento'):
            link_meeting_to_event(self.a, EVENT, self.storage)
        self.assertEqual(self.metadata(self.a)['title'], 'Origem')

    def test_event_record_rejects_garbage(self):
        for bad in (None, 'x', {}, {'uid': 'u'}, {'title': 't'}, {'uid': 'u', 'title': 't', 'start': 5}):
            with self.subTest(bad=bad), self.assertRaises(RelocationError):
                event_record(bad)
        record = event_record({'uid': 'u', 'title': ' T ', 'attendees': [{'name': 'A'}, 'lixo', {'x': 1}]})
        self.assertEqual(record['title'], 'T'); self.assertEqual(record['attendees'], [{'name': 'A'}])

    def test_library_detail_exposes_linked_event(self):
        before = MeetingLibrary(self.storage).detail(self.a)['meeting']
        self.assertFalse(before['event_linked']); self.assertEqual(before['event_attendees'], [])
        link_meeting_to_event(self.a, EVENT, self.storage)
        after = MeetingLibrary(self.storage).detail(self.a)['meeting']
        self.assertTrue(after['event_linked'])
        self.assertEqual(after['event_title'], 'Weekly do time'); self.assertEqual(after['event_uid'], EVENT['uid'])
        self.assertEqual(after['event_attendees'], ['Ana', 'Bruno'])
        self.assertEqual(after['title'], 'Weekly do time')


class TestAgendaDoDia(unittest.TestCase):
    def _fonte(self, eventos):
        from castanha import agenda
        from castanha.zinom_calendar import ZinomCalendar
        import time
        fonte = ZinomCalendar({'zinom': {'token': 't'}, 'calendar': {'zinom': {}}})
        fonte._calendars = [{'calendar_ref': 'a', 'summary': 'Bruno', 'email': 'a@x', 'primary': True}]
        fonte._calendars_at = time.time()
        fonte._call = lambda name, args: {'calendars': fonte._calendars} if name == 'list_calendars' else {'events': eventos}
        agenda._zinom = fonte
        self.addCleanup(lambda: setattr(agenda, '_zinom', None))
        return fonte

    def test_only_timed_events_that_start_on_the_day(self):
        from castanha.agenda import events_on_day, find_event
        tz = datetime.datetime.now().astimezone().tzinfo
        dia = datetime.date(2026, 9, 12)
        def ev(uid, start, end=None, all_day=False):
            if all_day:
                return {'id': uid, 'summary': uid, 'start': {'date': start}, 'end': {'date': start}}
            return {'id': uid, 'summary': uid, 'start': {'dateTime': start}, 'end': {'dateTime': end or start}}
        weekly = datetime.datetime(2026, 9, 12, 10, 30, tzinfo=tz).isoformat()
        vespera = datetime.datetime(2026, 9, 11, 23, 0, tzinfo=tz).isoformat()
        madrugada = datetime.datetime(2026, 9, 12, 1, 0, tzinfo=tz).isoformat()
        self._fonte([ev('weekly_20260912T133000Z', weekly), ev('vespera', vespera, madrugada), ev('aniversario', '2026-09-12', all_day=True)])
        resultado = events_on_day(dia)
        self.assertEqual([e.uid for e in resultado['meetings']], ['weekly_20260912T133000Z'])
        self.assertEqual(resultado['warnings'], [])
        self.assertEqual(find_event(dia, 'weekly_20260912T133000Z').title, 'weekly_20260912T133000Z')
        self.assertEqual(find_event(dia, 'weekly').uid, 'weekly_20260912T133000Z')  # pela chave da série
        self.assertIsNone(find_event(dia, 'outra'))

    def test_day_bounds_follow_the_local_rules_of_that_date_not_today(self):
        import os, time
        from castanha.agenda import events_on_day
        tz_antes = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'; time.tzset()
        try:
            def ev(uid, start):
                return {'id': uid, 'summary': uid, 'start': {'dateTime': start}, 'end': {'dateTime': start}}
            # Janeiro em Nova York é -05:00; o offset "de agora" (setembro, -04:00) deslocaria o dia.
            self._fonte([ev('vespera', '2026-01-11T23:30:00-05:00'), ev('do_dia', '2026-01-12T23:30:00-05:00')])
            resultado = events_on_day(datetime.date(2026, 1, 12))
            self.assertEqual([e.uid for e in resultado['meetings']], ['do_dia'])
        finally:
            if tz_antes is None: os.environ.pop('TZ', None)
            else: os.environ['TZ'] = tz_antes
            time.tzset()

    def test_day_query_includes_configured_ical_feeds(self):
        from castanha import agenda
        from castanha.agenda import events_on_day
        agenda._zinom = None; self.addCleanup(lambda: setattr(agenda, '_zinom', None))
        tz = datetime.datetime.now().astimezone()
        offset = tz.strftime('%z')
        ics = tempfile.NamedTemporaryFile('w', suffix='.ics', delete=False); self.addCleanup(lambda: os.unlink(ics.name))
        ics.write('BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:ical-1\nSUMMARY:Reunião do feed\n'
                  'DTSTART;TZID=UTC:' + datetime.datetime(2026, 9, 12, 14, 0, tzinfo=tz.tzinfo).astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '\n'
                  'DTEND;TZID=UTC:' + datetime.datetime(2026, 9, 12, 15, 0, tzinfo=tz.tzinfo).astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '\n'
                  'END:VEVENT\nBEGIN:VEVENT\nUID:ical-dia\nSUMMARY:Feriado\nDTSTART;VALUE=DATE:20260912\nEND:VEVENT\nEND:VCALENDAR\n'); ics.close()
        cfg = {'calendar': {'feeds': [{'url': ics.name}], 'zinom': {'enabled': False}}, 'zinom': {'token': ''}}
        resultado = events_on_day(datetime.date(2026, 9, 12), cfg)
        self.assertEqual([e.uid for e in resultado['meetings']], ['ical-1'])
        self.assertEqual(resultado['warnings'], [])

    def test_day_query_keeps_json_clean_and_survives_zinom_failure(self):
        import contextlib, io
        from castanha import agenda
        from castanha.agenda import events_on_day
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte([])
        fonte._calendars_at = 0.0  # a lista de agendas expirou e vai falhar
        def _call(name, args): raise ZinomError('<urlopen error [Errno -3] Temporary failure in name resolution>')
        fonte._call = _call
        cfg = {'calendar': {'feeds': [{'name': 'Feed quebrado', 'url': 'https://127.0.0.1:9/nada.ics'}], 'zinom': {}}, 'zinom': {'token': 't'}}
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            resultado = events_on_day(datetime.date(2026, 9, 12), cfg)
        self.assertEqual(stdout.getvalue(), '')  # nada em stdout: o JSON do CLI fica íntegro
        self.assertEqual(resultado['meetings'], [])
        self.assertEqual(resultado['warnings'], ['Zinom: sem conexão com o Zinom', 'Feed iCal Feed quebrado: indisponível'])

    def test_failed_calendar_becomes_warning_not_silence(self):
        from castanha.agenda import events_on_day
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte([])
        fonte._calendars.append({'calendar_ref': 'b', 'summary': 'Eventos Nora', 'email': 'a@x', 'accessRole': 'owner'})
        def _call(name, args):
            if name == 'list_calendars': return {'calendars': fonte._calendars}
            if args['calendar_ref'] == 'b': raise ZinomError('<urlopen error timed out>')
            return {'events': []}
        fonte._call = _call
        resultado = events_on_day(datetime.date(2026, 9, 12))
        self.assertEqual(resultado['meetings'], [])
        self.assertEqual(resultado['warnings'], ['Eventos Nora: sem conexão com o Zinom'])


class TestRelocationCli(RelocationFixture):
    BOOTSTRAP = '''
import json, runpy, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[1]))
from castanha.calendar import Attendee, MeetingEvent
import datetime
ev = MeetingEvent(uid="weekly_20260912T133000Z", title="Weekly do time",
                  start=datetime.datetime.fromisoformat("2026-09-12T10:30:00-03:00"), end=datetime.datetime.fromisoformat("2026-09-12T11:30:00-03:00"),
                  attendees=[Attendee(name="Ana", email="ana@example.com", response="accepted", organizer=True)],
                  conference_url="https://meet.google.com/abc-defg-hij", account="bruno@example.com", source="zinom")
calls = []
def fake_day(dia, config=None):
    calls.append(str(dia)); return {"meetings": [ev], "warnings": []}
sys.argv = sys.argv[1:]
with patch("castanha.agenda.events_on_day", side_effect=fake_day):
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    finally:
        Path(sys.argv[0]).parent.parent.joinpath("tests", ".unused").exists()
        sys.stderr.write("DAYS=" + json.dumps(calls) + "\\n")
'''

    def cli(self, *args, agenda=False):
        if agenda:
            cmd = [sys.executable, '-c', self.BOOTSTRAP, CLI, *args]
        else:
            cmd = [sys.executable, CLI, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_move_cli_uses_revision_and_reports_destination(self):
        stale = self.cli('recordings', 'move', '--json', '--expected-revision', '5', '--to=' + self.b, '--', self.a, 'capture_job1.ogg')
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(json.loads(stale.stdout)['status'], 'conflict')
        self.assertEqual(len(self.metadata(self.b)['recordings']), 1)
        result = self.cli('recordings', 'move', '--json', '--expected-revision', '0', '--to=' + self.b, '--', self.a, 'capture_job1.ogg')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 'ok'); self.assertEqual(payload['destination']['slug'], self.b)
        self.assertEqual(len(self.metadata(self.b)['recordings']), 2)
        # A origem está sendo refeita: uma segunda movimentação espera o daemon concluir.
        again = self.cli('recordings', 'move', '--json', '--to=' + self.b, '--', self.a, 'capture_job2.ogg')
        self.assertEqual(again.returncode, 1)
        self.assertIn('em andamento', json.loads(again.stdout)['message'])
        self.assertTrue((self.storage.bronze_dir / self.a / 'capture_job2.ogg').exists())

    def test_move_cli_to_new_meeting_with_title(self):
        result = self.cli('recordings', 'move', '--json', '--new-title=Conversa avulsa', '--', self.a, 'capture_job1.ogg')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['destination']['created']); self.assertIn('conversa-avulsa', payload['destination']['slug'])

    def test_link_event_cli_uses_local_day_for_timezone_aware_recorded_at(self):
        meta = self.metadata(self.a); meta['recorded_at'] = '2026-09-12T01:00:00+00:00'
        write_json(self.storage.bronze_dir / self.a / 'metadata.json', meta)
        esperado = datetime.datetime.fromisoformat('2026-09-12T01:00:00+00:00').astimezone().date().isoformat()
        result = self.cli('link-event', '--json', '--', self.a, 'weekly_20260912T133000Z', agenda=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'DAYS=["{esperado}"]', result.stderr)

    def test_link_event_cli_resolves_the_day_of_the_recording(self):
        result = self.cli('link-event', '--json', '--', self.a, 'weekly_20260912T133000Z', agenda=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 'ok'); self.assertEqual(payload['title'], 'Weekly do time')
        self.assertIn('DAYS=["2026-09-11"]', result.stderr)  # dia da gravação, sem --date
        self.assertEqual(self.metadata(self.a)['title'], 'Weekly do time')
        missing = self.cli('link-event', '--json', '--date', '2026-09-12', '--', self.a, 'nao-existe', agenda=True)
        self.assertEqual(missing.returncode, 1)
        self.assertIn('não encontrado', json.loads(missing.stdout)['message'])

    def test_agenda_dia_cli_lists_the_day_as_json(self):
        result = self.cli('agenda', '--json', '--dia', '2026-09-12', agenda=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 'ok'); self.assertEqual(payload['date'], '2026-09-12')
        self.assertEqual([m['title'] for m in payload['meetings']], ['Weekly do time'])
        self.assertEqual(payload['meetings'][0]['attendees'][0]['name'], 'Ana')
        bad = self.cli('agenda', '--json', '--dia', '12/09/2026', agenda=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)['status'], 'error')


if __name__ == '__main__':
    unittest.main()
