import QtQuick
import QtTest
import "../.."

TestCase {
    name: "SeparateWaveforms"
    ChannelWaveform { id: mic; width: 140; height: 30 }
    ChannelWaveform { id: call; width: 140; height: 30 }
    function cleanup() { mic.active = false; call.active = false }
    function test_channels_independent_and_reset() {
        mic.peak = 0.7; call.peak = 0
        mic.active = true; call.active = true
        wait(400)
        verify(mic.samples.length > 1)
        compare(mic.samples[mic.samples.length - 1], 0.7)
        compare(call.samples[call.samples.length - 1], 0)
        mic.active = false
        compare(mic.samples.length, 0)
        call.peak = 0.4
        wait(150)
        compare(call.samples[call.samples.length - 1], 0.4)
        compare(mic.samples.length, 0)
    }
}
