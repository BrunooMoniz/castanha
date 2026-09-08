import QtQuick
import Quickshell.Services.Pipewire
import "AudioMeter.js" as AudioMeterLogic

QtObject {
  id: root

  property bool recording: false
  property string mode: "dual"
  property real audioPeak: 0
  property var micSource: null
  property bool micMuted: false

  readonly property real peak: recording ? AudioMeterLogic.clampPeak(audioPeak) : 0
  readonly property string text: meter.text
  // O QML não abre clientes de áudio. O daemon/FFmpeg faz a medição e apenas
  // projeta o valor pronto no estado, portanto não há monitor PipeWire aqui.
  readonly property bool micMonitoring: false
  readonly property bool systemMonitoring: false

  // O estado mudo só é confiável quando o objeto PipeWire está vinculado ao
  // grafo. Não medimos pico aqui, mas mantemos o tracker para o diagnóstico do
  // microfone físico.
  property PwObjectTracker micTracker: PwObjectTracker {
    objects: root.micSource ? [root.micSource] : []
  }

  property AudioMeter meter: AudioMeter {
    active: root.recording
    peak: root.peak
  }

  onModeChanged: meter.reset()
}
