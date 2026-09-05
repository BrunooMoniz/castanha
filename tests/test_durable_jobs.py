"""Retomada real em disco e transporte remoto isolado, sem conta ou áudio pessoal."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from castanha.audio import ChannelLevels
from castanha.engine import CastanhaEngine
from castanha.storage import MeetingStorage
from castanha.sync import sync_meeting, sync_pending
from castanha.transcription import TranscriptionPending, TranscriptionResult, VpsSshTranscriber, get_transcriber
from castanha.zinom_adapter import ZinomAdapter, ZinomError


class ProcessDeath(BaseException):
    pass


class TestDurableJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / 'config/castanha'
        config.mkdir(parents=True)
        base = self.root / 'meetings'
        (config / 'config.json').write_text(json.dumps({
            'storage': {'base_dir': str(base), 'bronze_dir': str(base / 'bronze'),
                        'silver_dir': str(base / 'silver'), 'gold_dir': str(base / 'gold')},
            'llm': {'api_key': ''}, 'zinom': {'enabled': False, 'token': ''},
            'transcription': {'groq_api_key': '', 'vps_ssh_host': ''},
        }))
        for patcher in (
            patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                     'XDG_STATE_HOME': str(self.root / 'state')}),
            patch('castanha.engine.notify'),
            patch('castanha.engine.measure_channel_levels', return_value=[ChannelLevels(0, 'mic', -30, -10, False)]),
            patch('castanha.engine.probe_duration_seconds', return_value=30),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = CastanhaEngine()
        source = self.root / 'volatile.ogg'
        source.write_bytes(b'original unique recording')
        self.engine.state_mgr.write({'status': 'recording', 'pid': None, 'audio_path': str(source),
                                     'current_meeting': {'title': 'Fixture'}, 'mode': 'mic-only'})
        self.source = source

    def transcription(self, text='Decisão preservada', provider='groq'):
        return TranscriptionResult(text, [], provider, {})

    def crash(self):
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.side_effect = ProcessDeath
            with self.assertRaises(ProcessDeath):
                self.engine.stop_recording()
        return self.engine.state_mgr.read()['capture_slug']

    def test_resume_after_process_death_without_original_or_duplicate_recording(self):
        slug = self.crash()
        self.source.unlink()
        # Nova instância, como após reiniciar a máquina; não depende do /tmp.
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            sync_meeting(slug, self.engine.storage)
            sync_meeting(slug, self.engine.storage)
            self.assertEqual(provider.return_value.transcribe.call_count, 1)
        bronze = self.engine.storage.bronze_dir / slug
        self.assertEqual((bronze / 'transcript_raw.txt').read_text(), 'Decisão preservada')
        self.assertEqual(len(self.engine.storage._read_bronze_metadata(slug)['recordings']), 1)
        self.assertEqual(len(list(bronze.glob('*.ogg'))), 1)

    def test_segment_provenance_survives_checkpoint_and_summary_retry(self):
        from castanha.transcription import Utterance
        result = TranscriptionResult(
            '[0.20s] Microfone local: decisão',
            [Utterance('Microfone local', 'decisão', 0.2, 1.3)],
            'fixture', {'channel_provenance': True})
        with patch('castanha.engine.get_transcriber') as provider, \
             patch.object(self.engine.summarizer, 'generate_silver', side_effect=ProcessDeath):
            provider.return_value.transcribe.return_value = result
            with self.assertRaises(ProcessDeath):
                self.engine.stop_recording()
        slug = self.engine.state_mgr.read()['capture_slug']
        bronze = self.engine.storage.bronze_dir / slug
        before = (bronze / 'transcript_segments.json').read_bytes()
        with patch('castanha.engine.get_transcriber') as provider:
            sync_meeting(slug, self.engine.storage)
            provider.assert_not_called()
        self.assertEqual(before, (bronze / 'transcript_segments.json').read_bytes())
        recording = json.loads(before)['recordings'][0]
        self.assertTrue(recording['channel_provenance'])
        self.assertEqual(recording['time_reference'], 'recording_start')
        self.assertEqual(recording['utterances'][0]['speaker'], 'Microfone local')
        self.assertEqual(recording['utterances'][0]['start'], 0.2)
        self.assertTrue(recording['source_sha256'])

    def test_channel_origin_and_hashes_reach_the_bronze_envelope(self):
        from castanha.channel_transcription import ChannelUtterance
        falas = [ChannelUtterance('Microfone local', 'decisão local', 0.2, 1.3,
                                  0, 'microfone_local', 'sha-origem', 'sha-canal-0'),
                 ChannelUtterance('Áudio do sistema', 'resposta remota', 0.4, 1.9,
                                  1, 'audio_sistema', 'sha-origem', 'sha-canal-1')]
        canais = [{'channel': 0, 'origin': 'microfone_local', 'label': 'Microfone local',
                   'provider': 'fixture', 'silent': False, 'channel_sha256': 'sha-canal-0',
                   'utterance_count': 1},
                  {'channel': 1, 'origin': 'audio_sistema', 'label': 'Áudio do sistema',
                   'provider': 'fixture', 'silent': False, 'channel_sha256': 'sha-canal-1',
                   'utterance_count': 1}]
        result = TranscriptionResult(
            '[0.20s] Microfone local: decisão local\n[0.40s] Áudio do sistema: resposta remota',
            falas, 'fixture', {'channel_provenance': True, 'identity_inferred': False,
                               'channels': canais, 'utterance_count': 2})
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = result
            slug = self.engine.stop_recording()['result']['slug']
        bronze = self.engine.storage.bronze_dir / slug
        gravacao = json.loads((bronze / 'transcript_segments.json').read_text())['recordings'][0]
        metadata = self.engine.storage._read_bronze_metadata(slug)
        self.assertTrue(gravacao['channel_provenance'])
        self.assertEqual(gravacao['utterance_count'], len(gravacao['utterances']))
        self.assertEqual([u['origin'] for u in gravacao['utterances']],
                         ['microfone_local', 'audio_sistema'])
        self.assertEqual([u['channel'] for u in gravacao['utterances']], [0, 1])
        self.assertEqual([u['start'] for u in gravacao['utterances']], [0.2, 0.4])
        self.assertEqual({u['channel_sha256'] for u in gravacao['utterances']},
                         {'sha-canal-0', 'sha-canal-1'})
        self.assertEqual([c['origin'] for c in gravacao['channels']],
                         ['microfone_local', 'audio_sistema'])
        # O hash da gravação vem do job em disco, não do que o provedor afirmou.
        self.assertEqual(gravacao['source_sha256'], metadata['recordings'][0]['sha256'])
        self.assertEqual([o['origin'] for o in metadata['recordings'][0]['origins']],
                         ['microfone_local', 'audio_sistema'])
        # Nenhuma fala recebe nome: só as duas origens técnicas aparecem.
        self.assertEqual({u['speaker'] for u in gravacao['utterances']},
                         {'Microfone local', 'Áudio do sistema'})

    def gravacao_estereo(self):
        """Estéreo real: o módulo por canal roda FFmpeg de verdade sobre ele."""
        caminho = self.root / 'estereo.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'aevalsrc=0.2*sin(2*PI*440*t)|0.3*sin(2*PI*880*t):d=1:s=16000',
                        str(caminho)], check=True, capture_output=True)
        return caminho

    def engine_por_canal(self):
        """Liga a flag no arquivo de config: é assim que o daemon a enxerga."""
        arquivo = self.root / 'config/castanha/config.json'
        config = json.loads(arquivo.read_text())
        config['transcription']['por_canal'] = True
        arquivo.write_text(json.dumps(config))
        engine = CastanhaEngine()
        self.assertTrue(engine._por_canal())
        engine.state_mgr.write({'status': 'recording', 'pid': None,
                                'audio_path': str(self.gravacao_estereo()),
                                'current_meeting': {'title': 'Fixture'}, 'mode': 'dual'})
        return engine

    def test_channel_flag_is_off_by_default_and_sends_the_whole_recording(self):
        recebidos = []
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.side_effect = (
                lambda path, mode='dual': recebidos.append(Path(path))
                or self.transcription())
            self.engine.stop_recording()
        self.assertFalse(self.engine._por_canal())
        self.assertEqual([p.suffix for p in recebidos], ['.ogg'])
        self.assertFalse(any(p.name.startswith('channel-') for p in recebidos))

    def test_channel_flag_sends_each_origin_apart_and_resumes_only_the_pending_one(self):
        from castanha.transcription import Utterance
        engine = self.engine_por_canal()
        chamadas, duracoes, falhar = [], [], {1}

        class Provedor:
            def transcribe(_self, path, mode='dual'):
                canal = int(Path(path).stem[-1])
                chamadas.append((canal, Path(path).suffix))
                if canal in falhar:
                    raise RuntimeError('provedor caiu no canal remoto')
                texto = f'fala do canal {canal}'
                return TranscriptionResult(
                    texto, [Utterance('Falante', texto, 0.1 * canal, 1.0)], 'fixture', {})

        def escolher(estimated_duration_sec=60):
            duracoes.append(estimated_duration_sec)
            return Provedor()

        with patch('castanha.engine.get_transcriber', side_effect=escolher):
            resultado = engine.stop_recording()
        # O canal 1 caiu: a reunião fica pendente, com o canal 0 já pago no checkpoint.
        self.assertEqual(resultado['status'], 'partial')
        self.assertEqual(chamadas, [(0, '.flac'), (1, '.flac')])
        slug = resultado['result']['slug']
        bronze = engine.storage.bronze_dir / slug
        checkpoints = sorted(p.name for p in (bronze / '.channels').rglob('*.json'))
        self.assertEqual(checkpoints, ['channel-0.json'])

        falhar.clear()
        with patch('castanha.engine.get_transcriber', side_effect=escolher):
            sync_meeting(slug, engine.storage)
        # Só o canal que faltava é enviado de novo; o canal 0 não é recobrado.
        self.assertEqual(chamadas, [(0, '.flac'), (1, '.flac'), (1, '.flac')])
        # Orçamento por canal: a duração consultada é a do canal, não da reunião.
        for medida in duracoes:
            self.assertAlmostEqual(medida, 1.0, delta=0.2)
        gravacao = json.loads(
            (bronze / 'transcript_segments.json').read_text())['recordings'][0]
        self.assertTrue(gravacao['channel_provenance'])
        self.assertEqual([u['origin'] for u in gravacao['utterances']],
                         ['microfone_local', 'audio_sistema'])
        self.assertEqual([u['speaker'] for u in gravacao['utterances']],
                         ['Microfone local', 'Áudio do sistema'])
        self.assertEqual(gravacao['utterance_count'], 2)
        self.assertEqual([c['origin'] for c in gravacao['channels']],
                         ['microfone_local', 'audio_sistema'])
        texto = (bronze / 'transcript_raw.txt').read_text()
        self.assertIn('Microfone local: fala do canal 0', texto)
        self.assertIn('Áudio do sistema: fala do canal 1', texto)

    def test_channel_path_keeps_the_audio_pending_when_only_the_vps_answers(self):
        engine = self.engine_por_canal()
        with patch('castanha.engine.get_transcriber',
                   side_effect=lambda estimated_duration_sec=60: VpsSshTranscriber('host-de-teste')):
            resultado = engine.stop_recording()
        self.assertEqual(resultado['status'], 'partial')
        slug = resultado['result']['slug']
        bronze = engine.storage.bronze_dir / slug
        # Nada de mandar FLAC com nome de .ogg para o job remoto: fica pendente.
        self.assertFalse(list((bronze / '.channels').rglob('*.json')))
        self.assertIn('VPS', resultado['result']['transcription_error'] or '')

    def test_all_summary_stages_receive_channel_identity_guardrail(self):
        from castanha import summarizer
        for name in ('PARTIAL', 'COMBINE', 'SILVER', 'GOLD'):
            prompt = getattr(summarizer, name + '_SYSTEM_PROMPT')
            self.assertIn(summarizer.CHANNEL_GROUNDING, prompt)

    def test_stop_resume_uses_bronze_if_temporary_audio_disappears(self):
        slug = self.crash()
        self.source.unlink()
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            result = self.engine.stop_recording()
        self.assertEqual(result['result']['slug'], slug)
        self.assertEqual(len(self.engine.storage.list_meeting_recordings(slug)), 1)
        self.assertEqual(self.engine.state_mgr.read()['status'], 'idle')

    def test_retry_button_resumes_new_jobs_and_unblocks_recording(self):
        slug = self.crash()
        self.source.unlink()
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            self.engine.reprocess_meeting(slug)
            self.engine.reprocess_meeting(slug)
            self.assertEqual(provider.return_value.transcribe.call_count, 1)
        self.assertEqual(self.engine.state_mgr.read()['status'], 'idle')
        self.assertEqual((self.engine.storage.bronze_dir / slug / 'transcript_raw.txt').read_text(),
                         'Decisão preservada')

    def test_resume_after_transcription_checkpoint_does_not_transcribe_twice(self):
        with patch('castanha.engine.get_transcriber') as provider, \
             patch.object(self.engine.summarizer, 'generate_silver', side_effect=ProcessDeath):
            provider.return_value.transcribe.return_value = self.transcription()
            with self.assertRaises(ProcessDeath):
                self.engine.stop_recording()
        slug = self.engine.state_mgr.read()['capture_slug']
        with patch('castanha.engine.get_transcriber') as provider:
            sync_pending(storage=self.engine.storage)
            provider.assert_not_called()
        self.assertEqual((self.engine.storage.bronze_dir / slug / 'transcript_raw.txt').read_text(), 'Decisão preservada')

    def test_orphan_job_recovers_if_process_dies_before_metadata_publish(self):
        slug = self.crash()
        (self.engine.storage.bronze_dir / slug / 'metadata.json').unlink()
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            self.assertEqual(len(sync_pending(storage=self.engine.storage)), 1)
        self.assertEqual(self.engine.storage._read_bronze_metadata(slug)['title'], 'Fixture')

    def test_remote_pending_returns_promptly_and_is_retried(self):
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.side_effect = TranscriptionPending('Aguardando VPS')
            first = self.engine.stop_recording()
        self.assertEqual(first['status'], 'partial')
        slug = first['result']['slug']
        self.assertEqual(self.engine.storage._read_bronze_metadata(slug)['processing_status'], 'pending')
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            sync_pending(storage=self.engine.storage)
        self.assertEqual(self.engine.storage._read_bronze_metadata(slug)['processing_status'], 'complete')

    def test_mock_append_is_excluded_while_preserving_real_recording(self):
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            slug = self.engine.stop_recording()['result']['slug']
        self.engine.state_mgr.write({'status': 'recording', 'audio_path': str(self.source),
                                     'target_meeting_slug': slug, 'current_meeting': {'title': 'Fixture'}})
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription('Simulação', 'mock')
            self.engine.stop_recording()
        metadata = self.engine.storage._read_bronze_metadata(slug)
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token = True, 'fixture-token'
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            result = adapter.ingest_meeting(metadata, 'nota', {'facts': []})
        transcript = (self.engine.storage.bronze_dir / slug / 'transcript_raw.txt').read_text()
        self.assertNotIn('Simulação', transcript)
        self.assertIn('Decisão preservada', transcript)
        self.assertEqual([r['transcription_provider'] for r in metadata['recordings']], ['groq', 'mock'])
        self.assertEqual(metadata['transcription_provider'], 'groq')
        self.assertEqual(len(metadata['memory_recording_ids']), 1)

    def test_silent_append_does_not_erase_earlier_valid_audio(self):
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            slug = self.engine.stop_recording()['result']['slug']
        self.engine.state_mgr.write({'status': 'recording', 'audio_path': str(self.source),
                                     'target_meeting_slug': slug, 'current_meeting': {'title': 'Fixture'}})
        with patch('castanha.engine.measure_channel_levels', return_value=[ChannelLevels(0, 'mic', -99, -99, True)]), \
             patch('castanha.engine.get_transcriber') as provider:
            self.engine.stop_recording()
            provider.assert_not_called()
        metadata = self.engine.storage._read_bronze_metadata(slug)
        self.assertEqual(metadata['audio_status'], 'ok')
        self.assertEqual(metadata['transcription_provider'], 'groq')
        self.assertEqual([r['audio_status'] for r in metadata['recordings']], ['ok', 'sem_audio'])
        self.assertEqual((self.engine.storage.bronze_dir / slug / 'transcript_raw.txt').read_text(), 'Decisão preservada')

    def test_real_append_after_mock_keeps_mock_out_of_real_transcript(self):
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription('SIMULACAO NAO E FATO', 'mock')
            slug = self.engine.stop_recording()['result']['slug']
        self.engine.state_mgr.write({'status': 'recording', 'audio_path': str(self.source),
                                     'target_meeting_slug': slug, 'current_meeting': {'title': 'Fixture'}})
        with patch('castanha.engine.get_transcriber') as provider:
            provider.return_value.transcribe.return_value = self.transcription()
            self.engine.stop_recording()
        metadata = self.engine.storage._read_bronze_metadata(slug)
        self.assertEqual([r['transcription_provider'] for r in metadata['recordings']], ['mock', 'groq'])
        self.assertEqual(metadata['transcription_provider'], 'groq')
        self.assertEqual(len(metadata['memory_recording_ids']), 1)
        bronze = self.engine.storage.bronze_dir / slug
        self.assertEqual((bronze / 'transcript_raw.txt').read_text(), 'Decisão preservada')
        self.assertTrue(any('SIMULACAO NAO E FATO' in p.read_text() for p in (bronze / '.jobs').glob('*.json')))
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token = True, 'fixture-token'
        silver = (self.engine.storage.silver_dir / f'{slug}.md').read_text()
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            client.return_value.call_tool.return_value = {'content': [{'type': 'text', 'text': '{"source_id":"fixture"}'}]}
            result = adapter.ingest_meeting(metadata, silver, {'facts': []})
            self.assertNotIn('SIMULACAO NAO E FATO', str(client.return_value.call_tool.call_args_list))
        self.assertEqual(result['status'], 'ok')

    def test_pending_facts_keep_original_hash_and_note_receipt_across_sync(self):
        self.engine.zinom.enabled, self.engine.zinom.token = True, 'fixture-token'
        fact = {'subject': 'Projeto', 'predicate': 'usa', 'object': 'Python'}
        with patch('castanha.engine.get_transcriber') as provider, \
             patch('castanha.summarizer.MeetingSummarizer.generate_gold', return_value={'facts': [fact]}), \
             patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            provider.return_value.transcribe.return_value = self.transcription()
            client.return_value.call_tool.return_value = {'content': [{'type': 'text', 'text': '{"source_id":"conversation:fixture"}'}]}
            result = self.engine.stop_recording()
        self.assertEqual(result['status'], 'partial')
        slug = result['result']['slug']
        meta = self.engine.storage._read_bronze_metadata(slug)
        self.assertEqual(meta['zinom']['facts_pending'], [fact])
        self.assertEqual(meta['zinom']['source']['slug'], slug)
        self.assertEqual(meta['zinom']['source']['remember_id'], 'conversation:fixture')
        self.assertEqual(meta['zinom']['source']['recordings'][0]['sha256'], meta['recordings'][0]['sha256'])
        self.assertEqual(len(meta['recordings'][0]['sha256']), 64)
        self.assertEqual(meta['zinom']['facts_status'], 'pending_lineage')
        self.assertEqual(meta['zinom']['status'], 'pending')
        calls = client.return_value.call_tool.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['remember'])
        with patch('castanha.sync.ZinomAdapter', return_value=self.engine.zinom), \
             patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            client.return_value.call_tool.return_value = {'content': [{'type': 'text', 'text': '{"source_id":"conversation:fixture"}'}]}
            sync_meeting(slug, self.engine.storage)
            self.assertEqual([call.args[0] for call in client.return_value.call_tool.call_args_list], ['brain_update'])
        sync_meeting(slug, self.engine.storage)  # Credencial desabilitada: preserva recibo e fatos.
        meta = self.engine.storage._read_bronze_metadata(slug)
        self.assertEqual(meta['zinom']['facts_pending'], [fact])
        self.assertEqual(meta['zinom']['source']['remember_id'], 'conversation:fixture')
        self.assertEqual(meta['zinom']['facts_status'], 'pending_lineage')

    def test_backlog_over_twenty_drains_including_old_items(self):
        storage = self.engine.storage
        for i in range(45):
            bronze = storage.bronze_dir / f'meeting-{i:03}'
            bronze.mkdir()
            (bronze / 'metadata.json').write_text(json.dumps({
                'audio_status': 'ok', 'transcription_provider': 'groq',
                'zinom': {'status': 'ok' if i < 20 else 'pending'},
            }))
            (storage.silver_dir / f'{bronze.name}.md').write_text('Nota')
        with patch('castanha.sync.ZinomAdapter') as adapter:
            adapter.return_value.ingest_meeting.return_value = {'status': 'ok', 'remember': {'id': 'fixture-id'}}
            self.assertEqual(len(sync_pending(limit=20, storage=storage)), 20)
            self.assertEqual(len(sync_pending(limit=20, storage=storage)), 5)
            self.assertEqual(sync_pending(storage=storage), [])


class TestRemoteJob(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'audio.ogg'
        self.audio.write_bytes(b'original')

    def test_ssh_timeout_is_bounded_and_pending(self):
        with patch('castanha.transcription.subprocess.run', side_effect=subprocess.TimeoutExpired('ssh', 15)) as run:
            with self.assertRaises(TranscriptionPending):
                VpsSshTranscriber('fixture').transcribe(self.audio)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 30)
        self.assertIn('BatchMode=yes', run.call_args.args[0])

    def test_scp_timeout_is_bounded_and_pending(self):
        with patch('castanha.transcription.subprocess.run', side_effect=[
            subprocess.CompletedProcess([], 0, 'UPLOAD', ''), subprocess.TimeoutExpired('scp', 30),
        ]) as run:
            with self.assertRaises(TranscriptionPending):
                VpsSshTranscriber('fixture').transcribe(self.audio)
        self.assertEqual(run.call_args.args[0][0], 'scp')
        self.assertLessEqual(run.call_args.kwargs['timeout'], 30)
        self.assertIn('BatchMode=yes', run.call_args.args[0])

    def test_real_detached_job_survives_retry_without_second_transcription(self):
        # Executa os comandos remotos de verdade num diretório temporário local.
        script = self.root / 'transcribe.sh'
        script.write_text('#!/bin/sh\necho started >> count\nsleep 0.3\necho \'{"text":"Conteúdo completo","segments":[]}\'\n')
        script.chmod(0o700)
        real_run = subprocess.run
        commands = []

        def transport(args, **kwargs):
            commands.append((args, kwargs))
            if args[0] == 'scp':
                target = self.root / args[-1].split(':', 1)[1]
                shutil.copyfile(args[-2], target)
                return subprocess.CompletedProcess(args, 0, '', '')
            command = args[-1].replace('/root/castanha-transcribe.py', str(script))
            return real_run(['sh', '-c', command], cwd=self.root, **kwargs)

        with patch('castanha.transcription.subprocess.run', side_effect=transport):
            for _ in range(2):
                with self.assertRaises(TranscriptionPending):
                    VpsSshTranscriber('fixture').transcribe(self.audio)
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = VpsSshTranscriber('fixture').transcribe(self.audio)
                    break
                except TranscriptionPending:
                    if time.monotonic() > deadline:
                        self.fail('Job local não terminou')
                    time.sleep(0.1)
        self.assertEqual(result.text, 'Conteúdo completo')
        self.assertEqual((self.root / 'count').read_text().splitlines(), ['started'])
        self.assertEqual(sum(args[0] == 'scp' for args, _ in commands), 1)
        self.assertTrue(all(0 < kwargs['timeout'] <= 30 for _, kwargs in commands))

    def test_missing_configuration_never_selects_mock(self):
        with patch('castanha.transcription.load_config', return_value={'transcription': {'vps_ssh_host': ''}}), \
             patch.dict('os.environ', {'GROQ_API_KEY': ''}):
            with self.assertRaises(TranscriptionPending):
                get_transcriber()


class TestMemoryCheckpoints(unittest.TestCase):
    def test_tombstone_stops_facts_as_well_as_replacement_note(self):
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token = True, 'fixture'
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            client.return_value.call_tool.side_effect = ZinomError(
                'Source explicitly deleted', code='source_tombstoned', tool='brain_update')
            result = adapter.ingest_meeting({}, 'nota', {'facts': [
                {'subject': 'Projeto', 'predicate': 'usa', 'object': 'Python'},
            ]}, previous_remember_id='conversation:deleted')
        self.assertEqual(result['status'], 'tombstoned')
        self.assertEqual(client.return_value.call_tool.call_count, 1)

    def test_receipt_checkpoint_keeps_pending_facts_without_publishing_them(self):
        adapter = ZinomAdapter()
        adapter.enabled, adapter.token = True, 'fixture'
        receipts = []
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            client.return_value.call_tool.side_effect = [
                {'content': [{'type': 'text', 'text': '{"source_id":"conversation:original"}'}]}, ProcessDeath,
            ]
            result = adapter.ingest_meeting({}, 'nota', {'facts': [
                {'subject': 'Projeto', 'predicate': 'usa', 'object': 'Python'},
            ]}, on_remember=receipts.append)
            self.assertEqual(client.return_value.call_tool.call_count, 1)
            self.assertEqual(result['facts_status'], 'pending_lineage')
            self.assertEqual(result['status'], 'pending')
            self.assertEqual(result['facts_ingested'], 0)
        self.assertEqual(receipts[0]['remember']['id'], 'conversation:original')
        self.assertEqual(receipts[0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
