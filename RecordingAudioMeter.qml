import QtQuick
import "AudioMeter.js" as AudioMeterLogic

QtObject {
  id: root

  property bool recording: false
  property string mode: "dual"
  property real audioPeak: 0
  property bool micMuted: false

  readonly property real peak: recording ? AudioMeterLogic.clampPeak(audioPeak) : 0
  readonly property string text: meter.text
  // O QML não abre clientes de áudio. O daemon/FFmpeg faz a medição e apenas
  // projeta o valor pronto no estado, portanto não há monitor PipeWire aqui.
  readonly property bool micMonitoring: false
  readonly property bool systemMonitoring: false

  property AudioMeter meter: AudioMeter {
    active: root.recording
    peak: root.peak
  }

  onModeChanged: meter.reset()
}
