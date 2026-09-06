"""Engine + Bronze + validador reais; áudio sintético e VPS em subprocesso local."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from castanha.engine import CastanhaEngine
from castanha.transcription import MockTranscriber, TranscriptionResult, Utterance
from tests.test_vps_channel_transport import LocalVps, TEST_CONTRACT


class VpsChannelCallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cfg = self.root / 'config/castanha'
        cfg.mkdir(parents=True)
        self.config = {
            'storage': {'base_dir': str(self.root / 'meetings'),
                        **{level + '_dir': str(self.root / 'meetings' / level)
                           for level in ('bronze', 'silver', 'gold')}},
            'transcription': {'por_canal': True, 'provider': 'vps_ssh',
                              'groq_api_key': 'fixture-key', 'vps_ssh_host': 'fixture',
                              **{('vps_worker_contract' if k == 'contract' else 'vps_' + k): v
                                 for k, v in TEST_CONTRACT.items()}},
            'llm': {'api_key': ''}, 'zinom': {'enabled': False, 'token': ''}}
        (cfg / 'config.json').write_text(json.dumps(self.config))
        self.remote = LocalVps(self.root / 'remote')
        for patcher in (
            patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                    'XDG_STATE_HOME': str(self.root / 'state'),
                                    'CASTANHA_MOCK_TRANSCRIBER': '', 'GROQ_API_KEY': ''}),
            patch('castanha.engine.notify'),
            patch('castanha.budget.BudgetManager.can_use_groq', return_value=False),
            patch('castanha.transcription.subprocess.run', side_effect=self.remote),
            patch('castanha.transcription.MockTranscriber.transcribe', side_effect=AssertionError('mock proibido')),
            patch('urllib.request.urlopen', side_effect=AssertionError('rede proibida')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = CastanhaEngine()
        for name, patcher in (
            ('silver', patch.object(self.engine.summarizer, 'generate_silver', return_value='nota sintética')),
            ('gold', patch.object(self.engine.summarizer, 'generate_gold', return_value={'facts': []})),
            ('ingest', patch.object(self.engine.zinom, 'ingest_meeting', return_value={'status': 'disabled'})),
        ):
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.source = self.audio('0.2*sin(2*PI*440*t)|0.3*sin(2*PI*880*t)')
        self.original = self.source.read_bytes()
        self.record()

    def audio(self, expression):
        path = self.root / 'original.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        f'aevalsrc={expression}:d=1:s=48000', str(path)], check=True, capture_output=True)
        return path

    def record(self, mode='dual'):
        self.engine.state_mgr.write({'status': 'recording', 'pid': None, 'audio_path': str(self.source),
                                     'current_meeting': {'title': 'Fixture'}, 'mode': mode})

    def stop_pending(self):
        result = self.engine.stop_recording()
        self.slug = result['result']['slug']
        self.bronze = self.engine.storage.bronze_dir / self.slug
        self.assert_pending(result)
        return result

    def assert_pending(self, result):
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.job()['stage'], 'pending')
        self.silver.assert_not_called()
        self.gold.assert_not_called()
        self.ingest.assert_not_called()
        self.assertEqual(Path(self.job()['audio_path']).read_bytes(), self.original)
        self.assertEqual((self.bronze / 'transcript_raw.txt').read_text(), '')

    def job(self):
        return json.loads(next((self.bronze / '.jobs').glob('*.json')).read_text())

    def resume(self):
        # Reabre engine como sync/retry em processo seguinte, usando o mesmo disco.
        engine = CastanhaEngine()
        with patch.object(engine.summarizer, 'generate_silver', self.silver), \
             patch.object(engine.summarizer, 'generate_gold', self.gold), \
             patch.object(engine.zinom, 'ingest_meeting', self.ingest):
            return engine.reprocess_meeting(self.slug)

    def test_pending_lost_reply_checkpoint_retry_and_real_validator(self):
        self.remote.lose_reply = True
        self.stop_pending()
        self.assertEqual(list((self.bronze / '.channels').rglob('*.json')), [])
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 2,
                         'os dois canais devem subir antes de devolver pending ao computador')
        self.assertEqual(len(list(self.remote.root.rglob('request.json'))), 2)
        self.remote.completed(2)
        before = len(self.remote.calls)
        result = self.resume()
        self.assertEqual(self.job()['stage'], 'done')
        checkpoints = sorted((self.bronze / '.channels').rglob('*.json'))
        self.assertEqual([p.name for p in checkpoints], ['channel-0.json', 'channel-1.json'])
        self.assertFalse(any(args[0] == 'scp' or 'nohup' in args[-1]
                             for args, _ in self.remote.calls[before:]))
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 2)
        self.assertEqual((self.remote.root / 'count').read_text().splitlines(), ['started', 'started'])
        text = (self.bronze / 'transcript_raw.txt').read_text()
        self.assertIn('Microfone local: fala', text)
        self.assertIn('Áudio do sistema: fala', text)
        self.assertEqual(len(self.job()['utterances']), 2)
        self.silver.assert_called_once()
        self.gold.assert_called_once()
        self.ingest.assert_called_once()
        self.assertEqual(Path(self.job()['audio_path']).read_bytes(), self.original)
        self.assertFalse(any('mock' in p.read_text() for p in checkpoints))
        saved = {p.name: p.read_bytes() for p in checkpoints}
        before = len(self.remote.calls)
        self.resume()
        self.assertEqual(len(self.remote.calls), before)
        self.assertEqual({p.name: p.read_bytes() for p in checkpoints}, saved)

    def test_invalid_remote_segments_never_checkpoint_or_advance_and_can_retry(self):
        self.stop_pending()
        self.remote.completed(2)
        remote_results = list(self.remote.root.rglob('result.json'))
        bindings = {path: {key: json.loads(path.read_text())[key] for key in ('request_sha256', 'contract')}
                    for path in remote_results}
        replies = [
            {'text': 'fala', 'segments': []},
            {'text': 'fala', 'segments': [{'text': 'outra', 'start': .1, 'end': .9}]},
            {'text': 'fala', 'segments': [{'text': 'fala', 'start': -.1, 'end': .9}]},
            {'text': 'fala', 'segments': [{'text': 'fala', 'start': .1, 'end': 9}]},
            {'text': 'fala', 'segments': [{'text': 'fala', 'start': False, 'end': .9}]},
            {'text': '', 'segments': []},
            {'text': 'fala', 'segments': [{'text': 'fala'}]}, None,
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                raws = {path: json.dumps({**reply, **binding} if isinstance(reply, dict) else reply)
                        for path, binding in bindings.items()}
                for path, raw in raws.items():
                    path.write_text(raw)
                self.assert_pending(self.resume())
                self.assertEqual(list((self.bronze / '.channels').rglob('*.json')), [])
                self.assertEqual({path: path.read_text() for path in raws}, raws)
        for path, binding in bindings.items():
            path.write_text(json.dumps({'text': 'fala', 'segments': [{'text': 'fala', 'start': .1, 'end': .9}], **binding}))
        self.resume()
        self.assertEqual(self.job()['stage'], 'done')
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 2)

    def test_silent_channel_has_no_remote_job(self):
        self.source = self.audio('0|0.3*sin(2*PI*880*t)')
        self.original = self.source.read_bytes()
        self.record()
        self.stop_pending()
        self.remote.completed()
        self.resume()
        self.assertEqual(self.job()['stage'], 'done')
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 1)
        self.assertTrue(self.job()['channels'][0]['silent'])
        self.assertEqual([u['speaker'] for u in self.job()['utterances']], ['Áudio do sistema'])

    def test_mono_keeps_generic_identity_and_canonical_mode(self):
        self.source = self.audio('0.2*sin(2*PI*440*t)')
        self.original = self.source.read_bytes()
        self.record('mic_only')
        self.stop_pending()
        self.remote.completed()
        self.resume()
        self.assertEqual([u['speaker'] for u in self.job()['utterances']], ['Áudio da gravação'])
        self.assertEqual([u['origin'] for u in self.job()['utterances']], ['gravacao_mono'])

    def test_timeout_preserves_durable_job_and_original_without_mock(self):
        self.remote.fail_transport = 'ssh'
        self.stop_pending()
        self.assertEqual(list((self.bronze / '.channels').rglob('*.json')), [])
        self.assert_pending(self.resume())
        self.remote.fail_transport = None
        self.assert_pending(self.resume())
        self.remote.completed(2)
        self.resume()
        self.assertEqual(self.job()['stage'], 'done')

    def configure(self, **changes):
        path = self.root / 'config/castanha/config.json'
        cfg = json.loads(path.read_text())
        cfg['transcription'].update(changes)
        path.write_text(json.dumps(cfg))

    def test_selection_is_persisted_before_transport_and_pinned_on_retry(self):
        from castanha.transcription import GroqTranscriber
        observed = []
        def transport(args, **kwargs):
            if args[0] in ('ssh', 'scp'):
                job_file = next(self.engine.storage.bronze_dir.glob('*/.jobs/*.json'))
                job = json.loads(job_file.read_text())
                self.assertEqual(job['selected_provider'], 'vps_ssh')
                self.assertEqual(job['provider_selection']['contract'], TEST_CONTRACT)
                observed.append(job['selected_provider'])
            return self.remote(args, **kwargs)
        with patch('castanha.transcription.subprocess.run', side_effect=transport):
            self.stop_pending()
        self.assertTrue(observed)
        selection_path = next((self.bronze / '.providers').glob('*.json'))
        saved = selection_path.read_bytes()
        self.assertNotIn(b'fixture-key', saved)
        # Trocar config/orçamento não troca a seleção de uma gravação aceita.
        self.configure(provider='groq')
        self.remote.completed(2)
        with patch.object(GroqTranscriber, '_request_groq', side_effect=AssertionError('Groq automática proibida')):
            self.resume()
        self.assertEqual(self.job()['selected_provider'], 'vps_ssh')
        self.assertEqual(selection_path.read_bytes(), saved)
        self.assertEqual({c['provider'] for c in self.job()['channels']}, {'vps_whisper_large_v3'})

    def test_manual_revision_restarts_all_channels_without_mixing_partial_vps(self):
        from castanha.transcription import GroqTranscriber
        # Keep channel 1 offline to exercise a genuinely partial VPS revision,
        # even though the first round now attempts both independent uploads.
        def block_second_upload(args, **kwargs):
            if args[0] == 'scp' and 'channel-1' in args[-2]:
                raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
            return self.remote(args, **kwargs)
        with patch('castanha.transcription.subprocess.run', side_effect=block_second_upload):
            self.stop_pending()
            self.remote.completed()
            self.assert_pending(self.resume())
        old_checkpoint = next((self.bronze / '.channels').rglob('channel-0.json'))
        old_bytes = old_checkpoint.read_bytes()
        self.configure(provider='groq', provider_revision=1, fallback_from='vps_ssh', groq_fallback_mode='manual')
        with patch('castanha.budget.BudgetManager.can_use_groq', return_value=True), \
             patch.object(GroqTranscriber, '_request_groq', return_value={
                 'text': 'nova revisão', 'segments': [{'text': 'nova revisão', 'start': .1, 'end': .9}],
                 'duration': 1}) as request:
            self.resume()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.job()['selected_provider'], 'groq')
        self.assertEqual(self.job()['fallback_from'], 'vps_ssh')
        self.assertEqual(self.job()['stage'], 'done')
        self.assertEqual({c['provider'] for c in self.job()['channels']}, {'groq'})
        self.assertEqual(old_checkpoint.read_bytes(), old_bytes)
        self.assertEqual(len(list((self.bronze / '.channels/revision-1').rglob('channel-*.json'))), 2)
        self.assertEqual(len(list((self.bronze / '.providers/history').glob('*.json'))), 1)
        self.assertNotIn('fala', (self.bronze / 'transcript_raw.txt').read_text())

    def test_default_unapproved_segmentation_remains_pending_without_ssh(self):
        self.configure(vps_segmentation_strategy=None, vps_chunk_length=None)
        self.stop_pending()
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.job()['selected_provider'], 'vps_ssh')
        self.assert_pending(self.resume())

    def test_vps_is_primary_even_with_groq_key_and_budget(self):
        from castanha.transcription import get_transcriber, VpsSshTranscriber
        from castanha.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG['transcription']['provider'], 'vps_ssh')
        self.assertEqual(DEFAULT_CONFIG['transcription']['vps_segmentation_strategy'], 'whisper-vad-v1')
        self.assertEqual(DEFAULT_CONFIG['transcription']['vps_chunk_length'], 30)
        with patch('castanha.budget.BudgetManager.can_use_groq', return_value=True):
            self.assertIsInstance(get_transcriber(), VpsSshTranscriber)
        self.assertEqual(self.remote.calls, [])

    def test_groq_selection_requires_explicit_revision(self):
        from castanha.transcription import get_transcriber, GroqTranscriber, TranscriptionPending
        self.configure(provider='groq')
        with self.assertRaises(TranscriptionPending):
            get_transcriber()
        self.configure(provider_revision=1, fallback_from='vps_ssh', groq_fallback_mode='manual')
        with patch('castanha.budget.BudgetManager.can_use_groq', return_value=True):
            self.assertIsInstance(get_transcriber(), GroqTranscriber)
        self.assertEqual(self.remote.calls, [])

    def test_explicit_mock_is_rejected_by_real_validator(self):
        # Mesmo seleção mock explícita não pode promover um canal a real.
        with patch('castanha.engine.get_transcriber', return_value=MockTranscriber()), \
             patch('castanha.budget.BudgetManager.can_use_groq', return_value=True), \
             patch.object(MockTranscriber, 'transcribe', return_value=TranscriptionResult(
                 'fala', [Utterance('Falante', 'fala', .1, .9)], 'mock', {})):
            self.stop_pending()
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(list((self.bronze / '.channels').rglob('*.json')), [])


if __name__ == '__main__':
    unittest.main()
