// Interface pública: showMeeting(slug), slug vazio abre o histórico.
// foreground/fontFamily podem ser herdados do Panel; background/accent e
// readingFontFamily são opcionais. Fechar interrompe apenas a reprodução.
import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts
import QtMultimedia
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui as OmarchyUi
import "LibraryLogic.js" as LibraryLogic

FloatingWindow {
  id: root
  title: "Castanha · Biblioteca de reuniões"
  visible: false
  implicitWidth: 1100
  implicitHeight: 760
  minimumSize: Qt.size(800, 600)
  color: background
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family
  property string readingFontFamily: fontFamily
  property var cliCommand: ["castanha"]
  property var clipboardCommand: ["wl-copy", "--type", "text/plain;charset=utf-8"]
  property var meetings: []
  property var detail: null
  property string selectedSlug: ""
  property string query: ""
  property string statusFilter: "all"
  property string historyError: ""
  property string detailError: ""
  property string copyFeedback: ""
  property int currentTab: 0
  property bool showRawTranscript: false
  property bool creatingNote: false
  property string creationError: ""
  property int audioIndex: -1
  property string audioError: ""
  // Vincular a reunião a um evento da agenda do dia da gravação.
  property bool linking: false
  property var dayEvents: []
  property string linkTarget: ""
  property string linkNotice: ""
  property string linkFeedback: ""
  property bool dayEventsQueued: false
  // Agentes que recebem notas (config `agents`). Consultado ao abrir e depois de cada pedido, nunca no relógio.
  property var agents: []
  property real pendingSeek: -1
  property bool playWhenReady: false
  readonly property color muted: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.66)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.07)
  readonly property var filteredMeetings: LibraryLogic.filtered(meetings, query, statusFilter)
  readonly property var historyRows: LibraryLogic.historyRows(filteredMeetings)
  property string transcriptQuery: ""
  readonly property var transcriptTurns: LibraryLogic.turns(current ? current.segments : [], transcriptQuery)
  readonly property var talkShares: LibraryLogic.talkShares(current ? current.segments : [])
  readonly property bool namedSpeakers: talkShares.some(function(t) { return !LibraryLogic.genericSpeaker(t.name) })
  readonly property var current: detail && detail.slug === selectedSlug ? detail : null
  readonly property var summaryDocument: LibraryLogic.notesDocument(current ? current.summary : "", current ? current.title : "")
  readonly property var extraDecisions: LibraryLogic.extraFacts(current ? current.decisions : [], summaryDocument, "decisions")
  readonly property var extraActions: LibraryLogic.extraFacts(current ? current.action_items : [], summaryDocument, "actions")
  readonly property bool historyLoading: historyProcess.running
  readonly property bool detailLoading: detailProcess.running
  property bool detailRefreshQueued: false
  property string mutatingSlug: ""
  readonly property var audioRecords: current ? current.recordings || [] : []
  readonly property string currentCopyText: currentTab === 3 ? notesEditor.text : !current ? "" : currentTab === 1 ? current.transcript :
    [LibraryLogic.plainNotes(current.summary), current.decisions.length ? "Decisões\n" + current.decisions.join("\n") : "",
     current.action_items.length ? "Próximos passos\n" + current.action_items.join("\n") : ""].filter(function(x) { return x }).join("\n\n")

  function showMeeting(slug) {
    // O fechamento nativo pode ocultar o backing window sem zerar o
    // estado interno do wrapper. Reafirme false antes de reabrir.
    if (!root.backingWindowVisible) root.visible = false
    root.minimized = false
    root.visible = true
    Qt.callLater(function() {
      var window = root.contentItem.Window.window
      if (window) window.requestActivate()
    })
    refreshHistory()
    refreshAgents()
    if (slug) selectMeeting(String(slug))
  }
  function refreshAgents() {
    if (agentsProcess.running) return
    agentsProcess.command = cliCommand.concat(["agents", "status", "--json"])
    agentsProcess.running = true
  }
  function refreshHistory() {
    if (historyProcess.running) return
    historyProcess.command = cliCommand.concat(["library", "--json"])
    historyProcess.running = true
  }
  function selectMeeting(slug) {
    player.stop()
    player.source = ""
    audioIndex = -1
    pendingSeek = -1
    playWhenReady = false
    audioError = ""
    copyFeedback = ""
    selectedSlug = slug
    currentTab = 0
    showRawTranscript = false
    transcriptSearch.text = ""
    detailError = ""
    linking = false; linkTarget = ""; linkNotice = ""; linkFeedback = ""; dayEvents = []
    if (!detailProcess.running) startDetail()
  }
  function openLink() {
    if (!current) return
    linking = true; linkTarget = ""; linkNotice = ""; linkFeedback = ""; dayEvents = []
    // Uma consulta em andamento pertence à reunião que a pediu: a nova espera.
    if (dayEventsProcess.running) { dayEventsQueued = true; return }
    startDayEvents()
  }
  function startDayEvents() {
    dayEventsQueued = false
    dayEventsProcess.requestedSlug = selectedSlug
    dayEventsProcess.command = cliCommand.concat(["agenda", "--json", "--dia", LibraryLogic.isoDate(current.when)])
    dayEventsProcess.running = true
  }
  function cancelLink() { linking = false; linkTarget = ""; linkNotice = "" }
  function confirmLink() {
    if (!current || !linkTarget || linkProcess.running) return
    linkProcess.requestedSlug = selectedSlug
    linkProcess.command = cliCommand.concat(["link-event", "--json", "--date", LibraryLogic.isoDate(current.when), "--", selectedSlug, linkTarget])
    linkProcess.running = true
  }
  function startDetail() {
    if (!selectedSlug) return
    if (selectedSlug === mutatingSlug) return
    if (detailProcess.running) { detailRefreshQueued = true; return }
    detailProcess.requestedSlug = selectedSlug
    detailProcess.command = cliCommand.concat(["library", "--json", "--", selectedSlug])
    detailProcess.running = true
  }
  function prepareRecordingMutation(slug) {
    mutatingSlug = slug
    if (slug !== selectedSlug) return
    player.stop(); player.source = ""; audioIndex = -1; playWhenReady = false; pendingSeek = -1
    detail = null
  }
  function finishRecordingMutation(slug) {
    if (mutatingSlug === slug) mutatingSlug = ""
    if (slug === selectedSlug) startDetail()
    refreshHistory()
  }
  function loadAudio(index, start, play) {
    if (index < 0 || index >= audioRecords.length || !LibraryLogic.localAudio(audioRecords[index].url)) {
      audioError = "Áudio local indisponível."
      return
    }
    audioError = ""
    if (audioIndex === index && player.source.toString() === audioRecords[index].url && player.seekable) {
      if (start >= 0) player.position = start * 1000
      if (play) player.play()
      return
    }
    player.stop()
    audioIndex = index
    pendingSeek = start >= 0 ? start * 1000 : 0
    playWhenReady = play
    player.source = audioRecords[index].url
  }
  function seekSegment(segment) {
    if (!segment.can_seek) return
    loadAudio(segment.audio_index, segment.start, true)
  }
  function copyText(value) {
    if (!value || clipboard.running) return
    clipboard.pendingText = value
    clipboard.stdinEnabled = true
    clipboard.command = clipboardCommand
    clipboard.running = true
  }
  onClosed: root.visible = false
  onVisibleChanged: if (!visible) { player.stop(); playWhenReady = false; if (notesEditor) notesEditor.flush() }
  Shortcut { sequence: "Escape"; enabled: root.visible; onActivated: root.visible = false }
  Shortcut { sequence: "Ctrl+F"; enabled: root.visible; onActivated: searchField.forceActiveFocus() }

  Process {
    id: historyProcess
    objectName: "libraryHistoryProcess"
    stdout: StdioCollector {}
    onExited: function(code) {
      try {
        var result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok" || !Array.isArray(result.meetings)) throw new Error("failed")
        root.meetings = result.meetings
        var selected = result.meetings.find(function(item) { return item.slug === root.selectedSlug })
        if (selected && root.current && (selected.recording_revision !== root.current.recording_revision || selected.content_status !== root.current.content_status || selected.cleanup_status !== root.current.cleanup_status || selected.has_summary !== root.current.has_summary || selected.status !== root.current.status || selected.status_label !== root.current.status_label || selected.can_restore !== root.current.can_restore || selected.exclusion_id !== root.current.exclusion_id)) root.startDetail()
        root.historyError = Array.isArray(result.warnings) ? result.warnings.join("\n") : ""
      } catch (error) {
        root.historyError = "Não foi possível atualizar o histórico. A lista anterior foi preservada."
      }
    }
  }
  Process {
    id: detailProcess
    objectName: "libraryDetailProcess"
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      if (requestedSlug === root.mutatingSlug) return
      if (root.detailRefreshQueued) { root.detailRefreshQueued = false; root.startDetail(); return }
      if (requestedSlug === root.selectedSlug) {
        try {
          var result = JSON.parse(stdout.text)
          if (code !== 0 || result.status !== "ok" || !result.meeting || result.meeting.slug !== requestedSlug) throw new Error("failed")
          root.detail = result.meeting
          root.detailError = ""
        } catch (error) {
          root.detailError = "Não foi possível ler esta reunião. Os arquivos permanecem preservados."
        }
      } else root.startDetail()
    }
  }
  Process {
    id: dayEventsProcess
    objectName: "libraryDayEventsProcess"
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      if (root.dayEventsQueued) { if (root.linking && root.current) root.startDayEvents(); else root.dayEventsQueued = false; return }
      if (requestedSlug !== root.selectedSlug || !root.linking) return
      try {
        var result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok" || !Array.isArray(result.meetings))
          throw new Error(result && result.message ? result.message : "Não foi possível consultar a agenda desse dia.")
        root.dayEvents = result.meetings
        root.linkNotice = Array.isArray(result.warnings) && result.warnings.length ? "Algumas agendas não responderam: " + result.warnings.join("; ") : ""
      } catch (error) { root.dayEvents = []; root.linkNotice = String(error.message || error) }
    }
  }
  Process {
    id: linkProcess
    objectName: "libraryLinkProcess"
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      var slug = requestedSlug
      try {
        var result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok") throw new Error(result && result.message ? result.message : "Não foi possível vincular a reunião.")
        if (slug === root.selectedSlug) { root.linkFeedback = result.message || "Reunião vinculada."; root.linking = false; root.linkTarget = "" }
      } catch (error) { if (slug === root.selectedSlug) root.linkNotice = String(error.message || error) }
      if (slug === root.selectedSlug) root.startDetail()
      root.refreshHistory()
    }
  }
  Process {
    id: agentsProcess
    objectName: "libraryAgentsProcess"
    stdout: StdioCollector {}
    onExited: function(code) {
      // Sem agente, CLI antigo ou resposta estranha: a biblioteca segue igual, só sem o compartilhamento.
      try { var result = JSON.parse(stdout.text); root.agents = code === 0 && Array.isArray(result.agents) ? result.agents : [] }
      catch (error) { root.agents = [] }
    }
  }
  Process {
    id: createNoteProcess
    stdout: StdioCollector {}
    onExited: function(code) {
      try {
        var result = JSON.parse(stdout.text)
        if (code !== 0 || result.status !== "ok" || !result.slug) throw new Error(result.message || "Não foi possível criar a anotação.")
        root.creatingNote = false
        noteTitle.text = ""
        root.selectMeeting(result.slug)
        root.currentTab = 3
        root.refreshHistory()
      } catch (error) { root.creationError = String(error.message || error) }
    }
  }
  Process {
    id: clipboard
    property string pendingText: ""
    stdinEnabled: true
    onStarted: { write(pendingText); pendingText = ""; stdinEnabled = false }
    onExited: function(code) {
      root.copyFeedback = code === 0 ? "Texto copiado" : "Não foi possível copiar o texto"
      copyReset.restart()
    }
  }
  Timer { id: copyReset; interval: 3000; onTriggered: root.copyFeedback = "" }
  Timer { interval: 30000; running: root.visible; repeat: true; onTriggered: root.refreshHistory() }

  MediaPlayer {
    id: player
    objectName: "libraryMediaPlayer"
    audioOutput: AudioOutput { volume: 1 }
    onErrorOccurred: function(error, message) { root.audioError = "Não foi possível reproduzir este áudio local."; root.playWhenReady = false }
    onMediaStatusChanged: {
      if ((mediaStatus === MediaPlayer.LoadedMedia || mediaStatus === MediaPlayer.BufferedMedia) && seekable) {
        if (root.pendingSeek >= 0) { position = root.pendingSeek; root.pendingSeek = -1 }
        if (root.playWhenReady) { root.playWhenReady = false; play() }
      }
    }
  }

  Rectangle {
    id: libraryCanvas
    objectName: "libraryCanvas"
    anchors.fill: parent
    color: root.background
  RowLayout {
    anchors.fill: parent
    spacing: Style.space(0)
    Rectangle {
      Layout.preferredWidth: root.width < 940 ? 270 : 320
      Layout.fillHeight: true
      color: root.faint
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(18)
        spacing: Style.space(14)
        RowLayout {
          Layout.fillWidth: true
          Text { Layout.fillWidth: true; text: "Reuniões"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.display; font.weight: Font.DemiBold }
          LibraryButton { objectName: "libraryNewNote"; text: "Nova anotação"; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: { root.creatingNote = true; root.creationError = ""; noteTitle.forceActiveFocus() } }
        }
        ColumnLayout {
          visible: root.creatingNote
          Layout.fillWidth: true
          TextField { id: noteTitle; objectName: "libraryNoteTitle"; Layout.fillWidth: true; placeholderText: "Título da reunião"; color: root.foreground; font.family: root.readingFontFamily; selectByMouse: true; background: OmarchyUi.BorderSurface { color: Style.controlFill(noteTitle.activeFocus, noteTitle.hovered, root.foreground, root.accent); radius: Style.cornerRadius; borderSpec: Border.controlSpec(noteTitle.activeFocus ? "focus" : "normal", root.foreground, root.accent) } }
          RowLayout {
            LibraryButton { objectName: "libraryCreateNote"; text: createNoteProcess.running ? "Criando…" : "Criar"; enabled: !!noteTitle.text.trim() && !createNoteProcess.running; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: { root.creationError = ""; createNoteProcess.command = root.cliCommand.concat(["annotations", "create", "--title", noteTitle.text.trim(), "--json"]); createNoteProcess.running = true } }
            LibraryButton { text: "Cancelar"; enabled: !createNoteProcess.running; foreground: root.foreground; onClicked: root.creatingNote = false }
          }
          Text { Layout.fillWidth: true; visible: !!root.creationError; text: root.creationError; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground }
        }
        TextField {
          id: searchField
          objectName: "librarySearch"
          Layout.fillWidth: true
          implicitHeight: 38
          placeholderText: "Buscar reunião"
          color: root.foreground
          placeholderTextColor: root.muted
          font.family: root.readingFontFamily
          font.pixelSize: Style.font.title
          selectByMouse: true
          onTextChanged: root.query = text
          background: OmarchyUi.BorderSurface { objectName: "librarySearchSurface"; color: Style.controlFill(searchField.activeFocus, searchField.hovered, root.foreground, root.accent); radius: Style.cornerRadius; borderSpec: Border.controlSpec(searchField.activeFocus ? "focus" : "normal", root.foreground, root.accent) }
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(5)
          Repeater {
            model: [{label: "Todas", value: "all"}, {label: "Pendentes", value: "pending"}, {label: "Concluídas", value: "complete"}]
            LibraryButton {
              required property var modelData
              Layout.fillWidth: true
              text: modelData.label
              foreground: root.foreground; accent: root.accent
              selected: root.statusFilter === modelData.value
              font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall
              onClicked: root.statusFilter = modelData.value
            }
          }
        }
        Text { Layout.fillWidth: true; visible: root.historyError !== ""; text: root.historyError; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
        RowLayout {
          Layout.fillWidth: true
          Text { Layout.fillWidth: true; text: root.historyLoading ? "Atualizando…" : root.filteredMeetings.length + (root.filteredMeetings.length === 1 ? " reunião" : " reuniões"); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
          LibraryButton { text: "Atualizar"; enabled: !root.historyLoading; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; onClicked: root.refreshHistory() }
        }
        ListView {
          id: historyList
          objectName: "libraryHistory"
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          spacing: Style.space(2)
          model: root.historyRows
          activeFocusOnTab: true
          keyNavigationEnabled: true
          function openCurrent() { var row = root.historyRows[currentIndex]; if (row && row.meeting) root.selectMeeting(row.meeting.slug) }
          property int lastIndex: -1
          onCurrentIndexChanged: {
            var target = LibraryLogic.meetingRow(root.historyRows, currentIndex, lastIndex)
            if (target !== currentIndex) currentIndex = target; else lastIndex = currentIndex
          }
          Keys.onReturnPressed: openCurrent()
          Keys.onEnterPressed: openCurrent()
          ScrollBar.vertical: ScrollBar {}
          // Uma linha por reunião, agrupadas por dia: o cabeçalho do dia é uma linha da própria lista.
          delegate: Item {
            id: historyRow
            required property var modelData
            required property int index
            readonly property var meeting: modelData.meeting
            readonly property bool chosen: !!meeting && root.selectedSlug === meeting.slug
            width: historyList.width
            height: meeting ? rowContent.implicitHeight + Style.space(16) : dayHeader.implicitHeight + Style.space(index ? 16 : 4)
            Text { id: dayHeader; visible: !historyRow.meeting; anchors.bottom: parent.bottom; anchors.bottomMargin: Style.space(4); x: Style.space(8); text: historyRow.modelData.header; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall; font.weight: Font.DemiBold }
            Rectangle {
              visible: !!historyRow.meeting
              anchors.fill: parent
              radius: Style.cornerRadius
              color: historyRow.chosen ? Style.selectedFillFor(root.foreground, root.accent) : rowMouse.containsMouse ? Style.hoverFillFor(root.foreground, root.accent) : "transparent"
              border.width: historyList.activeFocus && historyList.currentIndex === historyRow.index ? 1 : 0
              border.color: root.accent
              Column {
                id: rowContent
                anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.space(8)
                spacing: Style.space(3)
                Text { width: parent.width; text: historyRow.meeting ? historyRow.meeting.title : ""; textFormat: Text.PlainText; elide: Text.ElideRight; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.title; font.weight: historyRow.chosen ? Font.DemiBold : Font.Normal }
                Text { width: parent.width; text: historyRow.meeting ? LibraryLogic.historyMeta(historyRow.meeting, root.agents) : ""; textFormat: Text.PlainText; elide: Text.ElideRight; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall }
              }
              MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; onClicked: { historyList.currentIndex = historyRow.index; historyList.forceActiveFocus(); root.selectMeeting(historyRow.meeting.slug) } }
            }
          }
          Text { anchors.centerIn: parent; width: parent.width - 20; visible: !root.historyLoading && root.filteredMeetings.length === 0; text: root.meetings.length ? "Nenhuma reunião encontrada.\nTente outro filtro." : "Suas gravações aparecerão aqui."; textFormat: Text.PlainText; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.title }
        }
      }
    }
    Rectangle { Layout.preferredWidth: 1; Layout.fillHeight: true; color: root.faint }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.fillHeight: true
      Layout.margins: root.width < 940 ? 22 : 32
      spacing: Style.space(18)
      RowLayout {
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: root.current ? LibraryLogic.date(root.current.when) + (root.current.duration_seconds > 0 ? " · " + LibraryLogic.duration(root.current.duration_seconds) : "") : "BIBLIOTECA LOCAL"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
        LibraryButton { text: "Fechar"; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: root.visible = false }
      }
      Text {
        Layout.fillWidth: true
        text: root.current ? root.current.title : root.detailLoading ? "Abrindo reunião…" : "Tudo o que foi conversado,\nno mesmo lugar."
        textFormat: Text.PlainText; wrapMode: Text.Wrap
        color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: root.width < 940 ? Style.font.display : Style.font.displayLarge; font.weight: Font.DemiBold
      }
      Text { objectName: "libraryContentStatus"; Layout.fillWidth: true; visible: !!root.current && !!root.current.content_status && (root.current.content_status !== "current" || root.current.cleanup_status === "pending"); text: root.current ? root.current.status_label : ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
      Text { Layout.fillWidth: true; visible: root.detailError !== ""; text: root.detailError; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.title }
      // Evento da agenda desta gravação. "Vincular" corrige a reunião gravada na
      // mão ou anexada ao evento errado, sem pedir a ninguém.
      RowLayout {
        visible: root.current !== null && root.current.source !== "manual"
        Layout.fillWidth: true
        spacing: Style.space(8)
        Text { objectName: "libraryEventLine"; Layout.fillWidth: true; text: !root.current ? "" : root.current.event_linked ? "Evento da agenda: " + root.current.event_title + (root.current.event_attendees.length ? " · " + root.current.event_attendees.length + (root.current.event_attendees.length === 1 ? " convidado" : " convidados") : "") : "Sem evento da agenda vinculado"; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
        LibraryButton { objectName: "libraryLinkEvent"; text: root.linking ? "Cancelar" : root.current && root.current.event_linked ? "Trocar evento…" : "Vincular a evento…"; enabled: !linkProcess.running; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; onClicked: root.linking ? root.cancelLink() : root.openLink() }
      }
      ColumnLayout {
        visible: root.linking && root.current !== null
        Layout.fillWidth: true
        spacing: Style.space(6)
        Text { Layout.fillWidth: true; text: dayEventsProcess.running ? "Buscando as reuniões do dia nas suas agendas…" : root.linkNotice ? root.linkNotice : root.dayEvents.length ? "Escolha o evento desta gravação (" + LibraryLogic.isoDate(root.current ? root.current.when : "") + "). Título, convidados e link passam a ser os do evento; o resumo já escrito é mantido." : "Nenhuma reunião com hora nesse dia nas suas agendas."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
        OmarchyUi.Dropdown {
          objectName: "libraryLinkTarget"
          Layout.fillWidth: true
          visible: root.dayEvents.length > 0
          showLabel: false
          options: [{value: "", label: "Escolha o evento…"}].concat(root.dayEvents.map(function(e) { return {value: String(e.uid), label: LibraryLogic.eventLabel(e)} }))
          value: root.linkTarget
          foreground: root.foreground; background: root.background; accent: root.accent; fontFamily: root.readingFontFamily
          onChanged: function(value) { root.linkTarget = String(value) }
        }
        RowLayout {
          visible: root.dayEvents.length > 0
          LibraryButton { objectName: "libraryConfirmLink"; text: linkProcess.running ? "Vinculando…" : "Vincular"; enabled: !!root.linkTarget && !linkProcess.running; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: root.confirmLink() }
        }
      }
      LibraryShare {
        objectName: "libraryShare"
        Layout.fillWidth: true
        agents: root.agents
        meetingSlug: root.current ? root.selectedSlug : ""
        hasNotes: root.current ? root.current.has_summary === true : false
        cliCommand: root.cliCommand
        foreground: root.foreground; background: root.background; accent: root.accent; fontFamily: root.readingFontFamily
        onChanged: root.refreshAgents()
      }
      Text { objectName: "libraryLinkFeedback"; Layout.fillWidth: true; visible: !!root.linkFeedback; text: root.linkFeedback; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
      Text { Layout.fillWidth: true; visible: root.current && root.current.warnings.length > 0; text: root.current ? root.current.warnings.join("\n") : ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
      RowLayout {
        visible: root.current !== null
        Layout.fillWidth: true
        spacing: Style.space(5)
        Flow {
          Layout.fillWidth: true
          Layout.preferredHeight: childrenRect.height
          spacing: Style.space(5)
        Repeater {
          model: ["Resumo", "Transcrição", "Áudio", "Minhas notas"]
          LibraryButton {
            required property string modelData
            required property int index
            text: modelData; selected: root.currentTab === index
            foreground: root.foreground; accent: root.accent
            font.family: root.readingFontFamily; font.pixelSize: Style.font.title
            onClicked: root.currentTab = index
          }
        }
        }
        LibraryButton { visible: root.currentTab !== 2; text: root.copyFeedback || "Copiar texto"; enabled: root.currentCopyText !== ""; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; onClicked: root.copyText(root.currentCopyText) }
      }
      StackLayout {
        id: tabs
        visible: root.current !== null
        Layout.fillWidth: true
        Layout.fillHeight: true
        currentIndex: root.currentTab
        ScrollView {
          id: summaryScroll
          clip: true
          contentWidth: availableWidth
          Column {
            width: summaryScroll.availableWidth
            spacing: Style.space(24)
            Repeater {
              model: root.summaryDocument.blocks.length ? root.summaryDocument.blocks : [{level: 0, text: "O resumo ainda não está disponível."}]
              TextArea {
                required property var modelData
                objectName: "librarySummaryBlock"
                width: parent.width
                padding: 0
                text: modelData.text
                textFormat: TextEdit.PlainText
                wrapMode: TextEdit.Wrap
                readOnly: true; selectByMouse: true
                color: root.foreground; selectionColor: root.accent
                font.family: root.readingFontFamily
                font.pixelSize: modelData.level === 0 ? Math.round(Style.font.body * 4 / 3) : modelData.level <= 2 ? Style.font.display : Style.font.heading
                font.weight: modelData.level === 0 ? Font.Normal : Font.DemiBold
                background: null
              }
            }
            Text { visible: root.extraDecisions.length > 0; text: "Decisões extraídas"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.display; font.weight: Font.DemiBold }
            Repeater {
              model: root.extraDecisions
              TextArea { required property string modelData; width: parent.width; text: "• " + modelData; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Math.round(Style.font.body * 1.25); background: null }
            }
            Text { visible: root.extraActions.length > 0; text: "Ações extraídas"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.display; font.weight: Font.DemiBold }
            Repeater {
              model: root.extraActions
              TextArea { required property string modelData; width: parent.width; text: "□ " + modelData; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Math.round(Style.font.body * 1.25); background: null }
            }
          }
        }
        ColumnLayout {
          spacing: Style.space(12)
          // Quem falou e quanto da conversa foi de cada um. Fala sem dono entra com o rótulo que tem, não some da conta.
          ColumnLayout {
            objectName: "libraryTalkShares"
            visible: root.talkShares.length > 1
            Layout.fillWidth: true
            spacing: Style.space(4)
            Text { text: "Participação na conversa"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall }
            Repeater {
              model: root.talkShares
              RowLayout {
                id: shareRow
                required property var modelData
                Layout.fillWidth: true
                spacing: Style.space(10)
                Text { Layout.preferredWidth: Style.space(250); text: shareRow.modelData.name; textFormat: Text.PlainText; elide: Text.ElideRight; color: LibraryLogic.genericSpeaker(shareRow.modelData.name) ? root.muted : root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
                Rectangle {
                  Layout.fillWidth: true; Layout.preferredHeight: Style.space(6)
                  radius: height / 2; color: root.faint
                  Rectangle { width: parent.width * shareRow.modelData.share; height: parent.height; radius: parent.radius; color: LibraryLogic.genericSpeaker(shareRow.modelData.name) ? root.muted : root.accent }
                }
                Text { Layout.preferredWidth: Style.space(44); horizontalAlignment: Text.AlignRight; text: Math.round(shareRow.modelData.share * 100) + "%"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
              }
            }
          }
          Text { Layout.fillWidth: true; visible: !!root.current && root.current.segments.length > 0 && !root.namedSpeakers; text: "Canais de áudio não identificam participantes. Os tempos pertencem à gravação indicada."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
          RowLayout {
            visible: !!root.current && root.current.segments.length > 0
            Layout.fillWidth: true
            spacing: Style.space(5)
            TextField {
              id: transcriptSearch
              objectName: "libraryTranscriptSearch"
              Layout.fillWidth: true
              implicitHeight: 36
              enabled: !root.showRawTranscript
              placeholderText: "Buscar na transcrição"
              color: root.foreground; placeholderTextColor: root.muted
              font.family: root.readingFontFamily; font.pixelSize: Style.font.body
              selectByMouse: true
              onTextChanged: root.transcriptQuery = text
              background: OmarchyUi.BorderSurface { color: Style.controlFill(transcriptSearch.activeFocus, transcriptSearch.hovered, root.foreground, root.accent); radius: Style.cornerRadius; borderSpec: Border.controlSpec(transcriptSearch.activeFocus ? "focus" : "normal", root.foreground, root.accent) }
            }
            Text { visible: root.transcriptQuery.trim() !== "" && !root.showRawTranscript; text: root.transcriptTurns.length + (root.transcriptTurns.length === 1 ? " fala" : " falas"); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
            LibraryButton { objectName: "libraryTimedTranscript"; text: "Por fala"; selected: !root.showRawTranscript; foreground: root.foreground; accent: root.accent; font.family: root.readingFontFamily; onClicked: root.showRawTranscript = false }
            LibraryButton { objectName: "libraryFullTranscript"; text: "Texto completo"; selected: root.showRawTranscript; foreground: root.foreground; accent: root.accent; font.family: root.readingFontFamily; onClicked: root.showRawTranscript = true }
          }
          ListView {
            id: segmentsList
            objectName: "librarySegments"
            visible: root.current && root.current.segments.length > 0 && !root.showRawTranscript
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: Style.space(18)
            model: root.transcriptTurns
            ScrollBar.vertical: ScrollBar {}
            delegate: Column {
              id: segmentRow
              required property var modelData
              width: segmentsList.width - Style.space(12)
              spacing: Style.space(4)
              RowLayout {
                width: parent.width
                spacing: Style.space(8)
                Text { text: segmentRow.modelData.channel; textFormat: Text.PlainText; elide: Text.ElideRight; Layout.maximumWidth: parent.width - Style.space(140); color: LibraryLogic.genericSpeaker(segmentRow.modelData.channel) ? root.muted : root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; font.weight: Font.DemiBold }
                LibraryButton { objectName: "librarySegmentTime"; visible: segmentRow.modelData.start !== null; text: LibraryLogic.clock(segmentRow.modelData.start); enabled: segmentRow.modelData.can_seek; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall; implicitHeight: Style.space(24); onClicked: root.seekSegment(segmentRow.modelData) }
                Text { visible: !segmentRow.modelData.can_seek || root.audioRecords.length > 1; text: segmentRow.modelData.can_seek ? "gravação " + (segmentRow.modelData.audio_index + 1) : "sem vínculo para reprodução"; textFormat: Text.PlainText; elide: Text.ElideRight; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.bodySmall }
                Item { Layout.fillWidth: true }
              }
              TextArea { width: parent.width; text: segmentRow.modelData.text; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; selectionColor: root.accent; font.family: root.readingFontFamily; font.pixelSize: Math.round(Style.font.body * 4 / 3); background: null }
            }
            Text { anchors.centerIn: parent; visible: root.transcriptQuery.trim() !== "" && root.transcriptTurns.length === 0; text: "Nada encontrado na transcrição."; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.title }
          }
          ScrollView {
            visible: !root.current || root.current.segments.length === 0 || root.showRawTranscript
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true
            TextArea { objectName: "libraryRawTranscript"; text: root.current ? root.current.transcript || "Transcrição ainda não disponível." : ""; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Math.round(Style.font.body * 4 / 3); background: null }
          }
        }
        ScrollView {
          id: audioScroll
          clip: true
          contentWidth: availableWidth
        ColumnLayout {
          width: audioScroll.availableWidth
          spacing: Style.space(12)
          Text { Layout.fillWidth: true; text: "Gravações da reunião"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.display; font.weight: Font.DemiBold }
          Text { Layout.fillWidth: true; text: root.audioRecords.length ? "A reprodução usa o arquivo local. Nada é enviado para outro serviço." : "Esta reunião está sem gravações válidas. Você pode manter suas anotações; não há áudio para reprocessar."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.title }
          RecordingActions {
            id: recordingActions
            objectName: "libraryRecordingActions"
            Layout.fillWidth: true
            meetingSlug: root.selectedSlug
            recordings: root.audioRecords
            revision: root.current ? root.current.recording_revision || 0 : 0
            contentStatus: root.current ? root.current.content_status || "" : ""
            restoreAllowed: root.current ? root.current.can_restore === true : false
            lastExclusionId: root.current ? root.current.exclusion_id || "" : ""
            remoteCleanupRequired: root.current ? root.current.remote_cleanup_required === true : false
            canReprocess: root.current ? root.current.can_reprocess === undefined ? root.audioRecords.length > 0 : root.current.can_reprocess : false
            cliCommand: root.cliCommand
            moveTargets: LibraryLogic.moveTargets(root.meetings, root.selectedSlug, root.current ? root.current.when : "", 20)
            moveEnabled: root.current ? root.current.move_enabled === true : false
            foreground: root.foreground; accent: root.accent; fontFamily: root.readingFontFamily
            onListen: function(index) { root.loadAudio(index, 0, true) }
            onBeforeMutation: root.prepareRecordingMutation(root.selectedSlug)
            onMeetingChanged: function(slug) { root.finishRecordingMutation(slug) }
          }
          Text { Layout.fillWidth: true; text: root.audioError; visible: root.audioError !== ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.title }
          RowLayout {
            Layout.fillWidth: true
            LibraryButton { objectName: "libraryPlay"; text: player.playbackState === MediaPlayer.PlayingState ? "Pausar" : "Reproduzir"; enabled: root.audioIndex >= 0; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: player.playbackState === MediaPlayer.PlayingState ? player.pause() : player.play() }
            Item { Layout.fillWidth: true }
            Text { text: "Velocidade"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
            OmarchyUi.Dropdown {
              objectName: "librarySpeed"
              implicitWidth: Style.space(115)
              showLabel: false
              options: [{value: "0.75", label: "0,75×"}, {value: "1", label: "1×"}, {value: "1.25", label: "1,25×"}, {value: "1.5", label: "1,5×"}, {value: "2", label: "2×"}]
              value: String(player.playbackRate)
              foreground: root.foreground; background: root.background; accent: root.accent; fontFamily: root.readingFontFamily
              onChanged: function(value) { var rate = Number(value); if ([0.75, 1, 1.25, 1.5, 2].indexOf(rate) >= 0) player.playbackRate = rate }
            }
          }
          Basic.Slider {
            id: seekSlider
            objectName: "librarySeek"
            Layout.fillWidth: true
            from: 0; to: Math.max(1, player.duration)
            value: player.position
            enabled: player.seekable
            onMoved: player.position = value
            stepSize: 5000
            background: Rectangle {
              x: seekSlider.leftPadding
              y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
              width: seekSlider.availableWidth
              height: Style.space(4)
              radius: Math.min(Style.cornerRadius, height / 2)
              color: Style.selectedFillFor(root.foreground, root.accent)
              Rectangle { width: seekSlider.visualPosition * parent.width; height: parent.height; radius: parent.radius; color: root.accent }
            }
            handle: OmarchyUi.BorderSurface {
              x: seekSlider.leftPadding + seekSlider.visualPosition * (seekSlider.availableWidth - width)
              y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
              implicitWidth: Style.space(14); implicitHeight: Style.space(14)
              radius: Math.min(Style.cornerRadius, width / 2)
              color: root.foreground
              borderSpec: Border.controlSpec(seekSlider.activeFocus ? "focus" : "normal", root.foreground, root.accent)
            }
          }
          RowLayout {
            Layout.fillWidth: true
            Text { text: LibraryLogic.clock(player.position / 1000); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
            Item { Layout.fillWidth: true }
            Text { text: LibraryLogic.clock(player.duration / 1000); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
          }
        }
        }
        MeetingNotes {
          id: notesEditor
          objectName: "libraryMeetingNotes"
          meetingSlug: root.selectedSlug
          cliCommand: root.cliCommand
          foreground: root.foreground; accent: root.accent; fontFamily: root.readingFontFamily
          onSummaryUpdated: function(slug) { if (slug === root.selectedSlug) root.startDetail(); root.refreshHistory() }
        }
      }
      RowLayout {
        objectName: "libraryMiniPlayer"
        visible: root.current !== null && root.audioIndex >= 0 && root.currentTab !== 2
        Layout.fillWidth: true
        spacing: Style.space(10)
        LibraryButton { text: player.playbackState === MediaPlayer.PlayingState ? "Pausar" : "Reproduzir"; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; onClicked: player.playbackState === MediaPlayer.PlayingState ? player.pause() : player.play() }
        Text { Layout.fillWidth: true; text: LibraryLogic.clock(player.position / 1000) + " de " + LibraryLogic.clock(player.duration / 1000) + (root.audioRecords.length > 1 ? " · gravação " + (root.audioIndex + 1) : ""); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Style.font.body }
        LibraryButton { text: "Abrir áudio"; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: Style.font.body; onClicked: root.currentTab = 2 }
      }
      Item { Layout.fillHeight: true; visible: root.current === null }
      Text { visible: root.current === null && !root.detailLoading; Layout.fillWidth: true; text: "Escolha uma reunião no histórico para ler as notas, consultar a transcrição ou ouvir o áudio."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: Math.round(Style.font.body * 4 / 3) }
      Item { Layout.fillHeight: true; visible: root.current === null }
    }
  }
}
}
