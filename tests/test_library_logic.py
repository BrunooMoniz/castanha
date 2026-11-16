"""Lógica da biblioteca (LibraryLogic.js) no motor QML real: falas, tempo de fala e histórico por dia."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

QML = r'''
import QtQuick
import Quickshell
import "LibraryLogic.js" as L
ShellRoot {
  function eq(actual, expected, what) {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(what + ": " + JSON.stringify(actual))
  }
  function seg(channel, start, end, text, audio) {
    return {channel: channel, start: start, end: end, text: text, audio_index: audio === undefined ? 0 : audio, can_seek: start !== null}
  }
  Timer { interval: 50; running: true; onTriggered: {
    try {
      var s = [seg("Ana", 0, 2, "bom dia"), seg("Ana", 2.5, 4, "tudo bem"), seg("Bia", 4, 6, "tudo"), seg("Ana", 6, 7, "vamos"),
               seg("Ana", 20, 21, "depois da pausa"), seg("Ana", 21, 22, "outra gravação", 1), seg("Ana", null, null, "sem tempo")]
      var t = L.turns(s, "")
      eq(t.map(function(x) { return [x.channel, x.start, x.end, x.text, x.audio_index, x.can_seek] }),
         [["Ana", 0, 4, "bom dia tudo bem", 0, true], ["Bia", 4, 6, "tudo", 0, true], ["Ana", 6, 7, "vamos", 0, true],
          ["Ana", 20, 21, "depois da pausa", 0, true], ["Ana", 21, 22, "outra gravação", 1, true], ["Ana", null, null, "sem tempo", 0, false]], "turns")
      eq(s[0].text, "bom dia", "turns mutated its input")
      eq(L.turns(s, " TUDO ").map(function(x) { return x.text }), ["bom dia tudo bem", "tudo"], "search in text")
      eq(L.turns(s, "bia").length, 1, "search by speaker")
      eq(L.turns(null, "x"), [], "no segments")
      var long = []
      for (var i = 0; i < 60; i++) long.push(seg("Ana", i, i + 1, "palavras que enchem um bloco de fala"))
      var blocks = L.turns(long, "")
      if (blocks.length < 3 || blocks.some(function(b) { return b.text.length > 700 })) throw new Error("long monologue not split: " + blocks.length)
      eq(blocks[1].start, blocks[0].end, "split block lost its own start time")

      // Pelo texto, não pelo relógio: um segmento de 300 s com uma frase curta não vira metade da reunião.
      eq(L.talkShares([seg("Ana", 0, 300, "abc"), seg("Áudio remoto", 300, 301, "d"), seg("Bia", 301, 302, "efghij"), seg("Ana", null, null, "  "), seg("Ana", null, null, "  ")]),
         [{name: "Bia", share: 0.6}, {name: "Ana", share: 0.3}, {name: "Áudio remoto", share: 0.1}], "shares")
      eq(L.talkShares([seg("Ana", 0, 9, "")]), [], "shares without text")
      eq([L.genericSpeaker("Áudio remoto"), L.genericSpeaker("Microfone local"), L.genericSpeaker("Ana")], [true, true, false], "generic speaker")

      var now = new Date(2026, 10, 16, 9, 0, 0)
      var m = [{slug: "a", when: "2026-11-16T08:00:00", duration_seconds: 2768, status: "complete", status_label: "Concluída"},
               {slug: "b", when: "2026-11-16T07:00:00", duration_seconds: 0, status: "pending", status_label: "Pendente"},
               {slug: "c", when: "2026-11-15T23:30:00", duration_seconds: 4320, status: "complete", status_label: "Concluída"},
               {slug: "d", when: "", duration_seconds: 20, status: "complete", status_label: "Concluída"}]
      eq(L.historyRows(m, now).map(function(r) { return r.header || r.meeting.slug }), ["Hoje", "a", "b", "Ontem", "c", "Sem data", "d"], "history rows")
      if (!/13.*nov/i.test(L.dayLabel("2026-11-13T10:00:00", now)) || /2026/.test(L.dayLabel("2026-11-13T10:00:00", now))) throw new Error("day label: " + L.dayLabel("2026-11-13T10:00:00", now))
      if (!/2025/.test(L.dayLabel("2025-12-30T10:00:00", now))) throw new Error("other year hides the year")
      var agents = [{name: "Um", shared: {a: {audience: "x", auto: true}}}, {name: "Dois", shared: {}}, {name: "Três", shared: {a: {audience: "y", auto: false}}}]
      eq([L.historyMeta(m[0], agents), L.historyMeta(m[1], agents), L.historyMeta(m[2], []), L.historyMeta(m[3], null)],
         ["08:00 · 46 min · com Um, Três", "07:00 · Pendente", "23:30 · 1 h 12 min", "menos de 1 min"], "history meta")
      eq([L.duration(59 * 60 + 40), L.duration(3600), L.duration(-5)], ["1 h", "1 h", "menos de 1 min"], "duration")
      console.log("LIBRARY_LOGIC_OK"); Qt.exit(0)
    } catch (error) { console.error("LIBRARY_LOGIC_FAIL " + error); Qt.exit(1) }
  } }
}
'''


class TestLibraryLogic(unittest.TestCase):
    def test_turns_shares_and_history_in_the_real_qml_engine(self):
        self.assertTrue(shutil.which('quickshell'), 'Quickshell obrigatório para a lógica da biblioteca')
        with tempfile.TemporaryDirectory(prefix='castanha-library-logic-') as temporary:
            root = Path(temporary)
            (root / 'LibraryLogic.js').symlink_to(ROOT / 'LibraryLogic.js')
            (root / 'shell.qml').write_text(QML)
            env = dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                       XDG_CACHE_HOME=str(root / 'cache'), QT_QPA_PLATFORM='offscreen', TZ='America/Sao_Paulo')
            result = subprocess.run(['quickshell', '--no-duplicate', '--path', str(root / 'shell.qml'), '--no-color'],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout[-6000:])
            self.assertIn('LIBRARY_LOGIC_OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
