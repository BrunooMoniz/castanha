"""Opt-in local QA: real PipeWire/FFmpeg, synthetic audio, no physical microphone.

Run from the repository root: PYTHONPATH=. python3 -B tests/qa_pipewire_capture.py
Temporary sinks are explicitly selected and removed; defaults are never changed.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from castanha.audio import AudioDeviceInfo, AudioRecorder, measure_channel_levels, probe_duration_seconds


def pactl(*args):
    return subprocess.check_output(["pactl", *args], text=True, timeout=10).strip()


def main():
    defaults = (pactl("get-default-source"), pactl("get-default-sink"))
    modules, producers = [], []
    recorder = None
    try:
        sinks = [f"castanha_qa_{os.getpid()}_{i}" for i in range(2)]
        for name, frequency in zip(sinks, (440, 880)):
            modules.append(pactl("load-module", "module-null-sink", f"sink_name={name}"))
            producers.append(subprocess.Popen(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                 f"sine=frequency={frequency}:sample_rate=48000", "-ac", "2", "-f", "pulse",
                 "-device", name, "castanha-qa"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1)
        assert all(p.poll() is None for p in producers), "Synthetic generators exited"
        with tempfile.TemporaryDirectory(prefix="castanha-audio-qa-") as directory:
            recorder = AudioRecorder()
            recorder.devices = AudioDeviceInfo(sinks[0] + ".monitor", sinks[1], sinks[1] + ".monitor")
            recorder.start(Path(directory) / "synthetic.ogg", mode="dual")
            time.sleep(5)
            result = recorder.stop()
            levels = measure_channel_levels(result.audio_path, mode="dual")
            duration = probe_duration_seconds(result.audio_path)
            assert duration and duration >= 2, f"Capture too short: {duration}"
            assert len(levels) == 2, f"Expected two channels, got {len(levels)}"
            assert all(not channel.silent for channel in levels), "Synthetic channel missing"
            print(json.dumps({"status": "pass", "duration_seconds": duration,
                              "bytes": result.file_size_bytes, "channels": len(levels),
                              "physical_microphone_used": False}))
    finally:
        if recorder and recorder.is_recording():
            recorder.stop()
        for producer in producers:
            if producer.poll() is None:
                producer.terminate()
            producer.wait(timeout=5)
        for module in reversed(modules):
            pactl("unload-module", module)
        assert defaults == (pactl("get-default-source"), pactl("get-default-sink")), "Audio defaults changed"


if __name__ == "__main__":
    main()
