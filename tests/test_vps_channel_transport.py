"""Contrato SSH/SCP local: FFmpeg e processo detached reais, sem rede ou modelo."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from castanha.transcription import TranscriptionPending, VpsSshTranscriber, audio_mime


# Valores exclusivamente sintéticos para testar protocolo; não aprovam produção.
TEST_CONTRACT = {'contract': 'faster-whisper-json-v2', 'model': 'large-v3',
                 'compute_type': 'int8', 'cpu_threads': 8, 'multilingual': True,
                 'condition_on_previous_text': False, 'chunk_length': 30,
                 'segmentation_strategy': 'synthetic-fixture-only'}


class LocalVps:
    """Executa o comando remoto no sandbox; somente ssh/scp são substituídos."""

    def __init__(self, root, reply=None):
        self.root = root
        root.mkdir()
        self.run = subprocess.run
        self.calls = []
        self.lose_reply = False
        self.fail_transport = None
        self.worker = root / 'worker.py'
        self.contract_file = root / 'contract.json'
        self.contract_file.write_text(json.dumps(TEST_CONTRACT))
        self.settings = root / 'settings.json'
        self.settings.write_text(json.dumps({'delay': .15, 'exit_code': 0}))
        self.set_reply(reply or {'text': 'fala', 'segments': [{'start': .1, 'end': .9, 'text': 'fala'}]})
        self.worker.write_text('''#!/usr/bin/python3
import json, pathlib, stat, sys, time
root = pathlib.Path(__file__).parent
contract = json.loads((root / 'contract.json').read_text())
if sys.argv[1:] == ['--describe-contract']:
    print(json.dumps(contract))
    sys.exit(0)
request = None
if sys.argv[1] == '--request':
    path = pathlib.Path(sys.argv[2])
    assert path.is_file() and not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    request = json.loads(path.read_text())
    assert request['contract'] == contract
    assert request['audio']['sample_rate'] == 16000
    assert request['audio']['channels'] == 1
    assert request['audio']['mime_type'] == 'audio/flac'
with (root / 'count').open('a') as count:
    count.write('started\\n')
settings = json.loads((root / 'settings.json').read_text())
time.sleep(settings['delay'])
if settings['exit_code']:
    sys.exit(settings['exit_code'])
reply = json.loads((root / 'reply.json').read_text())
if request and isinstance(reply, dict):
    reply.setdefault('request_sha256', request['request_sha256'])
    reply.setdefault('contract', contract)
print(json.dumps(reply))
''')
        self.worker.chmod(0o700)

    def set_reply(self, reply):
        (self.root / 'reply.json').write_text(json.dumps(reply))

    def __call__(self, args, **kwargs):
        if args[0] not in ('ssh', 'scp'):
            return self.run(args, **kwargs)
        self.calls.append((args, kwargs))
        if self.fail_transport == args[0]:
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        if args[0] == 'scp':
            shutil.copyfile(args[-2], self.root / args[-1].split(':', 1)[1])
            return subprocess.CompletedProcess(args, 0, '', '')
        command = args[-1].replace('/root/castanha-transcribe-v2.py', str(self.worker))
        command = command.replace('/root/castanha-transcribe.py', str(self.worker))
        result = self.run(['sh', '-c', command], cwd=self.root, **kwargs)
        if self.lose_reply and 'nohup flock' in command:
            self.lose_reply = False
            raise subprocess.TimeoutExpired('ssh', 15)
        return result

    def completed(self, count=1):
        deadline = time.monotonic() + 5
        while len(list(self.root.rglob('result.json'))) < count:
            if time.monotonic() >= deadline:
                raise AssertionError('Processo local não concluiu')
            time.sleep(.03)


class VpsChannelTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.flac('channel.flac')
        self.original = self.source.read_bytes()
        self.remote = LocalVps(self.root / 'remote')
        patcher = patch('castanha.transcription.subprocess.run', side_effect=self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)

    def flac(self, name, rate=16000, channels=1):
        path = self.root / name
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', '-ar', str(rate), '-ac', str(channels), str(path)],
                       check=True, capture_output=True)
        return path

    def submit(self, path=None, mode='mic_only'):
        with self.assertRaises(TranscriptionPending):
            VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(path or self.source, mode=mode)

    def test_flac_remains_flac_and_uses_bounded_detached_job(self):
        self.submit()
        self.remote.completed()
        calls = self.remote.calls
        self.assertEqual(len(calls), 6)
        self.assertTrue(calls[2][0][-1].endswith('.flac'))
        self.assertIn('audio.flac', calls[3][0][-1])
        self.assertIn('request.json', calls[5][0][-1])
        self.assertIn('nohup flock -n', calls[5][0][-1])
        self.assertEqual([kw['timeout'] for _, kw in calls], [15, 15, 30, 15, 15, 15])
        uploaded = next(self.remote.root.rglob('audio.flac'))
        manifest = json.loads((uploaded.parent / 'request.json').read_text())
        self.assertEqual(manifest['namespace'], 'flac-mono-v1')
        self.assertEqual(manifest['contract'], TEST_CONTRACT)
        self.assertEqual(manifest['mode'], 'mic_only')
        self.assertEqual(uploaded.read_bytes(), self.original)
        self.assertEqual(audio_mime(uploaded), 'audio/flac')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_ogg_keeps_exact_legacy_job_identity_bytes_and_completed_replay(self):
        path = self.root / 'legacy.ogg'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source), '-c:a', 'libopus', str(path)],
                       capture_output=True, check=True)
        original = path.read_bytes()
        digest = hashlib.sha256(original + b'dual:whisper-large-v3').hexdigest()
        job = self.remote.root / '.local/state/castanha/jobs' / digest
        job.mkdir(parents=True)
        (job / 'audio.ogg').write_bytes(original)
        reply = json.dumps({'text': 'fala', 'segments': [{'text': 'fala', 'start': .1, 'end': .9}]})
        (job / 'result.json').write_text(reply)
        result = VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(path)
        self.assertEqual(result.utterances[0].speaker, 'Falante')
        self.assertEqual(result.text, 'fala')
        self.assertEqual(len(self.remote.calls), 1)
        self.assertIn(digest + '/audio.ogg', self.remote.calls[0][0][-1])
        self.assertEqual((job / 'result.json').read_text(), reply)
        self.assertEqual((job / 'audio.ogg').read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)

    def test_ogg_upload_or_pending_job_never_executes_old_worker(self):
        path = self.root / 'pending.ogg'
        path.write_bytes(b'synthetic legacy bytes')
        for ready in (False, True):
            if ready:
                digest = hashlib.sha256(path.read_bytes() + b'dual:whisper-large-v3').hexdigest()
                job = self.remote.root / '.local/state/castanha/jobs' / digest
                job.mkdir(parents=True)
                (job / 'audio.ogg').write_bytes(path.read_bytes())
            with self.assertRaisesRegex(TranscriptionPending, 'pending_worker_contract'):
                VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(path)
        self.assertEqual(len(self.remote.calls), 2)
        self.assertFalse(any(args[0] == 'scp' or 'nohup' in args[-1] for args, _ in self.remote.calls))
        self.assertEqual(path.read_bytes(), b'synthetic legacy bytes')

    def test_retry_after_lost_response_uses_same_job_and_no_second_upload(self):
        self.remote.lose_reply = True
        self.submit()
        self.remote.completed()
        result = VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(self.source, mode='mic-only')
        self.assertEqual(result.provider, 'vps_whisper_large_v3')
        self.assertEqual(result.utterances[0].start, .1)
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 1)
        self.assertEqual((self.remote.root / 'count').read_text().splitlines(), ['started'])
        self.assertEqual(self.remote.calls[0][0], self.remote.calls[-1][0])

    def test_normalizes_high_rate_without_modifying_original(self):
        path = self.flac('high.flac', rate=48000)
        original = path.read_bytes()
        self.submit(path)
        self.remote.completed()
        uploaded = next(self.remote.root.rglob('audio.flac'))
        info = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=channels,sample_rate',
                               '-of', 'json', str(uploaded)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(info.stdout)['streams'], [{'sample_rate': '16000', 'channels': 1}])
        self.assertEqual(path.read_bytes(), original)
        VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(path, mode='mic_only')
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 1)

    def test_invalid_mode_stereo_corruption_and_disguised_container_never_send(self):
        stereo = self.flac('stereo.flac', channels=2)
        corrupt = self.root / 'corrupt.flac'
        corrupt.write_bytes(b'fLaC' + b'broken stream' * 20)
        disguised = self.root / 'disguised.flac'
        disguised.write_bytes(b'OggS' + self.original[4:])
        damaged = self.root / 'damaged.flac'
        damaged.write_bytes(self.original[:-100])
        for path, mode in [(self.source, 'dual'), (stereo, 'mic_only'), (corrupt, 'mic_only'),
                           (disguised, 'mic_only'), (damaged, 'mic_only'), (self.root / 'bad.mp3', 'mic_only')]:
            with self.subTest(path=path.name, mode=mode):
                before = path.read_bytes() if path.exists() else None
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(path, mode)
                self.assertEqual(self.remote.calls, [])
                if before is not None:
                    self.assertEqual(path.read_bytes(), before)

    def test_ssh_and_scp_timeout_preserve_input_and_retry(self):
        for transport in ('ssh', 'scp'):
            self.remote.fail_transport = transport
            self.submit()
            self.assertEqual(self.source.read_bytes(), self.original)
        self.remote.fail_transport = None
        self.submit()
        self.remote.completed()
        self.assertEqual(VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(self.source, 'mic_only').text, 'fala')

    def test_worker_timeout_keeps_remote_audio_and_retry_restarts_same_job(self):
        self.remote.settings.write_text(json.dumps({'delay': 3, 'exit_code': 7}))
        with patch('castanha.transcription.VPS_MIN_TIMEOUT_SEC', 1), \
             patch('castanha.transcription.VPS_MAX_TIMEOUT_SEC', 1):
            self.submit()
        job_dir = next(self.remote.root.rglob('audio.flac')).parent
        deadline = time.monotonic() + 5
        while True:
            started = (self.remote.root / 'count').exists()
            # Só consultar o lock depois do início: adquiri-lo antes competiria
            # com o flock -n do worker e poderia impedir o próprio teste de iniciar.
            unlocked = started and self.remote.run(
                ['flock', '-n', str(job_dir / 'job.lock'), 'true']).returncode == 0
            if unlocked:
                break
            if time.monotonic() > deadline:
                self.fail('Worker fake não respeitou timeout')
            time.sleep(.03)
        self.assertFalse((job_dir / 'result.json').exists())
        self.assertEqual((job_dir / 'audio.flac').read_bytes(), self.original)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.remote.settings.write_text(json.dumps({'delay': .15, 'exit_code': 0}))
        self.remote.set_reply({'text': 'fala', 'segments': [{'text': 'fala', 'start': .1, 'end': .9}]})
        self.submit()
        self.remote.completed()
        self.assertEqual(VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(self.source, 'mic_only').text, 'fala')
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 1)
        self.assertEqual((self.remote.root / 'count').read_text().splitlines(), ['started', 'started'])

    def test_recompressed_same_pcm_reuses_pending_job(self):
        self.remote.lose_reply = True
        self.submit()
        self.remote.completed()
        recompressed = self.root / 'recompressed.flac'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source), '-c:a', 'flac',
                        '-compression_level', '12', '-metadata', 'comment=fixture', str(recompressed)],
                       check=True, capture_output=True)
        self.assertNotEqual(self.source.read_bytes(), recompressed.read_bytes())
        result = VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(recompressed, 'mic_only')
        self.assertEqual(result.text, 'fala')
        self.assertEqual(sum(args[0] == 'scp' for args, _ in self.remote.calls), 1)
        self.assertEqual((self.remote.root / 'count').read_text().splitlines(), ['started'])

    def test_unknown_strategy_or_chunk_length_never_authorizes_transport(self):
        for field in ('segmentation_strategy', 'chunk_length', 'model'):
            with self.subTest(field=field):
                contract = {**TEST_CONTRACT, field: None}
                with self.assertRaises(TranscriptionPending):
                    VpsSshTranscriber('fixture', contract).transcribe(self.source, 'mic_only')
        self.assertEqual(self.remote.calls, [])

    def test_worker_attestation_must_match_every_field_before_upload(self):
        for field, wrong in [('model', 'other'), ('compute_type', 'float32'), ('cpu_threads', True),
                             ('multilingual', False), ('condition_on_previous_text', True),
                             ('chunk_length', 15), ('segmentation_strategy', 'unapproved'),
                             ('contract', 'v1'), ('extra', True)]:
            with self.subTest(field=field):
                self.remote.contract_file.write_text(json.dumps({**TEST_CONTRACT, field: wrong}))
                self.submit()
                self.assertFalse(any(args[0] == 'scp' for args, _ in self.remote.calls))
        self.remote.contract_file.write_text(json.dumps(TEST_CONTRACT))
        self.submit()
        self.remote.completed()

    def test_attestation_rejects_duplicate_fields_and_nonfinite_json(self):
        for reply in ('{"contract":"bad","contract":"faster-whisper-json-v2"}',
                      '{"cpu_threads":NaN}'):
            def transport(args, **kwargs):
                if args[0] == 'ssh' and '--describe-contract' in args[-1]:
                    return subprocess.CompletedProcess(args, 0, reply, '')
                return self.remote(args, **kwargs)
            with self.subTest(reply=reply), patch('castanha.transcription.subprocess.run', side_effect=transport):
                self.submit()
        self.assertFalse(any(args[0] == 'scp' for args, _ in self.remote.calls))

    def test_derived_transport_is_outside_recording_inventory(self):
        high = self.flac('capture.flac', rate=48000)
        inventory = sorted(self.root.glob('*.flac'))
        self.submit(high)
        self.remote.completed()
        self.assertEqual(sorted(self.root.glob('*.flac')), inventory)
        self.assertTrue((self.root / '.vps-transport' / 'capture.vps-16k.flac').exists())

    def test_local_validation_timeout_never_sends(self):
        real = self.remote.run
        def timeout(args, **kwargs):
            if args[0] == 'ffmpeg':
                raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
            return real(args, **kwargs)
        with patch('castanha.transcription.subprocess.run', side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                VpsSshTranscriber('fixture', TEST_CONTRACT).transcribe(self.source, 'mic_only')
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
