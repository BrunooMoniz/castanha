"""Validate the archived synthetic real-service receipt, no network/model calls."""
import hashlib
import json
from pathlib import Path
import unittest
import subprocess
import tempfile
from unittest import mock

from tests.f13_real_recovery import SYNTHETIC_SHA, bronze_request
from tests import f13_real_recovery as canary


class RealRecoveryEvidence(unittest.TestCase):
    def test_canary_replay_cannot_restart_pending_remote_worker(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(canary, 'digest', return_value=SYNTHETIC_SHA), \
             mock.patch.object(canary, '_vps_mono_flac', side_effect=lambda p: p), \
             mock.patch.object(canary, 'pcm_sha256', return_value='0' * 64), \
             mock.patch('castanha.transcription._vps_mono_flac', side_effect=lambda p: p), \
             mock.patch('castanha.channel_transcription.pcm_sha256', return_value='0' * 64), \
             mock.patch.object(canary.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'READY', '')) as run:
            with self.assertRaisesRegex(AssertionError, 'Replay is read-only'):
                canary.client_phase('replay', Path(folder) / 'synthetic.flac', 'fixture', Path(folder))
            self.assertEqual(run.call_count, 1)  # No attestation, upload or launch.

    def test_canary_submit_refuses_an_existing_job_before_any_relaunch(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(canary, 'digest', return_value=SYNTHETIC_SHA), \
             mock.patch.object(canary, '_vps_mono_flac', side_effect=lambda p: p), \
             mock.patch.object(canary, 'pcm_sha256', return_value='0' * 64), \
             mock.patch('castanha.transcription._vps_mono_flac', side_effect=lambda p: p), \
             mock.patch('castanha.channel_transcription.pcm_sha256', return_value='0' * 64), \
             mock.patch.object(canary.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0, canary.WORKER_SHA + ' worker', ''),
                 subprocess.CompletedProcess([], 0, 'READY', '')]) as run:
            with self.assertRaisesRegex(AssertionError, 'Job already exists'):
                canary.client_phase('submit', Path(folder) / 'synthetic.flac', 'fixture', Path(folder))
            self.assertEqual(run.call_count, 2)

    def test_connection_loss_independent_completion_and_no_duplicate_submission(self):
        proof = json.loads((Path(__file__).parent / 'fixtures/f13-real-recovery.json').read_text())
        submit, observer, replay = (proof[k] for k in ('submit', 'observer', 'replay'))
        self.assertEqual(submit['status'], 'pending_after_connection_loss')
        self.assertEqual(observer['status'], 'completed')
        self.assertEqual(submit['job_id'], observer['job_id'])
        self.assertEqual(replay['job_id'], observer['job_id'])
        self.assertEqual(submit['original_sha256'], SYNTHETIC_SHA)
        self.assertEqual(replay['original_sha256'], SYNTHETIC_SHA)
        self.assertEqual(sum(c['transport'] == 'scp' for c in submit['calls']), 1)
        self.assertEqual(sum(c['launch'] for c in submit['calls']), 1)
        self.assertEqual(replay['calls'], [{'transport': 'ssh', 'launch': False, 'timeout': 15}])
        self.assertEqual(len(observer['worker_processes_observed']), 1)
        self.assertNotIn(observer['observer_pid'], observer['worker_processes_observed'])
        self.assertEqual(observer['request']['request_sha256'], observer['result']['request_sha256'])
        self.assertEqual(observer['request']['contract'], observer['result']['contract'])
        self.assertEqual(bronze_request(observer['result']), proof['bronze_request'])
        envelope = proof['bronze_request']['envelope']
        self.assertEqual(envelope['texto'], observer['result']['text'])
        self.assertEqual(envelope['proveniencia']['sha256_texto'],
                         hashlib.sha256(envelope['texto'].encode()).hexdigest())
        self.assertEqual(envelope['fidelidade'], 'projecao')
        self.assertEqual(proof['bronze_request']['facts'], [])
