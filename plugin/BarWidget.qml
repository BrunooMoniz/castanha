import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "moniz.castanha"

  property var stateData: ({
    "status": "idle",
    "elapsed_seconds": 0,
    "mode": "dual",
    "next_meeting": null,
    "current_meeting": null
  })

  readonly property string status: stateData.status || "idle"
  readonly property int elapsedSeconds: stateData.elapsed_seconds || 0
  readonly property bool isRecording: status === "recording"
  readonly property bool isPaused: status === "paused"
  readonly property bool isProcessing: status === "processing"

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
    if (stateData.next_meeting && stateData.next_meeting.title) {
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
        root.togglePanel()
      }
    }
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) {
      panelLoader.item.toggle()
    }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      if (item) {
        if ("bar" in item) item.bar = root.bar
        if ("anchorItem" in item) item.anchorItem = button
        if ("hostWidget" in item) item.hostWidget = root
      }
    }
  }
}
