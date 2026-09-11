"""Biblioteca real e janela Quickshell com fontes sintéticas, sem provedores."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from castanha.library import LibraryError, MeetingLibrary
from castanha.storage import MeetingStorage

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='castanha-library-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root/'config'),
                                     'XDG_STATE_HOME': str(self.root/'state'), 'XDG_CACHE_HOME': str(self.root/'cache')})
        env.start(); self.addCleanup(env.stop)
        self.storage = MeetingStorage(self.root/'meetings')
        self.library = MeetingLibrary(self.storage)
        cfg = self.root/'config/castanha/config.json'
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({'storage': {'base_dir': str(self.storage.base_dir),
            'bronze_dir': str(self.storage.bronze_dir), 'silver_dir': str(self.storage.silver_dir),
            'gold_dir': str(self.storage.gold_dir)}}))

    def meeting(self, slug='2026-09-11_1000_fixture', *, pending=False):
        bronze = self.storage.bronze_dir/slug
        bronze.mkdir()
        audio = bronze/'original.wav'
        import wave
        with wave.open(str(audio), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 48000)
        digest = hashlib.sha256(audio.read_bytes()).hexdigest()
        meta = {'title': 'Reunião sintética de produto', 'recorded_at': '2026-09-11T10:00:00-03:00',
                'duration_seconds': 3, 'processing_status': 'pending' if pending else 'complete',
                'zinom': {'status': 'pending' if pending else 'ok', 'secret_fixture': 'not-in-inventory'},
                'recordings': [{'filename': audio.name, 'job_id': 'fixture-job', 'sha256': digest, 'duration_seconds': 3}],
                'calendar_event': {'attendees': [{'name': 'Pessoa da agenda não é identificação de voz'}]}}
        (bronze/'metadata.json').write_text(json.dumps(meta))
        (self.storage.silver_dir/(slug+'.md')).write_text('---\ntitle: "private-frontmatter"\n---\n# Produto\n\n## Resumo Executivo\nResumo completo sintético.\n\n## 📝 Transcrição Bruta\nNão duplicar transcrição aqui.')
        (self.storage.gold_dir/(slug+'.json')).write_text(json.dumps({'decisions': [{'decision': 'Publicar o protótipo.'}],
            'action_items': [{'task': 'Preparar demonstração', 'assignee': None, 'deadline': None}]}))
        (bronze/'transcript_raw.txt').write_text('A proposta foi aprovada. <img src="https://fixture.invalid/x">')
        timeline = {'version': 1, 'recordings': [{'job_id': 'fixture-job', 'source_sha256': digest,
            'time_reference': 'recording_start', 'channel_provenance': True,
            'utterances': [{'start': 0.2, 'end': 1.5, 'channel': 1, 'speaker': 'Nome inventado', 'text': 'A proposta foi aprovada.'}]}]}
        (bronze/'transcript_segments.json').write_text(json.dumps(timeline))
        return slug, bronze

    def test_all_history_excludes_private_contents_and_keeps_pending_filter_metadata(self):
        for index in range(15):
            self.meeting(f'2026-09-11_{index:04d}_fixture', pending=index == 0)
        result = self.library.list()
        self.assertEqual(len(result['meetings']), 15)
        self.assertEqual(sum(item['status'] == 'pending' for item in result['meetings']), 1)
        encoded = json.dumps(result)
        for excluded in ('Resumo completo', 'A proposta', 'file://', 'secret_fixture', 'calendar_event', 'sha256'):
            self.assertNotIn(excluded, encoded)

    def test_delivery_unknown_or_pending_facts_never_claims_complete(self):
        slug, bronze = self.meeting()
        path = bronze/'metadata.json'
        meta = json.loads(path.read_text())
        for delivery in ({}, {'status': 'unexpected'}, {'status': 'ok', 'facts_status': 'pending_lineage'}):
            meta['zinom'] = delivery
            path.write_text(json.dumps(meta))
            entry = self.library.list()['meetings'][0]
            self.assertEqual(entry['status'], 'pending')
            self.assertNotEqual(entry['status_label'], 'Concluída')

    def test_detail_reads_full_text_and_only_proven_channel_times(self):
        slug, bronze = self.meeting()
        result = self.library.detail(slug)['meeting']
        self.assertIn('Resumo completo sintético.', result['summary'])
        self.assertNotIn('private-frontmatter', result['summary'])
        self.assertNotIn('Não duplicar', result['summary'])
        self.assertEqual(result['decisions'], ['Publicar o protótipo.'])
        self.assertEqual(result['action_items'], ['Preparar demonstração'])
        self.assertEqual(result['segments'][0]['channel'], 'Áudio remoto')
        self.assertTrue(result['segments'][0]['can_seek'])
        self.assertEqual(result['segments'][0]['audio_index'], 0)
        self.assertIn('<img src=', result['transcript'], 'conteúdo é texto e UI deve tratá-lo como PlainText')
        self.assertTrue(result['recordings'][0]['url'].startswith('file:///'))
        self.assertNotIn('Nome inventado', json.dumps(result))

    def test_changed_audio_keeps_transcript_but_refuses_seek(self):
        slug, bronze = self.meeting()
        (bronze/'original.wav').write_bytes(b'changed')
        result = self.library.detail(slug)['meeting']
        self.assertFalse(result['segments'][0]['can_seek'])
        self.assertIn('A proposta', result['transcript'])

    def test_invalid_times_and_unproven_voice_are_not_invented(self):
        slug, bronze = self.meeting()
        file = bronze/'transcript_segments.json'
        timeline = json.loads(file.read_text())
        timeline['recordings'][0].update(channel_provenance=False, time_reference='unknown')
        timeline['recordings'][0]['utterances'][0].update(start=-1, speaker='Bruno')
        file.write_text(json.dumps(timeline))
        segment = self.library.detail(slug)['meeting']['segments'][0]
        self.assertEqual(segment['channel'], 'Origem não identificada')
        self.assertIsNone(segment['start'])
        self.assertFalse(segment['can_seek'])

    def test_unsafe_slugs_and_symlinks_never_read_outside_storage(self):
        slug, bronze = self.meeting()
        for invalid in ('../outside', '/etc/passwd', 'a/b', 'a\\b', '.', '..', 'x\x00y'):
            with self.subTest(slug=invalid), self.assertRaises(LibraryError):
                self.library.detail(invalid)
        outside = self.root/'outside.txt'; outside.write_text('EXTERNAL-SENTINEL')
        silver = self.storage.silver_dir/(slug+'.md'); silver.unlink(); silver.symlink_to(outside)
        (bronze/'external.ogg').symlink_to(outside)
        result = self.library.detail(slug)
        self.assertNotIn('EXTERNAL-SENTINEL', json.dumps(result))
        self.assertNotIn('external.ogg', json.dumps(result))
        self.assertTrue(result['meeting']['warnings'])
        (self.storage.bronze_dir/'linked').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(LibraryError):
            self.library.detail('linked')

    def test_metadata_paths_are_not_used_for_reading(self):
        slug, bronze = self.meeting()
        file = bronze/'metadata.json'; meta = json.loads(file.read_text())
        meta['recordings'][0].update(path='/etc/passwd', filename='../passwd')
        file.write_text(json.dumps(meta))
        result = self.library.detail(slug)['meeting']
        self.assertEqual(len(result['recordings']), 1)
        self.assertFalse(result['segments'][0]['can_seek'])
        self.assertNotIn('/etc/passwd', json.dumps(result))

    def test_corrupt_metadata_retains_entry_with_explicit_warning(self):
        slug, bronze = self.meeting()
        (bronze/'metadata.json').write_text('not json')
        result = self.library.list()['meetings']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['slug'], slug)
        self.assertTrue(result[0]['warnings'])
        self.assertEqual(result[0]['status'], 'pending')
        with patch('castanha.library.os.scandir', side_effect=PermissionError):
            with self.assertRaises(LibraryError): self.library.list()

    def test_cli_uses_controlled_detail_and_nonzero_error(self):
        slug, _ = self.meeting()
        okay = subprocess.run([str(ROOT/'bin/castanha'), 'library', '--json', slug], capture_output=True, text=True)
        self.assertEqual(okay.returncode, 0, okay.stderr)
        self.assertEqual(json.loads(okay.stdout)['meeting']['slug'], slug)
        bad = subprocess.run([str(ROOT/'bin/castanha'), 'library', '--json', '../outside'], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout)['status'], 'error')

    def test_real_quickshell_library_loads_local_cli_and_audio(self):
        self.assertTrue(os.environ.get('WAYLAND_DISPLAY'), 'verificação real exige sessão Wayland')
        self.assertTrue(shutil.which('quickshell'), 'quickshell precisa estar instalado')
        slug, _ = self.meeting()
        config = self.root/'shell'; config.mkdir()
        for name in ('Commons', 'Ui'):
            (config/name).symlink_to(SHELL/name, target_is_directory=True)
        for file in ROOT.glob('Library*'):
            (config/file.name).symlink_to(file)
        copy_path = self.root/'copied.txt'
        shell = '''import QtQuick
import Quickshell
ShellRoot {
  LibraryWindow { id: library; cliCommand: CLI; clipboardCommand: COPY }
  property int attempts: 0
  property var media: null
  function findObject(owner, name, depth) {
    if (!owner || depth > 15) return null
    if (owner.objectName === name) return owner
    var items = owner.data || owner.children || []
    for (var i = 0; i < items.length; i++) { var found = findObject(items[i], name, depth + 1); if (found) return found }
    return null
  }
  Timer { interval: 100; running: true; repeat: true; onTriggered: {
    attempts++
    if (attempts === 1) { if (library.visible) { console.log("LIBRARY_FAIL initially visible"); Qt.exit(1) }; library.showMeeting(SLUG) }
    if (library.current && library.meetings.length === 1) {
      if (!library.current.segments[0].can_seek) { console.log("LIBRARY_FAIL unlinked segment"); Qt.exit(1) }
      library.loadAudio(0, 0, false)
      audioCheck.start(); stop()
    }
    if (attempts > 80) { console.log("LIBRARY_FAIL timeout " + library.detailError); Qt.exit(1) }
  } }
  Timer { id: audioCheck; interval: 800; onTriggered: {
    library.currentTab = 1
    var full = findObject(library, "libraryFullTranscript", 0)
    if (!full) { console.log("LIBRARY_FAIL full transcript control missing"); Qt.exit(1) }
    full.clicked()
    var raw = findObject(library, "libraryRawTranscript", 0)
    if (!library.showRawTranscript || !raw.visible || raw.text !== library.current.transcript) { console.log("LIBRARY_FAIL full transcript unavailable"); Qt.exit(1) }
    library.currentTab = 2
    if (library.audioIndex !== 0 || library.audioError) { console.log("LIBRARY_FAIL audio " + library.audioError); Qt.exit(1) }
    media = findObject(library, "libraryMediaPlayer", 0)
    if (!media || !media.seekable || media.duration < 2900) { console.log("LIBRARY_FAIL audio not loaded"); Qt.exit(1) }
    var speed = findObject(library, "librarySpeed", 0)
    if (!speed) { console.log("LIBRARY_FAIL speed control missing"); Qt.exit(1) }
    speed.currentIndex = 3; speed.activated(3)
    if (media.playbackRate !== 1.5) { console.log("LIBRARY_FAIL speed control"); Qt.exit(1) }
    library.seekSegment(library.current.segments[0])
    library.copyText(library.current.transcript)
    library.width = 800; library.height = 600
    finish.start()
  } }
  Timer { id: finish; interval: 500; onTriggered: {
    if (!media.playing || media.position < 250) { console.log("LIBRARY_FAIL seek playback"); Qt.exit(1) }
    if (library.copyFeedback !== "Texto copiado") { console.log("LIBRARY_FAIL clipboard"); Qt.exit(1) }
    library.cliCommand = ["/usr/bin/false"]; library.refreshHistory(); failedRead.start()
  } }
  Timer { id: failedRead; interval: 400; onTriggered: {
    if (library.meetings.length !== 1 || !library.historyError) { console.log("LIBRARY_FAIL history discarded"); Qt.exit(1) }
    library.visible = false
    if (media.playing) { console.log("LIBRARY_FAIL audio continued after close"); Qt.exit(1) }
    console.log("CASTANHA_LIBRARY_OK"); Qt.exit(0)
  } }
}'''.replace('CLI', json.dumps([str(ROOT/'bin/castanha')])).replace('SLUG', json.dumps(slug)).replace('COPY', json.dumps([
            'python3', '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())', str(copy_path)]))
        (config/'shell.qml').write_text(shell)
        result = subprocess.run(['quickshell', '--no-duplicate', '--path', str(config/'shell.qml'), '--no-color'],
                                capture_output=True, text=True, timeout=20, env={**os.environ, 'QT_QPA_PLATFORM': 'wayland'})
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('CASTANHA_LIBRARY_OK', result.stdout+result.stderr)
        self.assertNotIn('LIBRARY_FAIL', result.stdout+result.stderr)
        self.assertEqual(copy_path.read_text(), 'A proposta foi aprovada. <img src="https://fixture.invalid/x">')


if __name__ == '__main__':
    unittest.main()
