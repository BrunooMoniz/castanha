"""Ações nativas de gravação com CLI isolada, sem áudio nem reuniões reais."""
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
root, scenario, *args = sys.argv[1:]
root = pathlib.Path(root)
with (root/'calls.jsonl').open('a') as out:
    out.write(json.dumps(args)+'\n')
time.sleep(.20)
if args[0] == 'retry':
    data = {'summary_status': 'pending'}
    if scenario == 'retry_transcription': data = {'transcription_pending': True}
    if scenario == 'retry_zinom': data = {'zinom': {'status': 'pending_cleanup'}}
    if scenario == 'retry_problems': data = {'problemas': ['synthetic pending stage']}
    if scenario == 'retry_outer_pending': data = {}
    output = {'status': 'ok', 'results': [{'status': 'pending' if scenario == 'retry_outer_pending' else 'success', 'result': data}]}
elif args[0] == 'recordings' and len(args) > 1 and args[1] == 'move':
    output = {'status': 'ok', 'destination': {'slug': 'fixture-b', 'title': 'B', 'job_id': 'novo', 'created': False},
              'remaining_count': 1, 'cleanup_status': 'pending' if scenario == 'move_cleanup' else 'not_needed',
              'exclusion_id': 'moved-current', 'can_restore': False,
              'message': 'Gravação movida para “B”. Transcrição, resumo e entrega ao Zinom das duas reuniões serão refeitos automaticamente.'}
else:
    output = {'status': 'ok', 'remaining_count': 1, 'exclusion_id': 'excluded-current', 'can_restore': scenario == 'restore_allowed'}
if scenario == 'false_success':
    output = {'status': 'ok', 'message': 'Áudio excluído com sucesso.'}
