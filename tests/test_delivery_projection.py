"""Read-only delivery UI projections, real CLI and frozen synthetic originals."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha.durability import write_json
from castanha.engine import CastanhaEngine
from castanha.legacy_recovery import prepare_legacy_manifest, submit_legacy_recovery
from castanha.storage import MeetingStorage
from tests.test_bronze_sync import FakeMcp

ROOT = Path(__file__).resolve().parents[1]


class DeliveryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = MeetingStorage(self.root / 'meetings')
        self.slug = 'synthetic-meeting'
        self.bronze = self.storage.bronze_dir / self.slug
        self.bronze.mkdir()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=0.2', '-c:a', 'libopus',
                        str(self.bronze / 'audio.ogg')], check=True, capture_output=True)
        self.metadata = {'slug': self.slug, 'title': 'Synthetic meeting',
            'recorded_at': '2026-09-05T10:30:00', 'transcription_provider': 'groq',
            'recordings': [{'id': 'audio.ogg', 'job_id': None}], 'audio_status': 'ok',
            'zinom': {'status': 'error', 'errors': ['HTTP 530']}}
        write_json(self.bronze / 'metadata.json', self.metadata)
        (self.bronze / 'transcript_raw.txt').write_bytes(('Synthetic decisão 🧠\r\n' * 100).encode())
        self.client = FakeMcp()
        self.manifest = prepare_legacy_manifest(self.bronze)
        self.assertEqual(self.submit()['status'], 'ok')
        self.checkpoint = next((self.bronze / '.legacy-recovery/uploads').glob('*.json'))
        self.last = {'slug': self.slug, 'title': 'Original', 'zinom': self.metadata['zinom']}
        self.state = {'status': 'idle', 'last_result': self.last}

    def submit(self):
        return submit_legacy_recovery(self.bronze, self.client, workspace='fixture-workspace')

    def hashes(self):
        return {str(path.relative_to(self.bronze)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.bronze.rglob('*') if path.is_file()}

    def test_storage_engine_and_cli_project_completed_receipt_without_any_original_write(self):
        before = self.hashes()
        state_before = copy.deepcopy(self.state)
        calls = len(self.client.calls)
        engine = CastanhaEngine.__new__(CastanhaEngine)
        engine.storage = self.storage
        engine.state_mgr = Mock()
        engine.state_mgr.read.return_value = self.state
        with patch('castanha.legacy_recovery._audio_info', side_effect=AssertionError('No audio probing')), patch(
                'castanha.legacy_recovery.file_sha256', side_effect=AssertionError('No audio hashing')):
            self.assertEqual(engine.get_status()['last_result']['zinom']['status'], 'ok')
            self.assertEqual(self.storage.list_recent_meetings()[0]['zinom']['status'], 'ok')
            self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'ok')
        engine.state_mgr.write.assert_not_called()
        self.assertEqual(self.state, state_before)
        config_root, state_root = self.root / 'config', self.root / 'state'
        write_json(config_root / 'castanha/config.json', {'storage': {
            'base_dir': str(self.storage.base_dir), 'bronze_dir': str(self.storage.bronze_dir),
            'silver_dir': str(self.storage.silver_dir), 'gold_dir': str(self.storage.gold_dir)}})
        state_file = state_root / 'castanha/state.json'
        write_json(state_file, self.state)
        state_bytes = state_file.read_bytes()
        env = {**os.environ, 'XDG_CONFIG_HOME': str(config_root), 'XDG_STATE_HOME': str(state_root)}
        for args in (['status', '--json'], ['notes', '--json'], ['notes', self.slug, '--json']):
            result = subprocess.run([sys.executable, str(ROOT / 'bin/castanha'), *args], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            note = payload.get('last_result') or (payload['notes'][0] if 'notes' in payload else payload)
            self.assertEqual(note['zinom']['status'], 'ok')
            self.assertEqual(note['zinom']['ingestion']['state'], 'completed')
            self.assertNotIn('HTTP 530', json.dumps(note['zinom']))
        self.assertEqual(state_file.read_bytes(), state_bytes)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(len(self.client.calls), calls)

    def test_missing_corrupt_mismatched_and_unconfirmed_receipts_never_show_delivered(self):
        saved = json.loads(self.checkpoint.read_bytes())
        for mutate in (
            lambda x: x.update(status='ok', result=None),
            lambda x: x['result']['ingestion'].update(state='processing'),
            lambda x: x['result']['ingestion'].update(source_id='castanha:wrong'),
            lambda x: x['result']['ingestion'].update(job_id=True),
            lambda x: x.update(attempted=False),
            lambda x: x['request']['envelope'].update(texto='Changed'),
        ):
            value = copy.deepcopy(saved)
            mutate(value)
            write_json(self.checkpoint, value)
            self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'error')
            self.assertEqual(self.storage.status_projection(self.state)['last_result']['zinom']['status'], 'error')
        self.checkpoint.write_text('{broken')
        self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'error')
        self.checkpoint.unlink()
        self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'error')

    def test_manifest_destination_and_text_binding_are_checked_without_rewriting_metadata(self):
        original = (self.bronze / 'metadata.json').read_bytes()
        for path in (self.bronze / '.legacy-recovery/manifest.json',
                     self.bronze / '.legacy-recovery/destination.json', self.bronze / 'transcript_raw.txt'):
            before = path.read_bytes()
            path.write_bytes(b'corrupt')
            self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'error')
            self.assertEqual(path.read_bytes(), b'corrupt')
            path.write_bytes(before)
        self.assertEqual((self.bronze / 'metadata.json').read_bytes(), original)
        target = self.checkpoint.with_suffix('.backup')
        self.checkpoint.rename(target)
        self.checkpoint.symlink_to(target)
        self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'error')

    def test_terminal_receipt_overrides_old_success_and_never_resurrects(self):
        key = next(iter(self.client.requests))
        self.client.states[key] = 'tombstoned'
        self.assertEqual(self.submit()['status'], 'tombstoned')
        before = self.hashes()
        self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'], 'tombstoned')
        self.assertEqual(self.hashes(), before)

    def test_no_recovery_and_unrelated_slug_keep_original_status(self):
        previous = {'status': 'error', 'errors': ['Original']}
        self.assertEqual(self.storage.delivery_projection('unrelated', previous), previous)
        self.assertEqual(self.storage.delivery_projection('../synthetic-meeting', previous), previous)


if __name__ == '__main__':
    unittest.main()
