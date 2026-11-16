"""Compartilhar com agente na biblioteca: confirmação antes do pedido, argv exato e nada de sucesso falso."""
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
with (pathlib.Path(root)/'calls.jsonl').open('a') as out:
    out.write(json.dumps(args)+'\n')
time.sleep(.15)
if scenario == 'refused': print(json.dumps({'status': 'error', 'message': 'Sem permissão.'})); raise SystemExit(1)
if scenario == 'lying': print(json.dumps({'status': 'ok', 'message': 'Compartilhada.'})); raise SystemExit(1)
print(json.dumps({'status': 'pending' if scenario == 'pending' else 'ok', 'message': 'Resposta do agente.'}))
'''

QML = r'''
import QtQuick
import Quickshell
ShellRoot {
  id: harness
  property int stage: 0
  property int ticks: 0
  property int changes: 0
  function require(ok, message) { if (!ok) throw new Error(message) }
  function all(root, name) {
    var seen = [], found = []
    function walk(obj, depth) {
      if (!obj || depth > 14 || seen.indexOf(obj) >= 0) return
      seen.push(obj)
      if (obj.objectName === name) found.push(obj)
      var lists = [obj.data, obj.children, obj.contentItem]
      for (var i = 0; i < lists.length; i++) {
        var list = lists[i]; if (!list) continue
        if (list.length === undefined) list = [list]
        for (var j = 0; j < list.length; j++) walk(list[j], depth+1)
      }
    }
    walk(root, 0); return found
  }
  function control(name, index) { var found = all(share, name); require(found.length > (index || 0), 'missing control ' + name); return found[index || 0] }
  function finish() { console.log('LIBRARY_SHARE_OK'); Qt.exit(0) }
  readonly property var wide: ({id: 'wide', name: 'Wide', error: '', audiences: [{id: 'few', label: 'Poucos'}, {id: 'many', label: 'Muitos'}],
    shared: {'fixture-auto': {audience: 'many', auto: true}, 'fixture-manual': {audience: 'few', auto: false}}})
  readonly property var plain: ({id: 'plain', name: 'Plain', error: '', audiences: [], shared: {}})
  FloatingWindow {
    visible: true; implicitWidth: 850; implicitHeight: 500
    LibraryShare {
      id: share; anchors.fill: parent
      agents: [harness.wide, harness.plain]; meetingSlug: 'fixture-a'; hasNotes: true
      cliCommand: __COMMAND__
      onChanged: harness.changes++
    }
  }
  Timer {
    interval: 30; repeat: true; running: true
    onTriggered: {
      try {
        if (++harness.ticks > 100) throw new Error('share timeout stage=' + harness.stage)
        __STEPS__
      } catch (error) { console.error('LIBRARY_SHARE_FAIL ' + error); Qt.exit(1) }
    }
  }
}
'''


class TestLibraryShareUi(unittest.TestCase):
    def run_share(self, scenario, steps):
        self.assertTrue(shutil.which('quickshell'), 'Quickshell obrigatório para prova do compartilhamento')
        with tempfile.TemporaryDirectory(prefix='castanha-library-share-') as temporary:
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
            self.assertIn('LIBRARY_SHARE_OK', result.stdout)
            for error in ('TypeError', 'ReferenceError', 'Binding loop detected'):
                self.assertNotIn(error, result.stdout)
            path = root/'calls.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_first_click_only_opens_and_cancel_or_meeting_switch_send_nothing(self):
        calls = self.run_share('ok', r'''
        if (harness.stage === 0) {
          harness.require(harness.control('libraryShareLine').text === 'Wide não recebe esta reunião', 'state: ' + harness.control('libraryShareLine').text)
          harness.control('libraryShareButton').clicked()
          harness.require(share.openAgent === 'wide' && share.audience === 'few' && !share.busy, 'first click did not just open, most restricted first')
          harness.control('libraryShareButton').clicked()
          harness.require(share.openAgent === '', 'cancel kept the panel')
          harness.control('libraryShareButton').clicked(); share.meetingSlug = 'fixture-b'; harness.stage = 1
        } else {
          harness.require(share.openAgent === '' && !share.busy && harness.changes === 0, 'meeting switch kept the panel')
          share.hasNotes = false
          harness.require(!harness.control('libraryShareButton').enabled && harness.control('libraryShareLine').text.indexOf('sem notas') > 0, 'meeting without notes can be shared')
          share.agents = []
          harness.require(!share.visible, 'no agent still takes space')
          harness.finish()
        }
        ''')
        self.assertEqual(calls, [])

    def test_confirm_sends_exact_argv_with_chosen_audience_and_reports_agent_message(self):
        calls = self.run_share('pending', r'''
        if (harness.stage === 0) {
          harness.control('libraryShareButton').clicked(); share.audience = 'many'
          harness.control('libraryShareConfirm').clicked()
          harness.require(share.openAgent === '' && harness.control('libraryShareFeedback').text === 'Compartilhando com Wide…', 'confirm did not start'); harness.stage = 1
        } else if (harness.stage === 1 && !share.busy) {
          harness.require(harness.changes === 1 && harness.control('libraryShareFeedback').text === 'Resposta do agente.', 'feedback: ' + harness.control('libraryShareFeedback').text)
          harness.control('libraryShareButton', 1).clicked()
          harness.require(share.openAgent === 'plain' && share.audience === '', 'agent without audiences'); harness.control('libraryShareConfirm', 1).clicked(); harness.stage = 2
        } else if (harness.stage === 2 && !share.busy) harness.finish()
        ''')
        self.assertEqual(calls, [['agents', 'share', '--json', '--audience=many', '--', 'fixture-a', 'wide'],
                                 ['agents', 'share', '--json', '--', 'fixture-a', 'plain']])

    def test_manual_share_can_be_removed_and_rule_share_only_shows(self):
        calls = self.run_share('ok', r'''
        if (harness.stage === 0) {
          share.meetingSlug = 'fixture-auto'
          harness.require(harness.control('libraryShareLine').text === 'Wide recebe esta reunião · Muitos · automático', 'auto: ' + harness.control('libraryShareLine').text)
          harness.require(!harness.control('libraryShareButton').visible, 'rule share offers removal')
          share.meetingSlug = 'fixture-manual'
          harness.require(harness.control('libraryShareLine').text === 'Wide recebe esta reunião · Poucos', 'manual: ' + harness.control('libraryShareLine').text)
          harness.control('libraryShareButton').clicked(); harness.stage = 1
        } else if (!share.busy) harness.finish()
        ''')
        self.assertEqual(calls, [['agents', 'share', '--json', '--remove', '--', 'fixture-manual', 'wide']])

    def test_refusal_and_nonzero_exit_never_claim_success(self):
        for scenario, expected in (('refused', 'Sem permissão.'), ('lying', 'Não foi possível falar com o agente. Nada mudou.')):
            with self.subTest(scenario=scenario):
                self.run_share(scenario, r'''
                if (harness.stage === 0) { harness.control('libraryShareButton').clicked(); harness.control('libraryShareConfirm').clicked(); harness.stage = 1 }
                else if (!share.busy) {
                  harness.require(harness.control('libraryShareFeedback').text === %s, 'feedback: ' + harness.control('libraryShareFeedback').text)
                  harness.finish()
                }
                ''' % json.dumps(expected))


if __name__ == '__main__':
    unittest.main()
