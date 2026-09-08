import QtQuick
import Quickshell
import "." as Castanha

ShellRoot {
  id: root

  property bool sawSignal: false

  function finish(ok) {
    console.info(ok ? "CASTANHA_PIPEWIRE_PEAK_OK" : "CASTANHA_PIPEWIRE_PEAK_FAIL")
    Qt.quit()
  }

  Castanha.RecordingAudioMeter {
    id: recordingMeter
    recording: true
    mode: "dual"
    audioPeak: 0.85

    onTextChanged: {
      if (!root.sawSignal
          && recordingMeter.text !== ""
          && recordingMeter.text !== "▁▁▁▁▁") {
        root.sawSignal = true
        recordingMeter.audioPeak = 0
        Qt.callLater(function() {
          recordingMeter.recording = false
          Qt.callLater(function() {
            root.finish(recordingMeter.text === ""
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
