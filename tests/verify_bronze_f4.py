"""Prova do payload Castanha no parser F4 real, sem build, banco ou rede.

Uso: python3 -m tests.verify_bronze_f4 /caminho/worktree-f4
Exige fonte limpa no contrato f45050b e tsx já disponível nessa worktree.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from castanha.bronze_ingest import build_transcript_request


def verify(root: Path):
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert revision == "f45050b3d24f70b512220bc637541b2bf6c93ff6", revision
    subprocess.run(["git", "-C", str(root), "diff", "--exit-code", "HEAD", "--", "src"], check=True)
    text = "Decisão com ação e emoji 🌰\r\n" * 4000
    request = build_transcript_request("fixture", {"title": "Reunião sintética", "transcription_provider": "groq"},
        text, captured_at="2026-09-05T10:00:00-03:00", workspace="fixture-workspace",
        recording_id="native-recording-fixture")
    changed = build_transcript_request("renamed", {"title": "Título corrigido", "transcription_provider": "groq"},
        text + "Correção\r\n", captured_at="2026-09-06T10:00:00-03:00", workspace="fixture-workspace",
        recording_id="native-recording-fixture")
    with tempfile.TemporaryDirectory(prefix="castanha-f4-fixture-") as directory:
        payload = Path(directory) / "payload.json"
        payload.write_text(json.dumps([request, changed], ensure_ascii=False), encoding="utf-8")
        script = Path(directory) / "verify.mts"
        script.write_text('''import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { parseBronzeRequest } from ''' + json.dumps((root / "src/rag/bronze-ingest.ts").as_uri()) + ''';
const raw = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const parsed = raw.map((request) => {
  assert.equal(request.envelope.account_id, undefined);
  const parsed = parseBronzeRequest({...request, envelope: {...request.envelope, account_id: 'synthetic-account'}});
  assert.equal(parsed.envelope.texto, request.envelope.texto);
  assert.equal(parsed.envelope.workspace, 'fixture-workspace');
  assert.equal(parsed.envelope.proveniencia.sha256_texto, createHash('sha256').update(request.envelope.texto).digest('hex'));
  assert.equal(parsed.envelope.source_id, parsed.envelope.proveniencia.origem_id);
  assert.deepEqual(parsed.facts, []);
  return parsed;
});
assert.equal(parsed[0].envelope.source_id, parsed[1].envelope.source_id);
assert.notEqual(parsed[0].idempotencyKey, parsed[1].idempotencyKey);
console.log(JSON.stringify({parser: 'f45050b', bytes: Buffer.byteLength(raw[0].envelope.texto), revisions: parsed.length, result: 'PASS'}));
''', encoding="utf-8")
        # Não herdar credenciais, conexões de banco ou configuração de produção.
        env = {"PATH": os.environ["PATH"], "HOME": directory, "NODE_ENV": "test"}
        subprocess.run([str(root / "node_modules/.bin/tsx"), str(script), str(payload)],
                       env=env, check=True, timeout=60)


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve())
