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

  property var stateData: hostWidget ? hostWidget.stateData : ({})
  readonly property string status: stateData.status || "idle"
  readonly property bool isRecording: status === "recording"
  readonly property bool isPaused: status === "paused"
  readonly property bool isProcessing: status === "processing"

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.space(16)
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
        color: root.isRecording ? "#ff5555" : (root.isPaused ? "#ffb86c" : Style.selectedFillFor(Color.foreground, Color.accent))
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

    // Ações Principais
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Button {
        Layout.fillWidth: true
        text: root.isRecording ? "⏹ Finalizar Reunião" : (root.isProcessing ? "⏳ Processando..." : "● Iniciar Gravação")
        enabled: !root.isProcessing
        onClicked: {
          if (root.isRecording || root.isPaused) {
            root.bar.run("castanha stop")
          } else {
            root.bar.run("castanha start")
          }
          if (root.close) root.close()
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

    // Card Próxima Reunião / Reunião Atual
    Rectangle {
      Layout.fillWidth: true
      radius: Style.cornerRadius
      color: Style.alpha(root.bar ? root.bar.foreground : Color.foreground, 0.06)
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
          color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.4)
        }

        Text {
          text: {
            var m = root.isRecording ? root.stateData.current_meeting : root.stateData.next_meeting
            return m && m.title ? m.title : "Nenhuma reunião agendada no momento"
          }
          font.pixelSize: Style.font.body
          font.bold: true
          wrapMode: Text.WordWrap
          Layout.fillWidth: true
          color: root.bar ? root.bar.foreground : Color.foreground
        }

        Button {
          Layout.fillWidth: true
          visible: {
            var m = root.isRecording ? root.stateData.current_meeting : root.stateData.next_meeting
            return !!(m && m.conference_url)
          }
          text: "🔗 Abrir Chamada (Meet/Teams)"
          onClicked: {
            var m = root.isRecording ? root.stateData.current_meeting : root.stateData.next_meeting
            if (m && m.conference_url) {
              root.bar.run("xdg-open '" + m.conference_url + "'")
            }
          }
        }
      }
    }

    // Botões de Atalho
    RowLayout {
      Layout.fillWidth: true
      Button {
        Layout.fillWidth: true
        text: "📁 Abrir Pasta de Notas"
        onClicked: {
          root.bar.run("castanha notes --open")
          if (root.close) root.close()
        }
      }
    }
  }
}
