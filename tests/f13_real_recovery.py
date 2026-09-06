"""Opt-in synthetic canary. Never discovered as a normal unit test.

submit kills only its own SSH connection after the detached launch barrier.
observe runs independently on the VPS, without a client or a model invocation.
replay must only read the existing result. Evidence directories are never reset.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from castanha.config import DEFAULT_CONFIG
from castanha.transcription import VpsSshTranscriber, TranscriptionPending, vps_contract, _vps_mono_flac
from castanha.channel_transcription import pcm_sha256
from castanha.bronze_ingest import build_transcript_request

# Reference synthesis input is versioned in 19f72c7:tests/asr_quality_probe.py.
SYNTHETIC_SHA = '6800c9c62e64f37a160058528616600b1a624c1fb045724f8c7488a894d63fd8'
WORKER_SHA = 'e38007da8ac6fd17f0fd0b99f39618c15cb8a2bc6e5087209970839fe76ba422'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def bronze_request(result):
    return build_transcript_request('f13-synthetic-recovery', {
        'title': 'F13 synthetic PT EN recovery, no user recording',
        'recorded_at': '2026-09-06T03:30:00Z', 'audio_status': 'ok',
        'transcription_provider': 'vps_whisper_large_v3',
    }, result['text'], captured_at='2026-09-06T04:00:00Z', workspace='personal',
       recording_id='f13-synthetic:' + SYNTHETIC_SHA)


def observe(job, output):
    """Only inspect this exact canary job, never start/retry its worker."""
    assert len(job) == 64 and all(c in '0123456789abcdef' for c in job)
    root = Path.home() / '.local/state/castanha/jobs' / job
    request = json.loads((root / 'request.json').read_text())
    assert request['request_sha256'] == job
    deadline = time.monotonic() + 180
    seen = set()
    while not (root / 'result.json').exists():
        for proc in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                argv = proc.read_bytes().split(b'\0')
            except (OSError, ProcessLookupError):
                continue
            if (argv and b'python' in Path(os.fsdecode(argv[0])).name.encode()
                    and b'/root/castanha-transcribe-v2.py' in argv
                    and b'--request' in argv
                    and any(job.encode() in arg for arg in argv)):
                seen.add(int(proc.parent.name))
        assert time.monotonic() < deadline, 'Remote result still pending, do not resubmit'
        time.sleep(.25)
    result = json.loads((root / 'result.json').read_text())
    assert result['request_sha256'] == job and result['contract'] == request['contract']
    assert result['text'].strip() and result['segments']
    previous = 0
    for segment in result['segments']:
        assert all(math.isfinite(segment[k]) for k in ('start', 'end'))
        assert previous <= segment['start'] <= segment['end'] <= 42.2
        previous = segment['end']
    save(output, {'job_id': job, 'status': 'completed', 'observer_pid': os.getpid(),
                  'worker_processes_observed': sorted(seen),
                  'result_sha256': digest(root / 'result.json'),
                  'result_mtime_ns': (root / 'result.json').stat().st_mtime_ns,
                  'request': request, 'result': result})
    print(json.dumps({'status': 'completed', 'job_id': job, 'observed_worker_pids': sorted(seen)}))


def client_phase(phase, audio, host, directory):
    assert digest(audio) == SYNTHETIC_SHA, 'Only the versioned synthetic fixture is authorized'
    contract = vps_contract(DEFAULT_CONFIG['transcription'])
    transport = _vps_mono_flac(audio)
    identity = {'namespace': 'flac-mono-v1', 'mode': 'mic_only',
                'pcm_sha256': pcm_sha256(transport), 'contract': contract}
    job = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    assert not (directory / (phase + '.json')).exists(), 'Phase already recorded, never rerun submission'
    actual_run = subprocess.run
    if phase == 'submit':
        attestation = actual_run(['ssh', '-o', 'ConnectTimeout=5', host,
                                  'sha256sum /root/castanha-transcribe-v2.py'],
                                 capture_output=True, text=True, check=True, timeout=15)
        assert attestation.stdout.split()[0] == WORKER_SHA
        save(directory / 'submit-intent.json', {'job_id': job, 'worker_sha256': WORKER_SHA})
    calls = []
    def run(argv, **kwargs):
        if argv[0] not in ('ssh', 'scp'):
            return actual_run(argv, **kwargs)
        launch = argv[0] == 'ssh' and 'nohup flock -n' in argv[-1]
        calls.append({'transport': argv[0], 'launch': launch, 'timeout': kwargs.get('timeout')})
        if phase == 'replay':
            assert len(calls) == 1 and argv[0] == 'ssh' and not launch, 'Replay is read-only; use observe for pending jobs'
        if phase == 'submit' and launch:
            # Keep the canary connection alive after the actual product launch,
            # then terminate this PID only. The worker is already nohup/detached.
            command = argv[:-1] + [argv[-1] + " printf 'F13_LAUNCHED\\n'; sleep 30"]
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(proc.stdout, selectors.EVENT_READ)
                    assert ready.select(15), 'No launch barrier, canary outcome unknown'
                    assert proc.stdout.readline().strip() == b'F13_LAUNCHED'
                proc.terminate()
                proc.wait(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            raise subprocess.TimeoutExpired('ssh-canary-disconnected-after-launch', 15)
        response = actual_run(argv, **kwargs)
        if phase == 'submit' and len(calls) == 1:
            assert response.returncode == 0 and response.stdout.strip() == 'UPLOAD', 'Job already exists or outcome unknown; do not relaunch'
        return response
    with patch('castanha.transcription.subprocess.run', side_effect=run):
        try:
            result = VpsSshTranscriber(host, contract).transcribe(audio, mode='mic_only')
        except TranscriptionPending:
            assert phase == 'submit', 'Remote pending, preserve job; do not relaunch ASR'
            assert sum(c['transport'] == 'scp' for c in calls) == 1
            assert sum(c['launch'] for c in calls) == 1
            status = 'pending_after_connection_loss'
        else:
            assert phase == 'replay', 'Canary job already exists, cannot claim a fresh experiment'
            assert len(calls) == 1 and calls[0]['transport'] == 'ssh' and not calls[0]['launch']
            assert result.raw_response['request_sha256'] == job
            save(directory / 'transcript.json', result.raw_response)
            save(directory / 'bronze-request.json', bronze_request(result.raw_response))
            status = 'completed_existing_job'
    assert digest(audio) == SYNTHETIC_SHA
    receipt = {'phase': phase, 'status': status, 'job_id': job,
               'original_sha256': SYNTHETIC_SHA, 'calls': calls}
    save(directory / (phase + '.json'), receipt)
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('submit', 'observe', 'replay'))
    parser.add_argument('--audio', type=Path)
    parser.add_argument('--host')
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--job')
    args = parser.parse_args()
    if args.phase == 'observe':
        observe(args.job, args.evidence)
    else:
        assert args.audio and args.host
        client_phase(args.phase, args.audio, args.host, args.evidence)
