import QtQuick
import QtTest
import "../.."
import "../../AudioMeter.js" as Logic

TestCase {
    name: "SeparateWaveforms"
    ChannelWaveform { id: mic; width: 180; height: 36 }
    ChannelWaveform { id: call; width: 180; height: 36 }
    function cleanup() { mic.active = false; call.active = false; wait(200) }
    function test_channels_independent_and_reset() {
        mic.peak = Math.pow(10, -24 / 60); call.peak = 0
        mic.active = true; call.active = true
        wait(100)
        verify(findChild(mic, "levelBar13").height > 18)
        compare(findChild(call, "levelBar13").height, 2)
        mic.peak = 0
        call.peak = Math.pow(10, -18 / 60)
        wait(200)
        compare(findChild(mic, "levelBar13").height, 2)
        verify(findChild(call, "levelBar13").height > 24)
        call.active = false
        wait(200)
        compare(findChild(call, "levelBar13").height, 2)
    }
    function test_voice_has_clear_range_without_boosting_noise() {
        compare(Logic.visualLevel(Math.pow(10, -60 / 60)), 0)
        verify(Logic.visualLevel(Math.pow(10, -24 / 60)) > 0.5)
        compare(Logic.visualLevel(Math.pow(10, -6 / 60)), 1)
        compare(Logic.visualLevel(NaN), 0)
    }
}
