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

from castanha.audio import (AudioDeviceInfo, AudioRecorder, measure_channel_levels,
                            probe_duration_seconds, read_audio_peak)


def pactl(*args):
    return subprocess.check_output(["pactl", *args], text=True, timeout=10).strip()


def main():
    defaults = (pactl("get-default-source"), pactl("get-default-sink"))
    modules, producers = [], []
    recorder = None
    mic_recorder = None
    cleanup_errors = []
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
            time.sleep(2)
            loud_peak = read_audio_peak(recorder.peak_path)
            assert loud_peak is not None and loud_peak > 0.1, f"Live peak missing: {loud_peak}"
            mic_recorder = AudioRecorder()
            mic_recorder.devices = recorder.devices
            mic_recorder.start(Path(directory) / "synthetic-mic.ogg", mode="mic_only")
            time.sleep(1)
            mic_peak = read_audio_peak(mic_recorder.peak_path)
            assert mic_peak is not None and mic_peak > 0.1, f"Mic-only peak missing: {mic_peak}"
            mic_recorder.stop()
            mic_recorder = None
            for producer in producers:
                producer.terminate()
            for producer in producers:
                producer.wait(timeout=5)
            producers.clear()
            time.sleep(1)
            quiet_peak = read_audio_peak(recorder.peak_path)
            assert quiet_peak is not None and quiet_peak == 0.0, f"Silence not projected: {quiet_peak}"
            result = recorder.stop()
            levels = measure_channel_levels(result.audio_path, mode="dual")
            duration = probe_duration_seconds(result.audio_path)
            assert duration and duration >= 2, f"Capture too short: {duration}"
            assert len(levels) == 2, f"Expected two channels, got {len(levels)}"
            assert all(not channel.silent for channel in levels), "Synthetic channel missing"
            print(json.dumps({"status": "pass", "duration_seconds": duration,
                              "bytes": result.file_size_bytes, "channels": len(levels),
                              "physical_microphone_used": False,
                              "live_peak": loud_peak, "mic_only_peak": mic_peak,
                              "silence_peak": quiet_peak}))
    finally:
        for active_recorder in (mic_recorder, recorder):
            if active_recorder and active_recorder.is_recording():
                try:
                    active_recorder.stop()
                except Exception as error:
                    cleanup_errors.append(f"recorder: {error}")
        for producer in producers:
            if producer.poll() is None:
                producer.terminate()
            try:
                producer.wait(timeout=5)
            except Exception as error:
                cleanup_errors.append(f"producer: {error}")
        for module in reversed(modules):
            try:
                pactl("unload-module", module)
            except Exception as error:
                cleanup_errors.append(f"module {module}: {error}")
        if cleanup_errors:
            raise RuntimeError("Falha na limpeza do QA: " + "; ".join(cleanup_errors))
        assert defaults == (pactl("get-default-source"), pactl("get-default-sink")), "Audio defaults changed"


if __name__ == "__main__":
    main()
