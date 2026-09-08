import QtQuick
import QtTest
import "../.." as Castanha
import "../../AudioMeter.js" as AudioMeterLogic

TestCase {
  id: testCase
  name: "AudioMeter"

  Component {
    id: meterComponent
    Castanha.AudioMeter {}
  }

  function test_clamp_and_levels() {
    compare(AudioMeterLogic.clampPeak(-1), 0)
    compare(AudioMeterLogic.clampPeak(NaN), 0)
    compare(AudioMeterLogic.clampPeak(Infinity), 0)
    compare(AudioMeterLogic.clampPeak(0.25), 0.25)
    compare(AudioMeterLogic.clampPeak(2), 1)
  }

  function test_expired_state_peak_is_not_rendered() {
    var now = 1700000000000
    compare(AudioMeterLogic.isFresh(1700000000, now, true), true)
    compare(AudioMeterLogic.isFresh(1699999998.51, now, true), true)
    compare(AudioMeterLogic.isFresh(1699999998.49, now + 20, true), false)
    compare(AudioMeterLogic.isFresh(1700000000, now, false), false)
  }

  function test_quickshell_peak_levels() {
    var levels = "▁▂▃▄▅▆▇█"
    for (var i = 0; i < levels.length; ++i) {
      compare(AudioMeterLogic.levelChar(i / (levels.length - 1)), levels.charAt(i))
    }
    // PwNodePeakMonitor exposes the cube root of PCM peak. Even -90 dB PCM
    // therefore reaches about 0.0316 here and must still render as silence.
    compare(AudioMeterLogic.levelChar(Math.pow(10, -90 / 60)), "▁")
    compare(AudioMeterLogic.levelChar(0), "▁")
    compare(AudioMeterLogic.levelChar(2), "█")
  }

  function test_combines_channels_by_mode_and_mute() {
    compare(AudioMeterLogic.combinedPeak(0.4, 0.8, "mic_only", false), 0.4)
    compare(AudioMeterLogic.combinedPeak(0.4, 0.8, "dual", false), 0.8)
    compare(AudioMeterLogic.combinedPeak(0.9, 0.3, "dual", false), 0.9)
    compare(AudioMeterLogic.combinedPeak(0.9, 0.3, "mic_only", true), 0)
    compare(AudioMeterLogic.combinedPeak(0.9, 0.3, "dual", true), 0.3)
  }

  function test_fixed_history_and_rendering() {
    var original = [0.1, 0.2]
    var samples = AudioMeterLogic.pushSample(original, 1)
    compare(original.length, 2)
    compare(samples.length, 5)
    compare(AudioMeterLogic.render(samples), "▁▁▂▂█")
    for (var i = 0; i < 5; ++i) samples = AudioMeterLogic.pushSample(samples, 0)
    compare(AudioMeterLogic.render(samples), "▁▁▁▁▁")
    compare(AudioMeterLogic.render([0, 0.01, 0.1, 0.5, 1]).length, 5)
    compare(AudioMeterLogic.render(null), "▁▁▁▁▁")
  }

  function test_component_lifecycle() {
    var meter = createTemporaryObject(meterComponent, testCase, { active: false, peak: 1 })
    verify(meter !== null)
    compare(meter.text, "")
    compare(meter.sampleTimer.interval, 120)
    meter.sample()
    compare(AudioMeterLogic.render(meter.samples), "▁▁▁▁▁")

    meter.active = true
    compare(meter.text, "▁▁▁▁▁")
    meter.sample()
    compare(meter.text, "▁▁▁▁█")

    meter.active = false
    compare(meter.text, "")
    compare(AudioMeterLogic.render(meter.samples), "▁▁▁▁▁")
    meter.active = true
    compare(meter.text, "▁▁▁▁▁")
  }
}
