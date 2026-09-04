import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "moniz.castanha"
  ipcTarget: "castanha"

  property var stateData: ({
    "status": "idle",
    "elapsed_seconds": 0,
    "mode": "dual",
    "next_meeting": null,
    "current_meeting": null
  })

  readonly property string status: (stateData && stateData.status) ? stateData.status : "idle"
  readonly property int elapsedSeconds: (stateData && stateData.elapsed_seconds) ? stateData.elapsed_seconds : 0
  readonly property bool isRecording: status === "recording"
  readonly property bool isPaused: status === "paused"
  readonly property bool isProcessing: status === "processing"

  readonly property var activeMeeting: {
    if (!stateData) return null
    return isRecording ? stateData.current_meeting : stateData.next_meeting
  }

  function formatTime(sec) {
    var m = Math.floor(sec / 60)
    var s = sec % 60
    var h = Math.floor(m / 60)
    m = m % 60
    if (h > 0) {
      return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
    }
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
  }

  readonly property string barLabel: {
    if (isRecording) return "󰻃 REC " + formatTime(elapsedSeconds)
    if (isPaused) return "⏸ " + formatTime(elapsedSeconds)
    if (isProcessing) return "⏳ SALVANDO..."
    if (stateData && stateData.next_meeting && stateData.next_meeting.title) {
      var t = stateData.next_meeting.title
      return "󰍬 " + (t.length > 15 ? t.substring(0, 14) + "…" : t)
    }
    return "󰍬 REC"
  }

  FileView {
    id: stateFile
    path: Quickshell.env("HOME") + "/.local/state/castanha/state.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var txt = text()
        if (txt) root.stateData = JSON.parse(txt)
      } catch (e) {}
    }
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: stateFile.reload()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Botão na barra
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barLabel
    horizontalMargin: 8
    active: root.isRecording || root.isPaused
    activeColor: root.isRecording ? (root.bar ? root.bar.urgent : "#ff5555") : "#ffb86c"

    onPressed: function(btn) {
      if (!root.bar) return
      if (btn === Qt.RightButton) {
        root.bar.run("castanha toggle")
      } else {
        root.toggle()
      }
    }
  }

  // Painel popout sob o botão
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight + Style.space(24))

    ColumnLayout {
      id: contentColumn
      width: parent.width
      spacing: Style.space(12)

      // Cabeçalho
      RowLayout {
        Layout.fillWidth: true
        Text {
          text: "Castanha 🌰"
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.title
          font.bold: true
          color: root.bar ? root.bar.foreground : Color.foreground
        }
        Item { Layout.fillWidth: true }
        Rectangle {
          radius: Style.cornerRadius
          color: root.isRecording ? "#ff5555" : (root.isPaused ? "#ffb86c" : (root.bar ? root.bar.urgent : "#6272a4"))
          implicitWidth: statusText.implicitWidth + Style.space(12)
          implicitHeight: statusText.implicitHeight + Style.space(6)
          Text {
            id: statusText
            anchors.centerIn: parent
            text: root.isRecording ? "GRAVANDO" : (root.isPaused ? "PAUSADO" : (root.isProcessing ? "PROCESSANDO" : "OCIOSO"))
            font.pixelSize: Style.font.small
            font.bold: true
            color: "#ffffff"
          }
        }
      }

      // Ações
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Button {
          Layout.fillWidth: true
          text: root.isRecording ? "⏹ Finalizar e Salvar" : (root.isProcessing ? "⏳ Processando..." : "● Iniciar Gravação")
          enabled: !root.isProcessing
          onClicked: {
            if (root.isRecording || root.isPaused) {
              root.bar.run("castanha stop")
            } else {
              root.bar.run("castanha start")
            }
            root.close()
          }
        }

        Button {
          visible: root.isRecording || root.isPaused
          text: root.isPaused ? "▶ Retomar" : "⏸ Pausar"
          onClicked: {
            if (root.isPaused) {
              root.bar.run("castanha resume")
            } else {
              root.bar.run("castanha pause")
            }
          }
        }
      }

      // Card Reunião
      Rectangle {
        Layout.fillWidth: true
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.06)
        implicitHeight: meetingCol.implicitHeight + Style.space(16)

        ColumnLayout {
          id: meetingCol
          anchors.fill: parent
          anchors.margins: Style.space(10)
          spacing: Style.space(6)

          Text {
            text: root.isRecording ? "REUNIÃO ATUAL" : "PRÓXIMA REUNIÃO"
            font.pixelSize: Style.font.small
            font.bold: true
            color: root.bar ? root.bar.dim : Color.dim
          }

          Text {
            text: (root.activeMeeting && root.activeMeeting.title) ? root.activeMeeting.title : "Nenhuma reunião no momento"
            font.pixelSize: Style.font.body
            font.bold: true
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            color: root.bar ? root.bar.foreground : Color.foreground
          }

          Button {
            Layout.fillWidth: true
            visible: !!(root.activeMeeting && root.activeMeeting.conference_url)
            text: "🔗 Entrar na Chamada (Meet/Teams)"
            onClicked: {
              if (root.activeMeeting && root.activeMeeting.conference_url) {
                root.bar.run("xdg-open '" + root.activeMeeting.conference_url + "'")
              }
            }
          }
        }
      }

      // Atalhos
      RowLayout {
        Layout.fillWidth: true
        Button {
          Layout.fillWidth: true
          text: "📁 Abrir Notas (Silver)"
          onClicked: {
            root.bar.run("castanha notes --open")
            root.close()
          }
        }
      }
    }
  }
}