print(json.dumps(output))
raise SystemExit(1 if scenario == 'false_success' else 0)
'''

QML = r'''
import QtQuick
import Quickshell
ShellRoot {
  id: harness
  property int stage: 0
  property int ticks: 0
  property int beforeCount: 0
  property bool playerRunning: true
  property bool wasBusyBefore: false
  property var changed: []
  function require(ok, message) { if (!ok) throw new Error(message) }
  function find(root, name) {
    var seen = []
    function walk(obj, depth) {
      if (!obj || depth > 14 || seen.indexOf(obj) >= 0) return null
      seen.push(obj)
      if (obj.objectName === name) return obj
      var lists = [obj.data, obj.children, obj.contentItem]
      for (var i = 0; i < lists.length; i++) {
        var list = lists[i]; if (!list) continue
        if (list.length === undefined) list = [list]
        for (var j = 0; j < list.length; j++) { var found = walk(list[j], depth+1); if (found) return found }
      }
      return null
    }
    var found = walk(root, 0); if (!found) throw new Error('missing control ' + name); return found
  }
  function control(name) { return find(actions, name) }
  function finish() { console.log('RECORDING_ACTIONS_OK'); Qt.exit(0) }
  FloatingWindow {
    visible: true; implicitWidth: 850; implicitHeight: 500
    RecordingActions {
      id: actions; anchors.fill: parent
      meetingSlug: 'fixture-a'; revision: 17
      recordings: [{id: 'first.ogg', duration_seconds: 8, size_bytes: 1000}, {id: 'second.ogg', duration_seconds: 20, size_bytes: 2000}]
      cliCommand: __COMMAND__
      onBeforeMutation: { harness.beforeCount++; harness.wasBusyBefore = busy; harness.playerRunning = false }
      onMeetingChanged: function(slug) { harness.changed = harness.changed.concat([slug]) }
    }
  }
  Timer {
    interval: 30; repeat: true; running: true
    onTriggered: {
      try {
        if (++harness.ticks > 100) throw new Error('recordings timeout stage=' + harness.stage)
        __STEPS__
      } catch (error) { console.error('RECORDING_ACTIONS_FAIL ' + error); Qt.exit(1) }
    }
  }
}
'''


class TestRecordingActionsUi(unittest.TestCase):
    def run_actions(self, scenario, steps):
        self.assertTrue(shutil.which('quickshell'), 'Quickshell obrigatório para prova das ações')
        with tempfile.TemporaryDirectory(prefix='castanha-recording-actions-') as temporary:
            root = Path(temporary)
            for pattern in ('*.qml', '*.js'):
                for source in ROOT.glob(pattern):
                    (root/source.name).symlink_to(source)
            for name in ('Commons', 'Ui'):
                (root/name).symlink_to(SHELL/name, target_is_directory=True)
            (root/'fake.py').write_text(FAKE_CLI)
            command = [sys.executable, str(root/'fake.py'), str(root), scenario]
            (root/'shell.qml').write_text(QML.replace('__COMMAND__', json.dumps(command)).replace('__STEPS__', steps))
            env = dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root/'config'),
                       XDG_STATE_HOME=str(root/'state'), XDG_CACHE_HOME=str(root/'cache'),
                       QT_QPA_PLATFORM='offscreen', QT_QUICK_CONTROLS_STYLE='Basic')
            result = subprocess.run(['quickshell', '--no-duplicate', '--path', str(root/'shell.qml'), '--no-color'],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=7)
            self.assertEqual(result.returncode, 0, result.stdout[-14000:])
            self.assertIn('RECORDING_ACTIONS_OK', result.stdout)
            for error in ('TypeError', 'ReferenceError', 'Binding loop detected'):
                self.assertNotIn(error, result.stdout)
            path = root/'calls.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_exclude_only_arms_cancel_and_meeting_switch_send_no_command(self):
        calls = self.run_actions('no_action', r'''
        if (harness.stage === 0) {
          harness.control('recordingExclude0').clicked()
          harness.require(actions.armedFile === 'first.ogg' && !actions.busy && harness.beforeCount === 0, 'first click performed deletion')
          harness.require(harness.control('recordingConfirmExclude').visible, 'confirmation not visible')
          harness.control('recordingCancelExclude').clicked()
          harness.require(actions.armedFile === '', 'cancel kept destructive confirmation')
          harness.control('recordingExclude0').clicked(); actions.meetingSlug = 'fixture-b'; harness.stage = 1
        } else {
          harness.require(actions.armedFile === '' && !actions.busy && harness.beforeCount === 0, 'meeting switch kept armed file')
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [])

    def test_confirmation_sends_exact_file_slug_revision_after_before_mutation(self):
        calls = self.run_actions('exclude', r'''
        if (harness.stage === 0) {
          harness.control('recordingExclude1').clicked(); harness.control('recordingConfirmExclude').clicked()
          harness.require(harness.beforeCount === 1 && !harness.wasBusyBefore && !harness.playerRunning,
                          'beforeMutation did not stop caller playback before starting action')
          harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(actions.armedFile === '' && actions.feedback.indexOf('Áudio excluído') === 0, 'confirmed deletion lacks result')
          harness.require(harness.changed[0] === 'fixture-a', 'wrong meeting change signal')
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [['recordings', 'exclude', '--json', '--expected-revision', '17', '--', 'fixture-a', 'second.ogg']])

    def test_late_deletion_response_does_not_leak_into_other_meeting(self):
        calls = self.run_actions('exclude', r'''
        if (harness.stage === 0) {
          harness.control('recordingExclude0').clicked(); harness.control('recordingConfirmExclude').clicked()
          actions.meetingSlug = 'fixture-b'; harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(actions.feedback === '' && !actions.armedFile, 'A response leaked into B')
          harness.require(actions.messages['fixture-a'].indexOf('Áudio excluído') === 0 && harness.changed[0] === 'fixture-a', 'response lost original identity')
          harness.finish()
        }
        ''')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ['fixture-a', 'first.ogg'])

    def test_remote_exclusion_cannot_offer_stale_undo(self):
        self.run_actions('exclude', r'''
        if (harness.stage === 0) {
          actions.restoreAllowed = true; actions.lastExclusionId = 'older-exclusion'
          harness.control('recordingExclude0').clicked(); harness.control('recordingConfirmExclude').clicked(); harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(!harness.control('recordingUndo').visible, 'can_restore false still offers stale undo')
          harness.finish()
        }
        ''')

    def test_metadata_revokes_undo_and_zero_recordings_disable_reprocess(self):
        calls = self.run_actions('no_action', r'''
        if (harness.stage === 0) {
          actions.lastExclusionId = 'known-exclusion'; actions.restoreAllowed = true; harness.stage = 1
        } else if (harness.stage === 1) {
          harness.require(harness.control('recordingUndo').visible, 'valid metadata undo not offered')
          actions.restoreAllowed = false; actions.recordings = []; harness.stage = 2
        } else {
          harness.require(!harness.control('recordingUndo').visible, 'revoked undo remains visible')
          harness.require(!harness.control('recordingReprocess').enabled, 'zero recordings can reprocess')
          actions.restore(); actions.retry(); harness.finish()
        }
        ''')
        self.assertEqual(calls, [])

    def test_nonzero_exit_with_success_json_never_claims_success(self):
        calls = self.run_actions('false_success', r'''
        if (harness.stage === 0) {
          harness.control('recordingExclude0').clicked(); harness.control('recordingConfirmExclude').clicked(); harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(actions.feedback.indexOf('Não foi possível') === 0 && actions.feedback.indexOf('sucesso') < 0,
                          'failed process claimed deletion success')
          harness.require(!harness.control('recordingUndo').visible, 'failed process offers undo')
          harness.finish()
        }
        ''')
        self.assertEqual(len(calls), 1)

    def test_move_needs_a_destination_and_sends_exact_argv(self):
        calls = self.run_actions('move_cleanup', r'''
        if (harness.stage === 0) {
          actions.moveTargets = [{slug: 'fixture-b', title: 'B', when: '2026-09-11T10:00:00'}]
          harness.control('recordingMove0').clicked()
          harness.require(actions.movingFile === 'first.ogg' && harness.control('recordingConfirmMove').visible, 'move panel not armed')
          harness.require(!harness.control('recordingConfirmMove').enabled, 'move allowed without destination')
          harness.control('recordingMoveTarget').changed('fixture-b')
          harness.require(actions.moveTarget === 'fixture-b' && harness.control('recordingConfirmMove').enabled, 'destination not taken')
          harness.control('recordingCancelMove').clicked()
          harness.require(actions.movingFile === '' && actions.moveTarget === '' && harness.beforeCount === 0, 'cancel kept move armed')
          harness.control('recordingMove0').clicked(); harness.control('recordingMoveTarget').changed('fixture-b')
          harness.control('recordingConfirmMove').clicked()
          harness.require(harness.beforeCount === 1 && actions.movingFile === '', 'confirm did not start the move')
          harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(actions.feedback.indexOf('Gravação movida') === 0 && actions.feedback.indexOf('Zinom ainda está pendente') > 0, 'move feedback lost: ' + actions.feedback)
          harness.require(harness.changed[0] === 'fixture-a', 'wrong meeting change signal')
          harness.require(!harness.control('recordingUndo').visible, 'moved recording offers undo')
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [['recordings', 'move', '--json', '--expected-revision', '17', '--to=fixture-b', '--', 'fixture-a', 'first.ogg']])

    def test_move_to_new_meeting_requires_title_and_passes_it_glued_to_the_flag(self):
        calls = self.run_actions('move', r'''
        if (harness.stage === 0) {
          harness.control('recordingMove1').clicked()
          harness.control('recordingMoveTarget').changed('__new__')
          harness.require(harness.control('recordingMoveTitle').visible && !harness.control('recordingConfirmMove').enabled, 'new meeting allowed without title')
          harness.control('recordingMoveTitle').text = '  -Conversa avulsa  '
          harness.require(harness.control('recordingConfirmMove').enabled, 'title did not enable move')
          harness.control('recordingConfirmMove').clicked(); harness.stage = 1
        } else if (!actions.busy && harness.changed.length) {
          harness.require(actions.feedback.indexOf('Gravação movida') === 0 && actions.feedback.indexOf('pendente') < 0, 'unexpected feedback: ' + actions.feedback)
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [['recordings', 'move', '--json', '--expected-revision', '17', '--new-title=-Conversa avulsa', '--', 'fixture-a', 'second.ogg']])

    def test_move_button_hidden_when_move_is_disabled(self):
        calls = self.run_actions('no_action', r'''
        if (harness.stage === 0) {
          actions.moveEnabled = false
          harness.require(!harness.control('recordingMove0').visible, 'move button shown while disabled')
          harness.require(harness.control('recordingExclude0').visible, 'exclude button hidden too')
          actions.moveEnabled = true
          harness.require(harness.control('recordingMove0').visible, 'move button not restored')
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [])

    def test_retry_results_array_with_pending_summary_never_claims_complete(self):
        for scenario in ('retry', 'retry_transcription', 'retry_zinom', 'retry_problems', 'retry_outer_pending'):
            with self.subTest(scenario=scenario):
                calls = self.run_actions(scenario, r'''
                if (harness.stage === 0) { harness.control('recordingReprocess').clicked(); harness.stage = 1 }
                else if (!actions.busy && harness.changed.length) {
                  harness.require(actions.feedback.indexOf('etapas pendentes') >= 0 && actions.feedback.indexOf('Reunião reprocessada') < 0,
                                  'pending retry falsely marked complete')
                  harness.finish()
                }
                ''')
                self.assertEqual(calls, [['retry', '--json', '--', 'fixture-a']])

    def test_allowed_undo_uses_exact_exclusion_receipt(self):
        calls = self.run_actions('restore_allowed', r'''
        if (harness.stage === 0) {
          actions.restoreAllowed = true
          harness.control('recordingExclude0').clicked(); harness.control('recordingConfirmExclude').clicked(); harness.stage = 1
        } else if (harness.stage === 1 && !actions.busy && harness.changed.length === 1) {
          harness.require(harness.control('recordingUndo').visible, 'allowed receipt offers no undo')
          harness.control('recordingUndo').clicked(); harness.stage = 2
        } else if (harness.stage === 2 && !actions.busy && harness.changed.length === 2) {
          harness.require(!harness.control('recordingUndo').visible, 'successful restore keeps stale undo')
          harness.finish()
        }
        ''')
        self.assertEqual(calls[-1], ['recordings', 'restore', '--json', '--', 'fixture-a', 'excluded-current'])


if __name__ == '__main__':
    unittest.main()
