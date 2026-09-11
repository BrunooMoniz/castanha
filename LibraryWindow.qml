// Interface pública: showMeeting(slug), slug vazio abre o histórico.
// foreground/fontFamily podem ser herdados do Panel; background/accent e
// readingFontFamily são opcionais. Fechar interrompe apenas a reprodução.
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts
import QtMultimedia
import Quickshell
import Quickshell.Io
import qs.Commons
import "LibraryLogic.js" as LibraryLogic

FloatingWindow {
  id: root
  title: "Castanha · Biblioteca de reuniões"
  visible: false
  implicitWidth: 1100
  implicitHeight: 760
  minimumSize: Qt.size(800, 600)
  color: background
  property color foreground: Color.foreground
  property color background: Color.popups.background
  property color accent: Color.accent
  property string fontFamily: Style.font.family
  property string readingFontFamily: "sans-serif"
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
  property int audioIndex: -1
  property string audioError: ""
  property real pendingSeek: -1
  property bool playWhenReady: false
  readonly property color muted: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.66)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.07)
  readonly property var filteredMeetings: LibraryLogic.filtered(meetings, query, statusFilter)
  readonly property var current: detail && detail.slug === selectedSlug ? detail : null
  readonly property bool historyLoading: historyProcess.running
  readonly property bool detailLoading: detailProcess.running
  readonly property var audioRecords: current ? current.recordings || [] : []
  readonly property string currentCopyText: !current ? "" : currentTab === 1 ? current.transcript :
    [LibraryLogic.plainNotes(current.summary), current.decisions.length ? "Decisões\n" + current.decisions.join("\n") : "",
     current.action_items.length ? "Próximos passos\n" + current.action_items.join("\n") : ""].filter(function(x) { return x }).join("\n\n")

  function showMeeting(slug) {
    visible = true
    refreshHistory()
    if (slug) selectMeeting(String(slug))
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
    detailError = ""
    if (!detailProcess.running) startDetail()
  }
  function startDetail() {
    if (!selectedSlug) return
    detailProcess.requestedSlug = selectedSlug
    detailProcess.command = cliCommand.concat(["library", "--json", "--", selectedSlug])
    detailProcess.running = true
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
  onVisibleChanged: if (!visible) { player.stop(); playWhenReady = false }
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
        root.historyError = ""
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
    spacing: 0
    Rectangle {
      Layout.preferredWidth: root.width < 940 ? 260 : 300
      Layout.fillHeight: true
      color: root.faint
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 14
        Text { text: "CASTANHA"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12; font.letterSpacing: 2 }
        Text { text: "Suas reuniões"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 25; font.weight: Font.DemiBold }
        TextField {
          id: searchField
          objectName: "librarySearch"
          Layout.fillWidth: true
          implicitHeight: 42
          placeholderText: "Buscar por título ou data"
          color: root.foreground
          placeholderTextColor: root.muted
          font.family: root.readingFontFamily
          font.pixelSize: 14
          selectByMouse: true
          onTextChanged: root.query = text
          background: Rectangle { color: root.background; radius: 7; border.width: searchField.activeFocus ? 1 : 0; border.color: root.accent }
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: 3
          Repeater {
            model: [{label: "Todas", value: "all"}, {label: "Pendentes", value: "pending"}, {label: "Concluídas", value: "complete"}]
            LibraryButton {
              required property var modelData
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              text: modelData.label
              foreground: root.foreground; accent: root.accent
              selected: root.statusFilter === modelData.value
              font.family: root.readingFontFamily; font.pixelSize: 12
              onClicked: root.statusFilter = modelData.value
            }
          }
        }
        Text { Layout.fillWidth: true; visible: root.historyError !== ""; text: root.historyError; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 12 }
        RowLayout {
          Layout.fillWidth: true
          Text { Layout.fillWidth: true; text: root.historyLoading ? "Atualizando…" : root.filteredMeetings.length + " reuniões"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
          LibraryButton { text: "Atualizar"; enabled: !root.historyLoading; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 12; onClicked: root.refreshHistory() }
        }
        ListView {
          id: historyList
          objectName: "libraryHistory"
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          spacing: 5
          model: root.filteredMeetings
          activeFocusOnTab: true
          keyNavigationEnabled: true
          Keys.onReturnPressed: if (currentIndex >= 0 && currentIndex < count) root.selectMeeting(root.filteredMeetings[currentIndex].slug)
          Keys.onEnterPressed: if (currentIndex >= 0 && currentIndex < count) root.selectMeeting(root.filteredMeetings[currentIndex].slug)
          ScrollBar.vertical: ScrollBar {}
          delegate: Rectangle {
            id: meetingRow
            required property var modelData
            required property int index
            width: historyList.width
            height: rowContent.implicitHeight + 24
            radius: 8
            border.width: historyList.activeFocus && historyList.currentIndex === index ? 1 : 0
            border.color: root.accent
            color: root.selectedSlug === modelData.slug ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12) : rowMouse.containsMouse ? root.faint : "transparent"
            Column {
              id: rowContent
              anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
              anchors.margins: 12
              spacing: 6
              Text { width: parent.width; text: meetingRow.modelData.title; textFormat: Text.PlainText; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 15; font.weight: Font.DemiBold }
              Text { width: parent.width; text: LibraryLogic.date(meetingRow.modelData.when); textFormat: Text.PlainText; elide: Text.ElideRight; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 11 }
              Text { text: meetingRow.modelData.status_label + " · " + LibraryLogic.clock(meetingRow.modelData.duration_seconds); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
            }
            MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; onClicked: { historyList.currentIndex = meetingRow.index; historyList.forceActiveFocus(); root.selectMeeting(meetingRow.modelData.slug) } }
          }
          Text { anchors.centerIn: parent; width: parent.width - 20; visible: !root.historyLoading && root.filteredMeetings.length === 0; text: root.meetings.length ? "Nenhuma reunião encontrada.\nTente outro filtro." : "Suas gravações aparecerão aqui."; textFormat: Text.PlainText; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 14 }
        }
      }
    }
    Rectangle { Layout.preferredWidth: 1; Layout.fillHeight: true; color: root.faint }
    ColumnLayout {
      Layout.fillWidth: true
      Layout.fillHeight: true
      Layout.margins: root.width < 940 ? 22 : 32
      spacing: 18
      RowLayout {
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: root.current ? LibraryLogic.date(root.current.when) : "BIBLIOTECA LOCAL"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
        LibraryButton { text: "Fechar"; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: root.visible = false }
      }
      Text {
        Layout.fillWidth: true
        text: root.current ? root.current.title : root.detailLoading ? "Abrindo reunião…" : "Tudo o que foi conversado,\nno mesmo lugar."
        textFormat: Text.PlainText; wrapMode: Text.Wrap
        color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: root.width < 940 ? 25 : 31; font.weight: Font.DemiBold
      }
      Text { Layout.fillWidth: true; visible: root.detailError !== ""; text: root.detailError; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 14 }
      Text { Layout.fillWidth: true; visible: root.current && root.current.warnings.length > 0; text: root.current ? root.current.warnings.join("\n") : ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
      RowLayout {
        visible: root.current !== null
        Layout.fillWidth: true
        spacing: 5
        Repeater {
          model: ["Resumo", "Transcrição", "Áudio"]
          LibraryButton {
            required property string modelData
            required property int index
            text: modelData; selected: root.currentTab === index
            foreground: root.foreground; accent: root.accent
            font.family: root.readingFontFamily; font.pixelSize: 14
            onClicked: root.currentTab = index
          }
        }
        Item { Layout.fillWidth: true }
        LibraryButton { text: root.copyFeedback || "Copiar texto"; enabled: root.currentCopyText !== ""; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 12; onClicked: root.copyText(root.currentCopyText) }
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
            spacing: 24
            TextArea {
              objectName: "librarySummary"
              width: parent.width
              padding: 0
              text: root.current ? LibraryLogic.plainNotes(root.current.summary) || "O resumo ainda não está disponível. Sua gravação está preservada." : ""
              textFormat: TextEdit.PlainText
              wrapMode: TextEdit.Wrap
              readOnly: true; selectByMouse: true
              color: root.foreground; selectionColor: root.accent
              font.family: root.readingFontFamily; font.pixelSize: 16
              background: null
            }
            Text { visible: root.current && root.current.decisions.length > 0; text: "Decisões"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 20; font.weight: Font.DemiBold }
            Repeater {
              model: root.current ? root.current.decisions : []
              TextArea { required property string modelData; width: parent.width; text: "• " + modelData; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 15; background: null }
            }
            Text { visible: root.current && root.current.action_items.length > 0; text: "Próximos passos"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 20; font.weight: Font.DemiBold }
            Repeater {
              model: root.current ? root.current.action_items : []
              TextArea { required property string modelData; width: parent.width; text: "□ " + modelData; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 15; background: null }
            }
          }
        }
        ColumnLayout {
          spacing: 12
          Text { Layout.fillWidth: true; text: "Canais de áudio não identificam participantes. Os tempos pertencem à gravação indicada."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
          RowLayout {
            visible: root.current && root.current.segments.length > 0
            LibraryButton { objectName: "libraryTimedTranscript"; text: "Com tempos"; selected: !root.showRawTranscript; foreground: root.foreground; accent: root.accent; font.family: root.readingFontFamily; onClicked: root.showRawTranscript = false }
            LibraryButton { objectName: "libraryFullTranscript"; text: "Texto completo"; selected: root.showRawTranscript; foreground: root.foreground; accent: root.accent; font.family: root.readingFontFamily; onClicked: root.showRawTranscript = true }
          }
          ListView {
            id: segmentsList
            objectName: "librarySegments"
            visible: root.current && root.current.segments.length > 0 && !root.showRawTranscript
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: 16
            model: root.current ? root.current.segments : []
            ScrollBar.vertical: ScrollBar {}
            delegate: Column {
              id: segmentRow
              required property var modelData
              width: segmentsList.width
              spacing: 5
              RowLayout {
                width: parent.width
                LibraryButton { visible: segmentRow.modelData.start !== null; text: LibraryLogic.clock(segmentRow.modelData.start); enabled: segmentRow.modelData.can_seek; foreground: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 12; onClicked: root.seekSegment(segmentRow.modelData) }
                Text { Layout.fillWidth: true; text: segmentRow.modelData.channel + (segmentRow.modelData.can_seek ? " · gravação " + (segmentRow.modelData.audio_index + 1) : " · sem vínculo para reprodução"); textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 12 }
              }
              TextArea { width: parent.width; text: segmentRow.modelData.text; textFormat: TextEdit.PlainText; padding: 0; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 16; background: null }
            }
          }
          ScrollView {
            visible: !root.current || root.current.segments.length === 0 || root.showRawTranscript
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true
            TextArea { objectName: "libraryRawTranscript"; text: root.current ? root.current.transcript || "Transcrição ainda não disponível." : ""; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 16; background: null }
          }
        }
        ColumnLayout {
          spacing: 20
          Text { Layout.fillWidth: true; text: "Ouça a gravação original"; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 21; font.weight: Font.DemiBold }
          Text { Layout.fillWidth: true; text: root.audioRecords.length ? "A reprodução usa o arquivo local. Nada é enviado para outro serviço." : "Nenhum áudio local disponível para esta reunião."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 14 }
          Basic.ComboBox {
            id: recordingChoice
            objectName: "libraryAudioChoice"
            Layout.fillWidth: true
            model: root.audioRecords
            textRole: "name"
            currentIndex: root.audioIndex
            displayText: root.audioIndex < 0 ? "Escolher gravação" : root.audioRecords[root.audioIndex].name
            enabled: root.audioRecords.length > 0
            font.family: root.readingFontFamily
            palette.text: root.foreground; palette.buttonText: root.foreground; palette.button: root.faint; palette.base: root.background; palette.highlight: root.accent
            contentItem: Text { text: recordingChoice.displayText; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
            delegate: ItemDelegate {
              required property var modelData
              width: recordingChoice.width
              contentItem: Text { text: modelData.name; textFormat: Text.PlainText; color: root.foreground; font.family: root.readingFontFamily; elide: Text.ElideRight }
            }
            onActivated: function(index) { root.loadAudio(index, 0, false) }
          }
          Item { Layout.fillHeight: true }
          Text { Layout.fillWidth: true; text: root.audioError; visible: root.audioError !== ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.readingFontFamily; font.pixelSize: 14 }
          RowLayout {
            Layout.fillWidth: true
            LibraryButton { objectName: "libraryPlay"; text: player.playbackState === MediaPlayer.PlayingState ? "Pausar" : "Reproduzir"; enabled: root.audioIndex >= 0; foreground: root.foreground; font.family: root.readingFontFamily; onClicked: player.playbackState === MediaPlayer.PlayingState ? player.pause() : player.play() }
            Item { Layout.fillWidth: true }
            Text { text: "Velocidade"; textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
            Basic.ComboBox { objectName: "librarySpeed"; model: ["0,75×", "1×", "1,25×", "1,5×", "2×"]; currentIndex: 1; font.family: root.readingFontFamily; palette.text: root.foreground; palette.buttonText: root.foreground; palette.button: root.faint; palette.base: root.background; onActivated: function(index) { player.playbackRate = [0.75, 1, 1.25, 1.5, 2][index] } }
          }
          Basic.Slider {
            id: seekSlider
            objectName: "librarySeek"
            Layout.fillWidth: true
            from: 0; to: Math.max(1, player.duration)
            value: player.position
            enabled: player.seekable
            onMoved: player.position = value
            palette.highlight: root.accent; palette.button: root.foreground
          }
          RowLayout {
            Layout.fillWidth: true
            Text { text: LibraryLogic.clock(player.position / 1000); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
            Item { Layout.fillWidth: true }
            Text { text: LibraryLogic.clock(player.duration / 1000); textFormat: Text.PlainText; color: root.muted; font.family: root.readingFontFamily }
          }
          Item { Layout.fillHeight: true }
        }
      }
      Item { Layout.fillHeight: true; visible: root.current === null }
      Text { visible: root.current === null && !root.detailLoading; Layout.fillWidth: true; text: "Escolha uma reunião no histórico para ler as notas, consultar a transcrição ou ouvir o áudio."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.muted; font.family: root.readingFontFamily; font.pixelSize: 16 }
      Item { Layout.fillHeight: true; visible: root.current === null }
    }
  }
}
}
