"""Compartilhar notas com um agente: só o programa da config é chamado, com argv exato e sem shell."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

from castanha import agents
from castanha.library import LibraryError

ROOT = Path(__file__).resolve().parents[1]

FAKE = r'''#!/usr/bin/env python3
import json, pathlib, sys
here = pathlib.Path(__file__).parent
with open(here / 'calls', 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\n')
mode = (here / 'mode').read_text().strip() if (here / 'mode').exists() else 'ok'
if mode == 'garbage': print('not json'); raise SystemExit(0)
if sys.argv[1] == 'status':
    print(json.dumps({'audiences': [{'id': 'small', 'label': 'Grupo pequeno'}, {'id': 'wide'}, 'lixo'],
                      'shared': {'a-meeting.md': {'audience': 'small', 'auto': True},
                                 'b-meeting.md': {'audience': 'wide'}, 'sem-extensao': {}, 'c.md': 'lixo'}}))
elif mode == 'pending': print(json.dumps({'status': 'pending', 'message': 'Marcada; sai na próxima rodada.'}))
elif mode == 'refuse': print(json.dumps({'status': 'error', 'message': 'Sem permissão.'})); raise SystemExit(1)
elif mode == 'lying': print(json.dumps({'status': 'ok'})); raise SystemExit(3)
else: print(json.dumps({'status': 'ok'}))
'''


class TestAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='castanha-agents-')
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'agent.py'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        silver = self.root / 'silver'
        silver.mkdir()
        (silver / 'a-meeting.md').write_text('# notas')
        self.storage = types.SimpleNamespace(silver_dir=silver)
        self.config = {'agents': [{'id': 'helper', 'name': 'Helper', 'command': [sys.executable, str(self.fake)]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def calls(self):
        path = self.root / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_malformed_agents_are_ignored_and_no_agent_means_empty_status(self):
        config = {'agents': ['x', {'id': '', 'command': ['a']}, {'id': 'a', 'command': 'rm -rf'},
                             {'id': 'a', 'command': ['ok', '']}, {'id': 'b', 'command': ['prog']},
                             {'id': 'b', 'command': ['outro']}]}
        self.assertEqual(agents.configured(config), [{'id': 'b', 'name': 'b', 'command': ['prog']}])
        self.assertEqual(agents.status({}), {'status': 'ok', 'agents': []})
        self.assertEqual(agents.status({'agents': None}), {'status': 'ok', 'agents': []})

    def test_status_maps_note_files_to_slugs_and_keeps_only_well_formed_entries(self):
        agent = agents.status(self.config)['agents'][0]
        self.assertEqual(agent['audiences'], [{'id': 'small', 'label': 'Grupo pequeno'}, {'id': 'wide', 'label': 'wide'}])
        self.assertEqual(agent['shared'], {'a-meeting': {'audience': 'small', 'auto': True},
                                           'b-meeting': {'audience': 'wide', 'auto': False}})
        self.assertEqual((agent['error'], self.calls()), ('', [['status']]))

    def test_broken_agent_reports_its_error_without_hiding_the_others(self):
        (self.root / 'mode').write_text('garbage')
        config = {'agents': self.config['agents'] + [{'id': 'gone', 'command': [str(self.root / 'missing')]}]}
        first, second = agents.status(config)['agents']
        self.assertIn('não entende', first['error'])
        self.assertIn('Não foi possível rodar', second['error'])
        self.assertEqual((first['shared'], second['shared']), ({}, {}))

    def test_share_and_unshare_send_the_exact_note_file(self):
        result = agents.share('a-meeting', 'helper', 'small', config=self.config, storage=self.storage)
        self.assertEqual((result['status'], result['shared'], result['message']),
                         ('ok', True, 'Reunião compartilhada com Helper.'))
        agents.share('a-meeting', 'helper', config=self.config, storage=self.storage)
        gone = agents.share('never-had-notes', 'helper', remove=True, config=self.config, storage=self.storage)
        self.assertEqual((gone['shared'], gone['message']), (False, 'Helper deixou de receber esta reunião.'))
        self.assertEqual(self.calls(), [['share', 'a-meeting.md', 'small'], ['share', 'a-meeting.md'],
                                        ['unshare', 'never-had-notes.md']])

    def test_share_refuses_missing_notes_bad_ids_and_unknown_agents_before_running_anything(self):
        for slug, agent, audience in (('no-notes-yet', 'helper', None), ('../a-meeting', 'helper', None),
                                      ('a-meeting', 'stranger', None), ('a-meeting', 'helper', '../x')):
            with self.assertRaises(LibraryError):
                agents.share(slug, agent, audience, config=self.config, storage=self.storage)
        self.assertEqual(self.calls(), [])

    def test_pending_passes_through_and_failures_never_report_success(self):
        (self.root / 'mode').write_text('pending')
        pending = agents.share('a-meeting', 'helper', config=self.config, storage=self.storage)
        self.assertEqual((pending['status'], pending['message']), ('pending', 'Marcada; sai na próxima rodada.'))
        for mode, text in (('refuse', 'Sem permissão.'), ('lying', 'recusou'), ('garbage', 'não entende')):
            (self.root / 'mode').write_text(mode)
            with self.assertRaises(LibraryError) as caught:
                agents.share('a-meeting', 'helper', config=self.config, storage=self.storage)
            self.assertIn(text, str(caught.exception))

    def test_cli_reads_agents_from_the_user_config(self):
        home = self.root / 'config' / 'castanha'
        home.mkdir(parents=True)
        (home / 'config.json').write_text(json.dumps(self.config))
        (home / 'config.json').chmod(0o600)
        env = {**os.environ, 'XDG_CONFIG_HOME': str(self.root / 'config'), 'XDG_STATE_HOME': str(self.root / 'state')}
        ok = subprocess.run([str(ROOT / 'bin/castanha'), 'agents', 'status', '--json'], capture_output=True, text=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)['agents'][0]['shared']['a-meeting']['auto'], True)
        bad = subprocess.run([str(ROOT / 'bin/castanha'), 'agents', 'share', '--json', '--', 'a-meeting', 'stranger'],
                             capture_output=True, text=True, env=env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout), {'status': 'error', 'message': 'Agente não configurado.'})


if __name__ == '__main__':
    unittest.main()
