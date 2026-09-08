import QtQuick
import Quickshell.Services.Pipewire
import "AudioMeter.js" as AudioMeterLogic

QtObject {
  id: root

  property bool recording: false
  property string mode: "dual"
  property var micSource: null
  property var systemSink: null
  property bool micMuted: false

  readonly property real peak: AudioMeterLogic.combinedPeak(
    micPeakMonitor.peak, systemPeakMonitor.peak, mode, micMuted)
  readonly property string text: meter.text
  readonly property bool micMonitoring: micPeakMonitor.enabled
  readonly property bool systemMonitoring: systemPeakMonitor.enabled

  property PwObjectTracker nodeTracker: PwObjectTracker {
    objects: {
      var tracked = []
      if (root.micSource) tracked.push(root.micSource)
      if (root.systemSink) tracked.push(root.systemSink)
      return tracked
    }
  }

  property PwNodePeakMonitor micPeakMonitor: PwNodePeakMonitor {
    node: root.micSource
    enabled: root.recording && !!root.micSource
  }

  property PwNodePeakMonitor systemPeakMonitor: PwNodePeakMonitor {
    node: root.systemSink
    enabled: root.recording && root.mode === "dual" && !!root.systemSink
  }

  property AudioMeter meter: AudioMeter {
    active: root.recording
    peak: root.peak
  }

  onModeChanged: meter.reset()
  onMicSourceChanged: meter.reset()
  onSystemSinkChanged: meter.reset()
}
