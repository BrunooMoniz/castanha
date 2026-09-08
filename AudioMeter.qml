import QtQuick
import "AudioMeter.js" as AudioMeterLogic

QtObject {
  id: root

  property bool active: false
  property real peak: 0
  property var samples: [0, 0, 0, 0, 0]
  readonly property string text: active ? AudioMeterLogic.render(samples) : ""

  function sample() {
    if (!active) return
    samples = AudioMeterLogic.pushSample(samples, peak)
  }

  function reset() {
    samples = [0, 0, 0, 0, 0]
  }

  onActiveChanged: if (!active) reset()

  property Timer sampleTimer: Timer {
    interval: 120
    repeat: true
    running: root.active
    onTriggered: root.sample()
  }
}
