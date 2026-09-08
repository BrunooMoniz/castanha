import QtQuick
import Quickshell
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Ui
import "." as Castanha

ShellRoot {
  id: root

  PwNodePeakMonitor {
    node: null
    enabled: false
  }

  PwNodePeakMonitor {
    node: null
    enabled: false
  }

  Castanha.AudioMeter {
    id: meter
    active: true
    peak: 1
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
    }
  }

  Timer {
    interval: 100
    running: true
    onTriggered: {
      meter.sample()
      var ok = meter.text === "▁▁▁▁█"
            && meter.text.length === 5
            && meterButton.fontFamily === Style.font.family
            && meterButton.labelWidth > baselineButton.labelWidth
            && meterButton.width >= meterButton.labelWidth + 2 * meterButton.scaledHorizontalMargin
      console.info(ok ? "CASTANHA_AUDIO_METER_OK" : "CASTANHA_AUDIO_METER_FAIL")
      Qt.quit()
    }
  }
}
