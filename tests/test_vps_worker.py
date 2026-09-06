import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

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


if __name__ == "__main__":
    unittest.main()
