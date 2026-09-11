"""Tema do Castanha com Commons/Ui reais em processo e HOME isolados."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')

QML = r'''
import QtQuick
import Quickshell
import qs.Commons
ShellRoot {
  id: test
  property int stage: 0
  property int ticks: 0
  property var saved: ({})
  function require(value, message) { if (!value) throw new Error(message) }
  function objects(root) {
    var found = [], seen = []
    function walk(obj, depth) {
      if (!obj || depth > 16 || seen.indexOf(obj) >= 0) return
      seen.push(obj); found.push(obj)
      var lists = [obj.data, obj.children, obj.contentItem]
      for (var i = 0; i < lists.length; i++) {
        var list = lists[i]; if (!list) continue
        if (list.length === undefined) list = [list]
        for (var j = 0; j < list.length; j++) walk(list[j], depth + 1)
      }
    }
    walk(root, 0); return found
  }
  function find(root, name) {
    var all = objects(root)
    for (var i = 0; i < all.length; i++) if (all[i].objectName === name) return all[i]
    throw new Error('missing object ' + name)
  }
  function borders(root) {
    return objects(root).filter(function(o) { return o.borderSpec !== undefined && o.usesOverlayBorder !== undefined })
  }
  function sameColor(left, right) { return String(left) === String(right) }
  function widths(surface, top, right, bottom, left) {
    require(surface.borderTop === top && surface.borderRight === right && surface.borderBottom === bottom && surface.borderLeft === left,
            'theme border widths not inherited on ' + surface + ': ' + JSON.stringify(surface.borderSpec))
  }
  function finish() { console.log('CASTANHA_THEME_OK'); Qt.exit(0) }
  LibraryWindow { id: library; visible: false; cliCommand: ['/bin/false'] }
  FloatingWindow {
    visible: true; implicitWidth: 1000; implicitHeight: 850
    Item { id: neutral; anchors.fill: parent; focus: true }
    LibraryButton { id: button; x: 5; y: 5; text: 'Tema de teste'; hoverEnabled: false }
    MeetingNotes { id: notes; x: 5; y: 70; width: 600; height: 550; cliCommand: ['/bin/false'] }
    ChannelMeters { id: meters; x: 620; y: 70; width: 350 }
  }
  Timer {
    interval: 50; running: true; repeat: true
    onTriggered: {
      try {
        if (++test.ticks > 100) throw new Error('theme timeout stage=' + test.stage)
        if (test.ticks < 12) return
        __STEPS__
      } catch (error) { console.error('CASTANHA_THEME_FAIL ' + error); Qt.exit(1) }
    }
  }
}
'''


class TestLibraryTheme(unittest.TestCase):
    def run_theme(self, steps):
        self.assertTrue(shutil.which('quickshell'), 'Quickshell obrigatório para prova de tema')
        with tempfile.TemporaryDirectory(prefix='castanha-theme-') as temporary:
            root = Path(temporary)
            for source in ROOT.glob('*.qml'):
                (root/source.name).symlink_to(source)
            for source in ROOT.glob('*.js'):
                (root/source.name).symlink_to(source)
            for name in ('Commons', 'Ui'):
                (root/name).symlink_to(SHELL/name, target_is_directory=True)
            (root/'shell.qml').write_text(QML.replace('__STEPS__', steps))
            env = dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root/'config'),
                       XDG_STATE_HOME=str(root/'state'), XDG_CACHE_HOME=str(root/'cache'),
                       QT_QPA_PLATFORM='offscreen', QT_QUICK_CONTROLS_STYLE='Basic')
            result = subprocess.run(['quickshell', '--no-duplicate', '--path', str(root/'shell.qml'), '--no-color'],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout[-14000:])
            self.assertIn('CASTANHA_THEME_OK', result.stdout)
            for error in ('TypeError', 'ReferenceError', 'Binding loop detected'):
                self.assertNotIn(error, result.stdout)

    def test_light_dark_palette_font_family_and_user_override_change_live(self):
        self.run_theme(r'''
        if (test.stage === 0) {
          Color.loadColors('foreground = "#eeeeee"\nbackground = "#101010"\naccent = "#abcdef"')
          Color.loadShell('[popups]\nbackground = "#202020"\ntext = "#ededed"')
          Style.fontFamily = 'DejaVu Sans Mono'; test.stage = 1
        } else if (test.stage === 1) {
          test.require(test.sameColor(library.color, '#202020') && test.sameColor(library.foreground, '#ededed'), 'dark popup roles ignored')
          test.require(meters.dark && meters.micColor.hslLightness > .5, 'dark meter contrast not adapted')
          test.require(button.font.family === Style.font.family && notes.fontFamily === Style.font.family
                       && library.readingFontFamily === Style.font.family && meters.fontFamily === Style.font.family, 'font family hardcoded')
          test.saved = {mic: String(meters.micColor), button: String(button.contentItem.color)}
          Color.loadColors('foreground = "#151515"\nbackground = "#fafafa"\naccent = "#024681"')
          Color.loadUserShell('[popups]\nbackground = "#f4f2ea"\ntext = "#112233"')
          Color.loadShell('[popups]\nbackground = "#ffffff"\ntext = "#222222"')
          Style.fontFamily = 'DejaVu Serif'; test.stage = 2
        } else {
          test.require(test.sameColor(library.color, '#f4f2ea') && test.sameColor(library.foreground, '#112233'), 'user popup overrides lost on theme switch')
          test.require(test.sameColor(notes.foreground, '#112233'), 'editor popup text does not follow theme')
          test.require(!meters.dark && meters.micColor.hslLightness < .5 && String(meters.micColor) !== test.saved.mic, 'light meter contrast stale')
          test.require(String(button.contentItem.color) !== test.saved.button, 'button palette stale')
          test.require(button.font.family === 'DejaVu Serif' && notes.fontFamily === 'DejaVu Serif'
                       && library.readingFontFamily === 'DejaVu Serif' && meters.fontFamily === 'DejaVu Serif', 'font change not propagated')
          test.finish()
        }
        ''')

    def test_gradient_asymmetric_borders_rounding_and_control_states(self):
        self.run_theme(r'''
        if (test.stage === 0) {
          Color.loadShell('[controls]\nnormal-border = "#112233 #abcdef 45deg"\nnormal-border-width = "1 2 3 4"\nselected-border-width = 5\nfocus-border-width = 6\nnormal-fill-alpha = 0.1\nselected-fill-alpha = 0.2\nfocus-fill-alpha = 0.3\npressed-fill-alpha = 0.4')
          Style.applyRoundingJson('{"int": 13}'); neutral.forceActiveFocus(); test.stage = 1
        } else if (test.stage === 1) {
          var surfaces = [button.background, test.find(notes, 'meetingNotesSurface'), test.find(library, 'librarySearchSurface')].concat(test.borders(meters))
          test.require(surfaces.length >= 5, 'channel surfaces missing')
          for (var i = 0; i < surfaces.length; i++) {
            test.widths(surfaces[i], 1, 2, 3, 4)
            test.require(surfaces[i].radius === 13 && surfaces[i].usesOverlayBorder && surfaces[i].borderSpec.gradient.enabled, 'gradient or rounding flattened')
          }
          button.selected = true; test.stage = 2
        } else if (test.stage === 2) {
          test.widths(button.background, 5, 5, 5, 5)
          test.require(test.sameColor(button.background.color, Style.selectedFillFor(button.foreground, button.accent)), 'selected fill ignores theme')
          button.forceActiveFocus(); test.stage = 3
        } else if (test.stage === 3) {
          test.widths(button.background, 6, 6, 6, 6)
          test.require(test.sameColor(button.background.color, Style.focusFillFor(button.foreground, button.accent)), 'focus fill ignores theme')
          button.down = true; test.stage = 4
        } else if (test.stage === 4) {
          test.require(test.sameColor(button.background.color, Style.pressedFillFor(button.foreground, button.accent)), 'pressed fill ignores theme')
          button.down = false; button.selected = false; neutral.forceActiveFocus()
          Color.loadShell('[controls]\nnormal-border = "#234567"\nnormal-border-width = 2')
          Style.applyRoundingJson('{"int": 0}'); test.stage = 5
        } else {
          var surfaces = [button.background, test.find(notes, 'meetingNotesSurface'), test.find(library, 'librarySearchSurface')].concat(test.borders(meters))
          for (var i = 0; i < surfaces.length; i++) {
            test.widths(surfaces[i], 2, 2, 2, 2)
            test.require(surfaces[i].radius === 0 && !surfaces[i].usesOverlayBorder, 'new flat theme leaves stale gradient/rounding')
          }
          test.finish()
        }
        ''')

    def test_font_size_configuration_reaches_editor_and_library_body(self):
        self.run_theme(r'''
        if (test.stage === 0) {
          Color.loadShell('[font]\nbase-size = 12'); test.stage = 1
        } else if (test.stage === 1) {
          test.saved = {editor: test.find(notes, 'meetingNotesEditor').font.pixelSize,
                        body: test.find(library, 'libraryRawTranscript').font.pixelSize,
                        button: button.font.pixelSize}
          Color.loadShell('[font]\nbase-size = 24\nbody = 27\nheading = 33'); test.stage = 2
        } else {
          test.require(button.font.pixelSize === 27 && button.font.pixelSize > test.saved.button, 'button ignores body font token')
          test.require(test.find(notes, 'meetingNotesEditor').font.pixelSize > test.saved.editor, 'editor font ignores larger configured theme')
          test.require(test.find(library, 'libraryRawTranscript').font.pixelSize > test.saved.body, 'transcript font ignores larger configured theme')
          test.finish()
        }
        ''')


if __name__ == '__main__':
    unittest.main()
