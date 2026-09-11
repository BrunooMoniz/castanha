import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io

ColumnLayout {
  id: root
  property string meetingSlug: ""
  property var cliCommand: ["castanha"]
  SystemPalette { id: systemPalette }
  property color foreground: systemPalette.windowText
  property color accent: foreground
  property string fontFamily: "sans-serif"
  property var buffers: ({})
  property string regenerationMessage: ""
  property string regenerateAfterSave: ""
  readonly property var currentBuffer: buffers[meetingSlug] || null
  readonly property string text: currentBuffer ? currentBuffer.text : ""
  readonly property bool dirty: !!currentBuffer && currentBuffer.text !== currentBuffer.savedText
  readonly property bool saving: saveProcess.running && saveProcess.requestedSlug === meetingSlug
  readonly property bool loading: !currentBuffer || !currentBuffer.loaded
  readonly property color muted: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.7)
  signal summaryUpdated(string slug)
  spacing: 12

  function put(slug, buffer) {
    var next = Object.assign({}, buffers)
    next[slug] = buffer
    buffers = next
  }
  function requestLoad() {
    if (!loadProcess || !meetingSlug || loadProcess.running || (buffers[meetingSlug] && buffers[meetingSlug].loaded)) return
    loadProcess.requestedSlug = meetingSlug
    loadProcess.command = cliCommand.concat(["annotations", "get", meetingSlug, "--json"])
    loadProcess.running = true
  }
  function edit(value) {
    if (!currentBuffer || !currentBuffer.loaded || value === currentBuffer.text) return
    put(meetingSlug, Object.assign({}, currentBuffer, {text: value}))
    autosave.restart()
  }
  function flush() {
    if (!autosave || !saveProcess) return
    autosave.stop()
    if (saveProcess.running) return
    var slugs = Object.keys(buffers)
    for (var i = 0; i < slugs.length; i++) {
      var slug = slugs[i], buffer = buffers[slug]
      if (!buffer.loaded || buffer.error || buffer.text === buffer.savedText) continue
      saveProcess.requestedSlug = slug
      saveProcess.submittedText = buffer.text
      saveProcess.stdinEnabled = true
      saveProcess.command = cliCommand.concat(["annotations", "save", slug, "--stdin", "--expected-revision", buffer.revision, "--json"])
      saveProcess.running = true
      return
    }
    if (regenerateAfterSave) {
      var target = regenerateAfterSave
      regenerateAfterSave = ""
      var saved = buffers[target]
      if (saved && !saved.error && saved.text === saved.savedText) runRegeneration(target)
    }
  }
  function saveNow() {
    if (currentBuffer) put(meetingSlug, Object.assign({}, currentBuffer, {error: ""}))
    flush()
  }
  function regenerate() {
    if (!currentBuffer || !text.trim() || regenerateProcess.running) return
    regenerateAfterSave = meetingSlug
    regenerationMessage = "Salvando notas antes de atualizar o resumo…"
    saveNow()
  }
  function runRegeneration(slug) {
    regenerateProcess.requestedSlug = slug
    regenerateProcess.command = cliCommand.concat(["annotations", "regenerate", slug, "--json"])
    regenerateProcess.running = true
    regenerationMessage = "Preparando resumo e insights com suas anotações…"
  }
  Component.onCompleted: requestLoad()
  onMeetingSlugChanged: { regenerationMessage = ""; flush(); requestLoad() }
  Timer { id: autosave; interval: 700; onTriggered: root.flush() }

  Process {
    id: loadProcess
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      var result
      try {
        result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok" || typeof result.text !== "string" || typeof result.revision !== "string") throw new Error("invalid")
        root.put(requestedSlug, {loaded: true, text: result.text, savedText: result.text, revision: result.revision, error: ""})
      } catch (error) {
        root.put(requestedSlug, {loaded: false, text: "", savedText: "", revision: "", error: "Não foi possível abrir as anotações. Tente recarregar."})
      }
      if (root.meetingSlug !== requestedSlug) Qt.callLater(root.requestLoad)
    }
  }
  Process {
    id: saveProcess
    property string requestedSlug: ""
    property string submittedText: ""
    stdout: StdioCollector {}
    onStarted: { write(submittedText); stdinEnabled = false }
    onExited: function(code) {
      var buffer = root.buffers[requestedSlug]
      try {
        var result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok" || typeof result.revision !== "string") {
          throw new Error(result.status === "conflict" ? "As notas foram alteradas fora desta janela. Seu texto foi mantido aqui para evitar sobrescrever a outra versão." : (result.message || "Não foi possível salvar. Seu texto continua nesta janela."))
        }
        root.put(requestedSlug, Object.assign({}, buffer, {savedText: submittedText, revision: result.revision, error: ""}))
      } catch (error) {
        root.put(requestedSlug, Object.assign({}, buffer, {error: String(error.message || error)}))
        if (root.regenerateAfterSave === requestedSlug) {
          root.regenerateAfterSave = ""
          root.regenerationMessage = "Salve as anotações antes de atualizar o resumo."
        }
      }
      Qt.callLater(root.flush)
    }
  }
  Process {
    id: regenerateProcess
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      try {
        var result = JSON.parse(stdout.text)
        if (result.status !== "ok" && result.status !== "pending") throw new Error(result.message || "Não foi possível atualizar o resumo agora.")
        if (requestedSlug === root.meetingSlug) root.regenerationMessage = result.status === "pending"
          ? (result.message || "Pedido salvo na fila. O resumo será retomado quando o processamento estiver disponível.")
          : (result.message || "Resumo atualizado com suas anotações.")
        root.summaryUpdated(requestedSlug)
      } catch (error) {
        if (requestedSlug === root.meetingSlug) root.regenerationMessage = String(error.message || error)
      }
    }
  }

  Text { text: "Minhas anotações"; color: root.foreground; font.family: root.fontFamily; font.pixelSize: 21; font.bold: true }
  Text { Layout.fillWidth: true; text: "Escreva aqui o contexto, decisões e pontos importantes. Suas notas complementam o resumo sem alterar a transcrição."; wrapMode: Text.Wrap; color: root.muted; font.family: root.fontFamily; font.pixelSize: 13 }
  Rectangle {
    Layout.fillWidth: true
    Layout.fillHeight: true
    Layout.minimumHeight: 100
    radius: 8
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
    border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)
    ScrollView {
      anchors.fill: parent
      anchors.margins: 8
      contentWidth: availableWidth
      clip: true
      TextArea {
        id: editor
        objectName: "meetingNotesEditor"
        enabled: !root.loading
        text: root.text
        onTextChanged: root.edit(text)
        placeholderText: root.loading ? "Abrindo anotações…" : "O que vale lembrar desta reunião?"
        textFormat: TextEdit.PlainText
        wrapMode: TextEdit.Wrap
        color: root.foreground
        selectionColor: root.accent
        font.family: root.fontFamily
        font.pixelSize: 16
        background: null
        selectByMouse: true
      }
    }
  }
  Text {
    Layout.fillWidth: true
    text: root.currentBuffer && root.currentBuffer.error ? root.currentBuffer.error : root.saving ? "Salvando…" : root.dirty ? "Alterações ainda não salvas" : root.loading ? "" : "Salvo no computador · Markdown"
    textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.fontFamily; font.pixelSize: 12
  }
  Flow {
    Layout.fillWidth: true
    Layout.preferredHeight: childrenRect.height
    spacing: 8
    LibraryButton { text: root.saving ? "Salvando…" : "Salvar agora"; enabled: root.dirty && !root.saving; foreground: root.foreground; font.family: root.fontFamily; onClicked: root.saveNow() }
    LibraryButton { objectName: "meetingNotesRegenerate"; text: regenerateProcess.running ? "Atualizando resumo…" : "Atualizar resumo com minhas notas"; enabled: !root.loading && !!root.text.trim() && !regenerateProcess.running; foreground: root.foreground; font.family: root.fontFamily; onClicked: root.regenerate() }
    LibraryButton { text: "Recarregar"; enabled: !root.dirty && !root.saving && !loadProcess.running; foreground: root.foreground; font.family: root.fontFamily; onClicked: { var next = Object.assign({}, root.buffers); delete next[root.meetingSlug]; root.buffers = next; root.requestLoad() } }
  }
  Text { Layout.fillWidth: true; visible: !!root.regenerationMessage; text: root.regenerationMessage; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.fontFamily; font.pixelSize: 13 }
}
