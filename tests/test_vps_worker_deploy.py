import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / 'candidate.py'
        self.target = self.root / 'worker.py'
        self.script = Path(__file__).parents[1] / 'scripts/deploy-vps-worker.sh'
        self.candidate.write_text('print("contract-ok")\n')

    def deploy(self, sha=None):
        sha = sha or hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        return subprocess.run(['bash', str(self.script), str(self.candidate), sha,
                               str(self.target), sys.executable], capture_output=True)

    def test_install_and_preserve_previous(self):
        self.target.write_text('previous')
        self.assertEqual(self.deploy().returncode, 0)
        self.assertEqual(self.target.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(next(self.root.glob('worker.py.previous.*')).read_text(), 'previous')

    def test_wrong_sha_never_replaces(self):
        self.target.write_text('previous')
        self.assertNotEqual(self.deploy('0' * 64).returncode, 0)
        self.assertEqual(self.target.read_text(), 'previous')

    def test_health_failure_restores_previous(self):
        self.target.write_text('previous')
        self.candidate.write_text('from pathlib import Path\nimport sys\n'
                                  'if Path(__file__).name == "worker.py": sys.exit(1)\n'
                                  'print("contract-ok")\n')
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertEqual(self.target.read_text(), 'previous')

    def test_symlink_target_refused(self):
        other = self.root / 'other'
        other.write_text('protected')
        self.target.symlink_to(other)
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertEqual(other.read_text(), 'protected')
