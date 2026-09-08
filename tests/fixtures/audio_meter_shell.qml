import QtQuick
import Quickshell
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Ui
import "." as Castanha

ShellRoot {
  id: root

  property int sampleTicks: 0
  property double silenceStartedMs: 0
  property bool firstSampleWasLoud: false
  property bool recordingLifecycleOk: false

  function widthsAreStable() {
    var minLabel = Infinity, maxLabel = 0
    var minButton = Infinity, maxButton = 0
    for (var i = 0; i < widthRepeater.count; ++i) {
      var button = widthRepeater.itemAt(i)
      minLabel = Math.min(minLabel, button.labelWidth)
      maxLabel = Math.max(maxLabel, button.labelWidth)
      minButton = Math.min(minButton, button.implicitWidth)
      maxButton = Math.max(maxButton, button.implicitWidth)
    }
    return maxLabel - minLabel <= 1 && maxButton - minButton <= 1
  }

  function finish(ok) {
    console.info(ok ? "CASTANHA_AUDIO_METER_OK" : "CASTANHA_AUDIO_METER_FAIL")
    Qt.quit()
  }

  Castanha.RecordingAudioMeter {
    id: recordingMeter
    recording: true
    mode: "dual"
    micSource: null
    systemSink: null
  }

  Castanha.AudioMeter {
    id: meter
    active: true
    peak: 1
  }

  Connections {
    target: meter
    function onSamplesChanged() {
      root.sampleTicks++
      if (root.sampleTicks === 1) {
        root.firstSampleWasLoud = meter.text === "▁▁▁▁█"
        root.recordingLifecycleOk = recordingMeter.text === "▁▁▁▁▁"
                                    && !recordingMeter.micMonitoring
                                    && !recordingMeter.systemMonitoring
        recordingMeter.recording = false
        root.recordingLifecycleOk = root.recordingLifecycleOk
                                    && recordingMeter.text === ""
                                    && !recordingMeter.micMonitoring
                                    && !recordingMeter.systemMonitoring
        root.silenceStartedMs = Date.now()
        meter.peak = 0
      } else if (root.sampleTicks === 6) {
        var silenceElapsed = Date.now() - root.silenceStartedMs
        root.finish(root.firstSampleWasLoud
                    && root.recordingLifecycleOk
                    && meter.text === "▁▁▁▁▁"
                    && silenceElapsed <= 1000
                    && root.widthsAreStable()
                    && meterButton.fontFamily === Style.font.family
                    && meterButton.labelWidth > baselineButton.labelWidth
                    && meterButton.width >= meterButton.labelWidth
                                         + 2 * meterButton.scaledHorizontalMargin)
      }
    }
  }

  FloatingWindow {
    visible: true
    implicitWidth: meterButton.implicitWidth
    implicitHeight: meterButton.implicitHeight

    Item {
      anchors.fill: parent

      WidgetButton {
        id: baselineButton
        text: "󰫂  00:14  "
        visible: false
      }

      WidgetButton {
        id: meterButton
        text: baselineButton.text + meter.text
        width: implicitWidth
        height: implicitHeight
      }

      Column {
        visible: false

        Repeater {
          id: widthRepeater
          model: ["▁▁▁▁▁", "▂▂▂▂▂", "▃▃▃▃▃", "▄▄▄▄▄",
                  "▅▅▅▅▅", "▆▆▆▆▆", "▇▇▇▇▇", "█████"]
          WidgetButton {
            required property string modelData
            text: modelData
          }
        }
      }
    }
  }

  Timer {
    interval: 1200
    running: true
    onTriggered: {
      root.finish(false)
    }
  }
}
