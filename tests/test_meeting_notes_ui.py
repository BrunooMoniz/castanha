"""Editor QML real com CLI sintética, sem áudio, rede ou acervo pessoal."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')

FAKE_CLI = r'''
import json, pathlib, sys, time
base, scenario, *args = sys.argv[1:]
base = pathlib.Path(base)
if args[0] == 'library':
    print(json.dumps({'status': 'ok', 'meetings': [], 'meeting': {'slug': 'a', 'title': 'Fixture', 'summary': '', 'transcript': '', 'warnings': [], 'segments': [], 'decisions': [], 'action_items': [], 'recordings': []}})); sys.exit(0)
_, action, slug, *rest = args
path = base / (slug + '.json')
with (base / 'events.jsonl').open('a') as out:
    out.write(json.dumps({'action': action, 'slug': slug}) + '\n')
if action == 'get':
    time.sleep(.12)
    result = json.loads(path.read_text())
    print(json.dumps(dict(status='ok', **result)))
elif action == 'save':
    text = sys.stdin.read()
    time.sleep(.50 if scenario == 'switch_regeneration' else .20)
    current = json.loads(path.read_text())
    expected = rest[rest.index('--expected-revision') + 1]
    if scenario in ('conflict', 'switch_regeneration_conflict'):
        current = {'text': 'external preserved', 'revision': 'external'}
        path.write_text(json.dumps(current))
    if expected != current['revision']:
        print(json.dumps({'status': 'conflict'})); sys.exit(1)
    current = {'text': text, 'revision': current['revision'] + 'x'}
    path.write_text(json.dumps(current))
    print(json.dumps(dict(status='ok', **current)))
elif action == 'regenerate':
    result = {'status': 'ok', 'message': 'Resumo atualizado.'}
    if scenario == 'local_only':
        result.update(message='Resumo atualizado.', delivery={'status': 'local_only', 'reason': 'Este resumo permanece apenas no computador.'})
    print(json.dumps(result))
    sys.exit(1 if scenario == 'regeneration_failure' else 0)
else:
    raise SystemExit('unexpected synthetic command')
'''

QML = r'''
import QtQuick
import Quickshell
ShellRoot {
  id: harness
  property int stage: 0
  property int ticks: 0
  property int updates: 0
  property var editor: null
  function require(ok, message) { if (!ok) throw new Error(message) }
  function find(object, name, depth) {
    if (!object || depth > 12) return null
    if (object.objectName === name) return object
    var lists = [object.data, object.children, object.contentItem]
    for (var i = 0; i < lists.length; i++) {
      var list = lists[i]; if (!list) continue
      if (list.length === undefined) list = [list]
      for (var j = 0; j < list.length; j++) {
        var found = find(list[j], name, depth + 1); if (found) return found
      }
    }
    return null
  }
  function replaceText(value) { editor.remove(0, editor.length); editor.insert(0, value) }
  FloatingWindow {
    visible: true
    implicitWidth: 750; implicitHeight: 520
    MeetingNotes { id: notes; anchors.fill: parent; meetingSlug: 'a'; cliCommand: __COMMAND__; onSummaryUpdated: harness.updates++ }
  }
  Timer {
    interval: 30; repeat: true; running: true
    onTriggered: {
      try {
        if (++harness.ticks > 250) throw new Error('editor test timeout stage=' + harness.stage)
        if (!harness.editor) harness.editor = harness.find(notes, 'meetingNotesEditor', 0)
        __STEPS__
      } catch (error) { console.error('NOTES_FAIL: ' + error); Qt.exit(1) }
    }
  }
}
'''


class TestMeetingNotesUi(unittest.TestCase):
    def run_editor(self, scenario, steps, *, library=False):
        self.assertTrue(shutil.which('quickshell'), 'quickshell obrigatório para prova do editor')
        with tempfile.TemporaryDirectory(prefix='castanha-notes-ui-') as temporary:
            root = Path(temporary)
            for name in ['MeetingNotes.qml'] + [path.name for path in list(ROOT.glob('Library*')) + [ROOT/'RecordingActions.qml'] if path.is_file()]:
                (root/name).symlink_to(ROOT/name)
            (root/'Commons').symlink_to(SHELL/'Commons', target_is_directory=True)
            (root/'Ui').symlink_to(SHELL/'Ui', target_is_directory=True)
            for slug in ('a', 'b'):
                (root/(slug+'.json')).write_text(json.dumps({'text': 'initial '+slug, 'revision': 'r1'}))
            (root/'fake.py').write_text(FAKE_CLI)
            command = [sys.executable, str(root/'fake.py'), str(root), scenario]
            qml = QML
            if library:
                start = qml.index('  FloatingWindow {')
                end = qml.index('  Timer {', start)
                qml = qml[:start] + '''
  property var notes: null
  LibraryWindow { id: library; cliCommand: __COMMAND__; currentTab: 3 }
  Component.onCompleted: Qt.callLater(function() { harness.notes = harness.find(library, 'libraryMeetingNotes', 0); library.selectMeeting('a'); library.visible = true })
''' + qml[end:]
            (root/'shell.qml').write_text(qml.replace('__COMMAND__', json.dumps(command)).replace('__STEPS__', steps))
            env = dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root/'config'),
                       XDG_STATE_HOME=str(root/'state'), XDG_CACHE_HOME=str(root/'cache'),
                       QT_QPA_PLATFORM='offscreen', QT_QUICK_CONTROLS_STYLE='Basic')
            result = subprocess.run(['quickshell', '--no-duplicate', '--path', str(root/'shell.qml'), '--no-color'],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=12)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('NOTES_PASS', result.stdout)
            self.assertNotIn('ReferenceError', result.stdout)
            self.assertNotIn('TypeError', result.stdout)
            return ({slug: json.loads((root/(slug+'.json')).read_text()) for slug in ('a', 'b')},
                    [json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()])

    def test_autosave_preserves_edits_made_while_previous_save_runs(self):
        data, events = self.run_editor('autosave', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          harness.require(harness.editor.text === 'initial a', 'loaded text not displayed')
          harness.replaceText('first draft'); harness.stage = 1
        } else if (harness.stage === 1 && notes.saving) {
          harness.replaceText('newer draft'); harness.stage = 2
        } else if (harness.stage === 2 && !notes.dirty && !notes.saving) {
          harness.require(notes.text === 'newer draft' && harness.editor.text === notes.text, 'new edit lost by old save')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'newer draft')
        self.assertEqual(sum(event['action'] == 'save' for event in events), 2)

    def test_fast_meeting_switch_routes_pending_load_and_save_to_original_meeting(self):
        data, events = self.run_editor('switch', r'''
        if (harness.stage === 0) { notes.meetingSlug = 'b'; harness.stage = 1 }
        else if (harness.stage === 1 && !notes.loading && harness.editor) {
          harness.require(harness.editor.text === 'initial b', 'late A load replaced B')
          harness.replaceText('draft b'); notes.meetingSlug = 'a'; harness.stage = 2
        } else if (harness.stage === 2 && !notes.loading) {
          harness.require(harness.editor.text === 'initial a', 'B editor content leaked to A')
          harness.replaceText('draft a'); notes.meetingSlug = 'b'; harness.stage = 3
        } else if (harness.stage === 3 && notes.buffers.a && notes.buffers.b
                   && notes.buffers.a.savedText === 'draft a' && notes.buffers.b.savedText === 'draft b') {
          harness.require(notes.text === 'draft b' && harness.editor.text === notes.text, 'return to B lost its draft')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'draft a')
        self.assertEqual(data['b']['text'], 'draft b')
        self.assertEqual(sum(event['action'] == 'save' for event in events), 2)

    def test_revision_conflict_preserves_local_draft_and_external_version_without_retry_loop(self):
        data, events = self.run_editor('conflict', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          harness.replaceText('my unsaved draft'); harness.stage = 1
        } else if (harness.stage === 1 && notes.currentBuffer.error) {
          harness.require(notes.dirty && notes.text === 'my unsaved draft', 'conflict discarded local draft')
          harness.require(notes.currentBuffer.error.indexOf('fora desta janela') >= 0, 'conflict not explained')
          notes.meetingSlug = 'b'; harness.stage = 2
        } else if (harness.stage === 2 && !notes.loading) {
          notes.meetingSlug = 'a'; harness.stage = 3; harness.ticks = 0
        } else if (harness.stage === 3 && harness.ticks > 35) {
          harness.require(notes.dirty && notes.text === 'my unsaved draft' && harness.editor.text === notes.text,
                          'switch lost unsaved conflict draft')
          harness.require(!!notes.currentBuffer.error && !notes.saving, 'conflict retried without explicit user action')
          var reload = harness.find(notes, 'meetingNotesReload', 0)
          harness.require(reload.enabled, 'conflict leaves no way to reload')
          reload.clicked(); harness.stage = 4
        } else if (harness.stage === 4) {
          harness.require(notes.reloadConfirmation && notes.text === 'my unsaved draft', 'reload discarded draft before confirmation')
          var keep = harness.find(notes, 'meetingNotesKeepDraft', 0)
          harness.require(keep.activeFocus, 'safe confirmation choice is not the default')
          var warning = harness.find(notes, 'meetingNotesReloadWarning', 0)
          harness.require(warning.visible && warning.text.indexOf('Copie o texto antes') >= 0, 'destructive reload lacks warning')
          keep.clicked()
          harness.require(!notes.reloadConfirmation && notes.dirty && notes.text === 'my unsaved draft', 'cancel discarded draft')
          harness.find(notes, 'meetingNotesReload', 0).clicked()
          harness.find(notes, 'meetingNotesConfirmReload', 0).clicked(); harness.stage = 5
        } else if (harness.stage === 5 && !notes.loading) {
          harness.require(!notes.dirty && !notes.reloadConfirmation && notes.text === 'external preserved'
                          && harness.editor.text === notes.text, 'explicit reload did not load external version')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'external preserved')
        self.assertEqual(sum(event['action'] == 'save' for event in events), 1)

    def test_closing_library_flushes_notes_before_autosave_delay(self):
        data, events = self.run_editor('close', r'''
        if (harness.stage === 0 && notes && !notes.loading && harness.editor) {
          harness.replaceText('draft saved on close')
          library.visible = false; harness.stage = 1; harness.ticks = 0
        } else if (harness.stage === 1 && notes.saving) {
          harness.require(harness.ticks < 15, 'close did not flush before autosave timer')
          harness.stage = 2
        } else if (harness.stage === 2 && !notes.saving && !notes.dirty) {
          harness.require(notes.text === 'draft saved on close', 'closing discarded draft')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''', library=True)
        self.assertEqual(data['a']['text'], 'draft saved on close')
        self.assertEqual(sum(event['action'] == 'save' for event in events), 1)

    def test_regeneration_saves_first_and_nonzero_exit_cannot_report_success(self):
        data, events = self.run_editor('regeneration_failure', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          harness.replaceText('context before summary'); notes.regenerate(); harness.stage = 1
        } else if (harness.stage === 1 && notes.regenerationMessage.indexOf('Não foi possível') >= 0) {
          harness.require(!notes.dirty && notes.text === 'context before summary', 'regeneration preceded save')
          harness.require(harness.updates === 0, 'failed command emitted summary success')
          harness.require(notes.regenerationMessage.indexOf('Resumo atualizado') < 0, 'failed command displayed success message')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'context before summary')
        self.assertEqual([event['action'] for event in events], ['get', 'save', 'regenerate'])

    def test_local_only_summary_displays_delivery_reason(self):
        data, events = self.run_editor('local_only', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          notes.regenerate(); harness.stage = 1
        } else if (harness.stage === 1 && harness.updates === 1) {
          harness.require(notes.regenerationMessage.indexOf('Resumo atualizado.') >= 0, 'summary message missing')
          harness.require(notes.regenerationMessage.indexOf('apenas no computador') >= 0, 'local-only delivery hidden')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual([event['action'] for event in events], ['get', 'regenerate'])

    def test_queued_regeneration_does_not_change_another_meetings_message(self):
        data, events = self.run_editor('switch_regeneration', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          harness.replaceText('context for meeting a'); notes.regenerate()
          notes.meetingSlug = 'b'; harness.stage = 1
        } else if (harness.stage === 1 && !notes.loading) {
          harness.require(notes.regenerateAfterSave === 'a', 'regeneration was not waiting for A save')
          var button = harness.find(notes, 'meetingNotesRegenerate', 0)
          harness.require(!button.enabled && button.text === 'Atualizando resumo…', 'pending save accepts another summary request')
          notes.regenerate()
          harness.require(notes.regenerateAfterSave === 'a', 'second request overwrote queued meeting A')
          harness.stage = 2
        } else if (harness.stage === 2 && harness.updates === 1) {
          harness.require(notes.meetingSlug === 'b' && notes.text === 'initial b', 'regeneration changed selected meeting')
          harness.require(notes.regenerationMessage === '', 'meeting A progress leaked into meeting B')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'context for meeting a')
        self.assertEqual([event['slug'] for event in events if event['action'] == 'regenerate'], ['a'])

    def test_queued_save_failure_does_not_change_another_meetings_message(self):
        data, events = self.run_editor('switch_regeneration_conflict', r'''
        if (harness.stage === 0 && !notes.loading && harness.editor) {
          harness.replaceText('conflicting context for a'); notes.regenerate()
          notes.meetingSlug = 'b'; harness.stage = 1
        } else if (harness.stage === 1 && !notes.loading && notes.buffers.a.error) {
          harness.require(notes.meetingSlug === 'b' && notes.text === 'initial b', 'failed save changed selected meeting')
          harness.require(notes.regenerationMessage === '', 'meeting A save error leaked into meeting B')
          harness.require(notes.regenerateAfterSave === '' && harness.updates === 0, 'failed save kept summary request')
          harness.require(notes.buffers.a.text === 'conflicting context for a', 'failed save lost draft A')
          console.log('NOTES_PASS'); Qt.quit()
        }
        ''')
        self.assertEqual(data['a']['text'], 'external preserved')
        self.assertFalse(any(event['action'] == 'regenerate' for event in events))


if __name__ == '__main__':
    unittest.main()
