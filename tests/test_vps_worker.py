import hashlib
import concurrent.futures
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import time
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("vps_worker", Path(__file__).parents[1] / "scripts/castanha-transcribe-v2.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class VpsWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "audio.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2:sample_rate=16000", "-ac", "1",
                        str(self.audio)], check=True, capture_output=True)
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.audio),
                              "-f", "s16le", "-"], check=True, capture_output=True).stdout
        self.request = {"namespace": "flac-mono-v1", "mode": "mic_only",
                        "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "contract": dict(worker.CONTRACT)}
        self.request["request_sha256"] = hashlib.sha256(worker.canonical(self.request).encode()).hexdigest()
        self.request["audio"] = {"path": str(self.audio), "mime_type": "audio/flac", "channels": 1, "sample_rate": 16000}
        self.manifest = self.root / "request.json"
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.request))
        self.manifest.chmod(0o600)

    def test_valid_request_and_pcm(self):
        request, audio = worker.read_request(self.manifest)
        self.assertEqual(audio, self.audio)
        self.assertAlmostEqual(worker.validate_audio(audio, request["pcm_sha256"]), 2)

    def test_private_manifest_required(self):
        self.manifest.chmod(0o644)
        with self.assertRaises(ValueError):
            worker.read_request(self.manifest)

    def test_reject_changed_contract(self):
        self.request["contract"]["multilingual"] = False
        self.save()
        with self.assertRaises(ValueError):
            worker.read_request(self.manifest)

    def test_reject_wrong_request_hash(self):
        self.request["request_sha256"] = "0" * 64
        self.save()
        with self.assertRaises(ValueError):
            worker.read_request(self.manifest)

    def test_reject_symlink_audio(self):
        link = self.root / "linked.flac"
        link.symlink_to(self.audio)
        self.request["audio"]["path"] = str(link)
        self.save()
        with self.assertRaises(ValueError):
            worker.read_request(self.manifest)

    def test_reject_corrupt_pcm(self):
        with self.assertRaises(ValueError):
            worker.validate_audio(self.audio, "0" * 64)

    def test_strict_json(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                worker.strict_json(value)

    def test_offline_multilingual_flags_and_original_language(self):
        calls = {}
        class Model:
            def __init__(self, name, **kwargs):
                calls["model"] = (name, kwargs)
            def transcribe(self, path, **kwargs):
                calls["flags"] = kwargs
                return iter([SimpleNamespace(start=0.0, end=1.0, text="Bom dia."),
                             SimpleNamespace(start=1.0, end=2.0, text="Good morning.")]), None
        result = worker.transcribe(self.request, self.audio, Model, self.root / "global.lock")
        self.assertEqual(result["text"], "Bom dia. Good morning.")
        self.assertEqual(result["request_sha256"], self.request["request_sha256"])
        self.assertTrue(calls["model"][1]["local_files_only"])
        self.assertTrue(calls["flags"]["multilingual"])
        self.assertEqual(calls["flags"]["task"], "transcribe")
        self.assertFalse(calls["flags"]["condition_on_previous_text"])

    def test_empty_result_is_explicit_no_speech_not_mock_text(self):
        class Model:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, *args, **kwargs):
                return iter([]), None
        result = worker.transcribe(self.request, self.audio, Model, self.root / "global.lock")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["segments"], [])
        self.assertIs(result["no_speech"], True)
        self.assertEqual(result["request_sha256"], self.request["request_sha256"])

    def test_describe_without_model_dependency(self):
        result = subprocess.run(["python3", str(Path(worker.__file__)), "--describe-contract"],
                                check=True, text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout), worker.CONTRACT)

    def job_command(self, delay=0):
        reply = {'request_sha256': self.request['request_sha256'],
                 'contract': worker.CONTRACT, 'text': 'fala sintética',
                 'segments': [{'start': 0, 'end': 1, 'text': 'fala sintética'}]}
        return [sys.executable, '-c',
                'import pathlib,time; '
                f'pathlib.Path({str(self.root / "executions")!r}).open("a").write("run\\n"); '
                f'time.sleep({delay!r}); print({json.dumps(reply)!r})']

    def test_queue_wait_excluded_from_deadline_and_duplicate_does_not_start(self):
        lock_path = self.root / 'asr.lock'
        with lock_path.open('w') as lock, concurrent.futures.ThreadPoolExecutor(1) as pool:
            fcntl.flock(lock, fcntl.LOCK_EX)
            future = pool.submit(worker.run_job, self.manifest, .3,
                                 worker_command=self.job_command(), lock_path=lock_path)
            deadline = time.monotonic() + 3
            status = self.root / 'status.json'
            while not status.exists():
                if time.monotonic() > deadline:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    self.fail('supervisor não entrou na fila')
                time.sleep(.01)
            self.assertEqual(json.loads(status.read_text())['phase'], 'queued')
            time.sleep(.5)  # maior que o prazo de execução
            self.assertFalse(future.done())
            self.assertEqual(worker.run_job(self.manifest, .3,
                worker_command=self.job_command(), lock_path=lock_path), 0)
            self.assertFalse((self.root / 'executions').exists())
            fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertEqual(future.result(timeout=3), 0)
        self.assertEqual((self.root / 'executions').read_text(), 'run\n')
        self.assertEqual(json.loads(status.read_text())['phase'], 'completed')
        saved = (self.root / 'result.json').read_bytes()
        worker.run_job(self.manifest, .3, worker_command=self.job_command(), lock_path=lock_path)
        self.assertEqual((self.root / 'executions').read_text(), 'run\n')
        self.assertEqual((self.root / 'result.json').read_bytes(), saved)

    def test_execution_timeout_preserves_audio_and_releases_queue_for_next_job(self):
        lock_path = self.root / 'asr.lock'
        original = self.audio.read_bytes()
        with self.assertRaises(subprocess.TimeoutExpired):
            worker.run_job(self.manifest, .15, worker_command=self.job_command(10), lock_path=lock_path)
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['phase'], 'failed')
        self.assertEqual(status['error'], 'execution_timeout')
        self.assertFalse((self.root / 'result.json').exists())
        self.assertEqual(self.audio.read_bytes(), original)
        worker.run_job(self.manifest, 1, worker_command=self.job_command(), lock_path=lock_path)
        self.assertTrue((self.root / 'result.json').exists())

    def test_invalid_child_result_never_becomes_completed(self):
        with self.assertRaises(ValueError):
            worker.run_job(self.manifest, 1, worker_command=[sys.executable, '-c', 'print("{}")'],
                           lock_path=self.root / 'asr.lock')
        self.assertEqual(json.loads((self.root / 'status.json').read_text())['phase'], 'failed')
        self.assertFalse((self.root / 'result.json').exists())

    def test_inherited_lock_must_be_the_actual_global_lock(self):
        wrong = self.root / 'wrong.lock'
        actual = self.root / 'asr.lock'
        actual.touch(mode=0o600)
        with wrong.open('w') as fd, self.assertRaises(ValueError):
            with worker.asr_lock(actual, fd.fileno()):
                self.fail('lock incorreto foi aceito')

    def test_timeout_exit_race_keeps_timeout_diagnosis_and_reaps_child(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired(['fixture'], .1), 0]
        with patch.object(worker.subprocess, 'Popen', return_value=process), \
             patch.object(worker.os, 'killpg', side_effect=ProcessLookupError), \
             self.assertRaises(subprocess.TimeoutExpired):
            worker.run_job(self.manifest, .1, worker_command=['fixture'], lock_path=self.root / 'asr.lock')
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(json.loads((self.root / 'status.json').read_text())['error'], 'execution_timeout')


if __name__ == "__main__":
    unittest.main()
