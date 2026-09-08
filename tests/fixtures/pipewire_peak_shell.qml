import QtQuick
import Quickshell
import Quickshell.Services.Pipewire
import "." as Castanha

ShellRoot {
  id: root

  readonly property string sinkName: Quickshell.env("CASTANHA_TEST_SINK")
  readonly property var nodes: Pipewire.nodes ? Pipewire.nodes.values : []
  readonly property var targetSink: {
    for (var i = 0; i < nodes.length; ++i) {
      var node = nodes[i]
      if (node && node.isSink && !node.isStream && node.name === sinkName)
        return node
    }
    return null
  }
  property bool sawSignal: false

  function finish(ok) {
    console.info(ok ? "CASTANHA_PIPEWIRE_PEAK_OK" : "CASTANHA_PIPEWIRE_PEAK_FAIL")
    Qt.quit()
  }

  Castanha.RecordingAudioMeter {
    id: recordingMeter
    recording: true
    mode: "dual"
    micSource: null
    systemSink: root.targetSink

    onTextChanged: {
      if (!root.sawSignal
          && recordingMeter.text !== ""
          && recordingMeter.text !== "▁▁▁▁▁") {
        root.sawSignal = true
        recordingMeter.mode = "mic_only"
        Qt.callLater(function() {
          var modeStoppedSink = recordingMeter.text === "▁▁▁▁▁"
                                && !recordingMeter.micMonitoring
                                && !recordingMeter.systemMonitoring
          recordingMeter.recording = false
          Qt.callLater(function() {
            root.finish(modeStoppedSink
                        && recordingMeter.text === ""
                        && !recordingMeter.micMonitoring
                        && !recordingMeter.systemMonitoring)
          })
        })
      }
    }
  }

  Timer {
    interval: 7000
    running: true
    onTriggered: root.finish(false)
  }
}
