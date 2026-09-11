"""Real FFmpeg capture graph, isolated inputs and truthful per-origin state."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from castanha.audio import (AudioDeviceInfo, AudioRecorder, capture_channel_peaks,
                            is_safe_capture_peak, read_audio_peak, read_audio_peak_sample)
from castanha.daemon import CastanhaDaemon
from castanha.engine import CastanhaEngine
from castanha.state import StateManager


class TestDualAudioMeters(unittest.TestCase):
    def test_real_capture_each_origin_independent_and_same_process(self):
        tone = 'sine=frequency=440:sample_rate=48000:duration=0.8,pan=stereo|c0=c0|c1=c0'
        quiet = 'anullsrc=r=48000:cl=stereo:d=0.8'
        real_popen = subprocess.Popen
        for mic_on, call_on, mode in ((True, False, 'dual'), (False, True, 'dual'),
                                      (True, True, 'dual'), (False, False, 'dual'),
                                      (True, False, 'mic_only')):
            with self.subTest(mic=mic_on, call=call_on, mode=mode), tempfile.TemporaryDirectory() as directory:
                recorder = AudioRecorder()
                recorder.devices = AudioDeviceInfo('fixture-mic', 'fixture-sink', 'fixture-monitor')
                commands = []
                def capture(cmd, **kwargs):
                    commands.append(cmd[:])
                    translated = []
                    i = 0
                    while i < len(cmd):
                        if cmd[i:i+2] == ['-f', 'pulse']:
                            name = cmd[i+3]
                            active = mic_on if name == 'fixture-mic' else call_on
                            translated.extend(['-f', 'lavfi', '-i', tone if active else quiet])
                            i += 4
                        else:
                            translated.append(cmd[i]); i += 1
                    kwargs['stderr'] = subprocess.PIPE
                    return real_popen(translated, **kwargs)
                with patch('castanha.audio.subprocess.Popen', side_effect=capture):
                    process = recorder.start(Path(directory) / 'capture.ogg', mode=mode)
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(len(commands), 1)
                self.assertEqual(commands[0].count('pulse'), 2 if mode == 'dual' else 1)
                state = {'status': 'recording', 'mode': mode,
                         'mic_peak_path': str(recorder.mic_peak_path),
                         'call_peak_path': str(recorder.call_peak_path) if recorder.call_peak_path else None}
                updates = capture_channel_peaks(state)
                self.assertGreater(updates['mic_peak'], 0.3) if mic_on else self.assertEqual(updates['mic_peak'], 0)
                self.assertGreater(updates['call_peak'], 0.3) if call_on else self.assertEqual(updates['call_peak'], 0)
                self.assertGreater(updates['mic_peak_updated_at'], 0)
                if mode == 'mic_only':
                    self.assertIsNone(recorder.call_peak_path)
                    self.assertEqual(updates['call_peak_updated_at'], 0)
                else:
                    self.assertGreater(updates['call_peak_updated_at'], 0)
                self.assertAlmostEqual(read_audio_peak(recorder.peak_path), max(updates['mic_peak'], updates['call_peak']))
                stream = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                    'stream=codec_name,channels', '-of', 'json', str(recorder.output_path)]))['streams'][0]
                self.assertEqual(stream['codec_name'], 'opus')
                self.assertEqual(stream['channels'], 2)  # Existing mic_only format remains unchanged too.
                sidecars = [p for p in (recorder.peak_path, recorder.mic_peak_path, recorder.call_peak_path) if p]
                recorder.stop()
                self.assertTrue(all(not path.exists() for path in sidecars))
                self.assertGreater(recorder.output_path.stat().st_size, 0)

    def test_live_frames_arrive_then_expire_during_real_process_pause(self):
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            recorder = AudioRecorder()
            recorder.devices = AudioDeviceInfo('mic', 'sink', 'monitor')
            def capture(cmd, **kwargs):
                translated = []
                i = 0
                while i < len(cmd):
                    if cmd[i:i+2] == ['-f', 'pulse']:
                        source = ('sine=frequency=440:sample_rate=48000:duration=10,pan=stereo|c0=c0|c1=c0'
                                  if cmd[i+3] == 'mic' else 'anullsrc=r=48000:cl=stereo:d=10')
                        translated.extend(['-re', '-f', 'lavfi', '-i', source]); i += 4
                    else:
                        translated.append(cmd[i]); i += 1
                return real_popen(translated, **kwargs)
            def await_live():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if read_audio_peak_sample(recorder.mic_peak_path) and read_audio_peak_sample(recorder.call_peak_path):
                        return
                    time.sleep(0.05)
                self.fail('Fresh meter frames did not arrive in bounded time')
            try:
                with patch('castanha.audio.subprocess.Popen', side_effect=capture):
                    recorder.start(Path(directory) / 'live.ogg')
                await_live()
                self.assertGreater(read_audio_peak(recorder.mic_peak_path), 0)
                self.assertEqual(read_audio_peak(recorder.call_peak_path), 0)
                recorder.pause()
                time.sleep(1.7)
                self.assertIsNone(read_audio_peak_sample(recorder.mic_peak_path))
                self.assertIsNone(read_audio_peak_sample(recorder.call_peak_path))
                recorder.resume()
                await_live()
            finally:
                if recorder.process:
                    recorder.resume()
                    recorder.stop()
            self.assertFalse(list(Path(directory).glob('*.peak')))

    def test_staleness_and_timestamps_are_independent_and_silence_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            mic, call = (Path(directory) / name for name in ('mic.peak', 'call.peak'))
            mic.write_text('lavfi.astats.1.Peak_level=-18\n')
            call.write_text('lavfi.astats.2.Peak_level=-inf\n')
            now = time.time()
            os.utime(mic, (now-2, now-2)); os.utime(call, (now, now))
            state = {'status': 'recording', 'mode': 'dual', 'mic_peak_path': str(mic), 'call_peak_path': str(call)}
            updates = capture_channel_peaks(state)
            self.assertEqual(updates['mic_peak'], 0)
            self.assertEqual(updates['mic_peak_updated_at'], 0)
            self.assertEqual(updates['call_peak'], 0)
            self.assertEqual(updates['call_peak_updated_at'], call.stat().st_mtime)
            os.utime(mic, (now, now)); os.utime(call, (now-2, now-2))
            updates = capture_channel_peaks(state)
            self.assertGreater(updates['mic_peak'], 0)
            self.assertEqual(updates['mic_peak_updated_at'], mic.stat().st_mtime)
            self.assertEqual(updates['call_peak_updated_at'], 0)
            for status in ('paused', 'processing', 'idle'):
                self.assertEqual(set(capture_channel_peaks({**state, 'status': status}).values()), {0.0})
            self.assertEqual(capture_channel_peaks({**state, 'mode': 'mic_only'})['call_peak_updated_at'], 0)

    def test_sidecar_cleanup_only_accepts_capture_derived_paths_and_start_failure_cleans(self):
        audio = Path('/tmp/castanha_rec_1234567890.ogg')
        for channel in ('mic', 'call', None):
            self.assertTrue(is_safe_capture_peak(audio, AudioRecorder._peak_path(audio, channel)))
        self.assertFalse(is_safe_capture_peak(audio, Path('/tmp/private.txt')))
        with tempfile.TemporaryDirectory() as directory:
            recorder = AudioRecorder()
            recorder.devices = AudioDeviceInfo('mic', 'sink', 'monitor')
            with patch('castanha.audio.subprocess.Popen', side_effect=OSError('fixture cannot start')):
                with self.assertRaises(OSError): recorder.start(Path(directory) / 'capture.ogg')
            self.assertIsNone(recorder.peak_path)
            self.assertIsNone(recorder.mic_peak_path)
            self.assertIsNone(recorder.call_peak_path)
            self.assertEqual(list(Path(directory).glob('*.peak')), [])

    def test_invalid_or_missing_data_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mic.peak'
            self.assertIsNone(read_audio_peak_sample(path))
            for text in ('', 'arbitrary', 'lavfi.astats.1.Peak_level=inf\n'):
                path.write_text(text)
                self.assertIsNone(read_audio_peak_sample(path))
            fifo = Path(directory) / 'blocked.peak'
            os.mkfifo(fifo)
            self.assertIsNone(read_audio_peak_sample(fifo))
            link = Path(directory) / 'linked.peak'
            link.symlink_to(path)
            self.assertIsNone(read_audio_peak_sample(link))

    def test_engine_state_names_pause_and_cross_instance_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            engine = CastanhaEngine.__new__(CastanhaEngine)
            engine.state_mgr = StateManager()
            engine.config = {'audio': {}}
            engine.recorder = AudioRecorder()
            engine.recorder.devices = AudioDeviceInfo('mic-id', 'sink-id', 'monitor-id',
                                                      'Microfone USB', 'Monitor da saída USB')
            sidecars = []
            def start(path, **kwargs):
                for field, channel in (('peak_path', None), ('mic_peak_path', 'mic'), ('call_peak_path', 'call')):
                    sidecar = AudioRecorder._peak_path(path, channel)
                    sidecar.write_text('lavfi.astats.Overall.Peak_level=-18\n')
                    setattr(engine.recorder, field, sidecar)
                    sidecars.append(sidecar)
                return Mock(pid=None)
            # A unique synthetic timestamp keeps this test away from live paths.
            stamp = 900000000000 + os.getpid()
            try:
                with patch.object(engine.recorder, 'start', side_effect=start), \
                     patch('castanha.engine.time.time', return_value=stamp), \
                     patch('castanha.engine.is_default_source_muted', return_value=False), \
                     patch('castanha.engine.notify'):
                    engine.start_recording(title='Fixture')
                state = engine.state_mgr.read()
                self.assertEqual(state['mic_device_name'], 'Microfone USB')
                self.assertEqual(state['call_device_name'], 'Monitor da saída USB')
                self.assertEqual(state['mic_peak_path'], str(sidecars[1]))
                engine.state_mgr.write({'mic_peak': 0.8, 'call_peak': 0.7,
                                        'mic_peak_updated_at': 1000, 'call_peak_updated_at': 1000})
                with patch('castanha.engine.notify'):
                    engine.pause_recording()
                    self.assertEqual(engine.state_mgr.read()['mic_peak'], 0)
                    self.assertEqual(engine.state_mgr.read()['call_peak_updated_at'], 0)
                    # A fresh CLI recorder has no paths in memory; state must clean all.
                    engine.recorder = AudioRecorder()
                    stopped = engine.stop_recording()
                self.assertEqual(stopped['status'], 'error')  # Deliberately no captured audio in this fixture.
                self.assertTrue(all(not p.exists() for p in sidecars))
                self.assertIsNone(engine.state_mgr.read()['mic_peak_path'])
            finally:
                for path in sidecars: path.unlink(missing_ok=True)

    def test_daemon_recording_tick_is_250ms_and_paused_tick_is_one_second(self):
        import threading
        from types import SimpleNamespace
        for status, interval in (('recording', 0.25), ('paused', 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
                daemon = CastanhaDaemon.__new__(CastanhaDaemon)
                daemon.config = {'calendar': {'enabled': False}}
                daemon.state_mgr = StateManager()
                daemon.state_mgr.write({'status': status, 'mic_peak': 0.8, 'call_peak': 0.8})
                daemon._agenda_lock = threading.Lock()
                daemon._agenda_result = None
                daemon._agenda_thread = None
                daemon.retry_scheduler = SimpleNamespace(tick=Mock())
                daemon.running = True
                sleeps = []
                def sleep(seconds): sleeps.append(seconds); daemon.running = False
                with patch('castanha.daemon.signal.signal'), patch('castanha.daemon.time.sleep', side_effect=sleep):
                    daemon.run()
                self.assertEqual(sleeps, [interval])
                self.assertEqual(daemon.state_mgr.read()['mic_peak'], 0)
                self.assertEqual(daemon.state_mgr.read()['call_peak'], 0)

    def test_successful_agenda_timestamp_survives_reset_errors_do_not_advance(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            daemon = CastanhaDaemon.__new__(CastanhaDaemon)
            daemon.state_mgr = StateManager()
            daemon.config = {'calendar': {}}
            daemon.notified_meeting_uids = set()
            with patch('castanha.daemon.time.time', return_value=1000):
                daemon._apply_agenda_result(([], None))
            self.assertEqual(daemon.state_mgr.read()['agenda_updated_at'], 1000)
            with patch('castanha.daemon.time.time', return_value=2000):
                daemon._apply_agenda_result(([], RuntimeError('fixture failure')))
            self.assertEqual(daemon.state_mgr.read()['agenda_updated_at'], 1000)
            self.assertEqual(daemon.state_mgr.reset()['agenda_updated_at'], 1000)


if __name__ == '__main__': unittest.main()
