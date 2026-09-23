"""Quem fala no Meet: decodificador, host nativo, recorte, alinhamento e atribuição.

As mensagens do Meet são recriadas aqui com o mesmo formato observado numa
chamada de teste em 23/09/2026, com nomes e códigos fictícios.
"""

import base64
import gzip
import io
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from castanha import meet_speakers as ms
from castanha.audio import ChannelLevels

CODE = "abc-defg-hij"
SPACE = "spaces/FakeSpace01"
ANA, BETO = f"{SPACE}/devices/204", f"{SPACE}/devices/205"


def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def msg(*fields):
    return b"".join(field(n, v) for n, v in fields)


def device_message(device, name):
    # .1.2.13.1.2 {1: dispositivo, 2: nome, 3: avatar, 29: primeiro nome}
    person = msg((1, device), (2, name), (3, "https://example.com/avatar"), (4, 3), (29, name.split()[0]))
    return gzip.compress(msg((1, msg((2, msg((13, msg((1, msg((1, 43))), (2, person)))))))))


def source_message(device, ssrc, kind=1):
    # .1.2.3.2 {1: 1, 2: tipo, 4: SSRC em texto, 6: dispositivo, 8: {1: SSRC}}
    source = msg((1, 1), (2, kind), (4, str(ssrc)), (6, device), (8, msg((1, ssrc))))
    return gzip.compress(msg((1, msg((2, msg((3, msg((1, msg((1, 458274574110721))), (2, source)))))))))


def frames(start, end, ssrc, level=0.2, step=0.02):
    t, out = start, []
    while t < end:
        out.append([round(t, 3), ssrc, level])
        t += step
    return out


class DecoderTest(unittest.TestCase):
    def test_device_and_audio_source_are_read_from_the_observed_format(self):
        self.assertEqual(ms.decode_collections(device_message(ANA, "Ana Teste")),
                         [{"type": "device", "device": ANA, "name": "Ana Teste"}])
        self.assertEqual(ms.decode_collections(source_message(ANA, 879306692)),
                         [{"type": "source", "device": ANA, "ssrc": 879306692}])

    def test_video_source_and_foreign_bytes_are_ignored(self):
        self.assertEqual(ms.decode_collections(source_message(ANA, 3685207258, kind=2)), [])
        self.assertEqual(ms.decode_collections(gzip.compress(b"\xff\xfe nada disso")), [])
        with self.assertRaises(Exception):
            ms.decode_collections(b"nem gzip")

    def test_decompression_bomb_is_refused(self):
        with self.assertRaises(ValueError):
            ms.decode_collections(gzip.compress(b"\0" * (ms.MAX_INFLATED_BYTES + 10)))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "meet"
        self.now = 1_790_000_000.0
        self.active = False
        self.session = ms.Session(recording_active=lambda: {"status": "recording"} if self.active else None,
                                  clock=lambda: self.now, root=self.root)

    def send(self, **message):
        self.session.handle({"code": CODE, **message})

    def collections(self, raw):
        self.send(type="collections", t=self.now * 1000, b64=base64.b64encode(raw).decode())

    def test_nothing_is_written_while_castanha_is_not_recording(self):
        self.collections(device_message(ANA, "Ana Teste"))
        self.collections(source_message(ANA, 111))
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.session.devices, {ANA: "Ana Teste"})

    def test_recording_writes_private_file_with_the_map_known_before_it_started(self):
        self.collections(device_message(ANA, "Ana Teste"))
        self.collections(source_message(ANA, 111))
        self.active = True
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2], [float("nan"), 111, 0.2],
                                         [self.now * 1000, 111, 7], ["x", 1, 1]])
        self.collections(device_message(BETO, "Beto Teste"))
        self.send(type="bye")
        files = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        records = [json.loads(line) for line in files[0].read_text().splitlines()]
        self.assertEqual([r["type"] for r in records], ["session", "device", "source", "levels", "device"])
        self.assertEqual(records[3]["frames"], [[self.now, 111, 0.2]])
        self.assertNotIn("avatar", files[0].read_text())

    def test_other_call_in_the_same_tab_is_ignored(self):
        self.active = True
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.session.handle({"code": "zzz-zzzz-zzz", "type": "levels", "frames": [[self.now * 1000, 222, 0.2]]})
        self.session.close()
        text = next(self.root.glob("*.jsonl")).read_text()
        self.assertNotIn("222", text)

    def test_native_messaging_framing(self):
        def frame(obj):
            body = json.dumps(obj).encode()
            return struct.pack("=I", len(body)) + body
        stream = io.BytesIO(frame({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 1, 0.3]]}) + b"\x01\x00")
        self.active = True
        with patch.object(ms, "enabled", return_value=True), patch.object(ms, "prune"):
            self.assertEqual(ms.run_host(stdin=stream, session=self.session), 0)
        self.assertEqual(len(list(self.root.glob("*.jsonl"))), 1)

    def test_disabled_host_stays_up_and_writes_nothing(self):
        body = json.dumps({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 1, 0.3]]}).encode()
        self.active = True
        with patch.object(ms, "enabled", return_value=False):
            ms.run_host(stdin=io.BytesIO(struct.pack("=I", len(body)) + body), session=self.session)
        self.assertFalse(self.root.exists())


class TimelineTest(unittest.TestCase):
    start = 1_790_000_000.0

    def snap(self, extra_frames=()):
        return {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": ANA, "222": BETO},
                "frames": frames(self.start + 1, self.start + 4, 111) + frames(self.start + 5, self.start + 8, 222)
                + list(extra_frames)}

    def utt(self, start, end, origin="audio_sistema", text="fala"):
        return {"speaker": "Áudio do sistema", "text": text, "start": start, "end": end, "channel": 1, "origin": origin}

    def test_wall_clock_skips_paused_time(self):
        pauses = [{"paused_at": self.start + 10, "resumed_at": self.start + 70}]
        self.assertEqual(ms.wall_clock(5, self.start, pauses), self.start + 5)
        self.assertEqual(ms.wall_clock(12, self.start, pauses), self.start + 72)
        self.assertEqual(ms.wall_clock(12, self.start, [{"paused_at": self.start + 10, "resumed_at": None}]),
                         self.start + 12)

    def test_each_remote_utterance_gets_the_participant_who_was_speaking(self):
        names = ms.attribute([self.utt(1.1, 3.9), self.utt(5.2, 7.8)], self.snap(), self.start)
        self.assertEqual({i: v["name"] for i, v in names.items()}, {0: "Ana Teste", 1: "Beto Teste"})

    def test_microphone_never_gets_a_name(self):
        self.assertEqual(ms.attribute([self.utt(1.1, 3.9, origin="microfone_local")], self.snap(), self.start), {})

    def test_mixed_or_silent_interval_keeps_the_channel_label(self):
        # 3.0–6.0 pega Ana e Beto por igual; 9–12 ninguém falava.
        self.assertEqual(ms.attribute([self.utt(3.0, 6.0), self.utt(9, 12)], self.snap(), self.start), {})

    def test_unknown_source_and_quiet_frames_do_not_count(self):
        snap = self.snap(extra_frames=frames(self.start + 9, self.start + 12, 999)
                         + frames(self.start + 13, self.start + 16, 111, level=0.01))
        self.assertEqual(ms.attribute([self.utt(9.1, 11.9), self.utt(13.1, 15.9)], snap, self.start), {})

    def test_pause_moves_later_utterances_to_the_right_speaker(self):
        pauses = [{"paused_at": self.start + 4.5, "resumed_at": self.start + 5.5}]
        # Depois da pausa de 1 s, o segundo 4,6 do arquivo é o 5,6 da chamada (Beto).
        names = ms.attribute([self.utt(4.6, 6.9)], self.snap(), self.start, pauses)
        self.assertEqual(names[0]["name"], "Beto Teste")

    def test_lag_is_found_by_correlating_the_system_channel(self):
        envelope = [0.0] * 100
        for i in range(100):
            t = i * 0.1 - 0.5  # a gravação está 0,5 s atrasada em relação ao Meet
            if 1 <= t < 4 or 5 <= t < 8:
                envelope[i] = 0.3
        lag, r = ms.estimate_lag(envelope, self.snap(), self.start)
        self.assertAlmostEqual(lag, 0.5, delta=0.11)
        self.assertGreater(r, 0.8)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start = datetime(2026, 9, 23, 10, 0, 0)
        self.t0 = self.start.timestamp()

    def session(self, code, begin, end, name="Ana Teste"):
        path = self.root / f"{code}-{int(begin)}.jsonl"
        records = [{"type": "session", "code": code, "t": begin},
                   {"type": "device", "device": ANA, "name": name},
                   {"type": "source", "device": ANA, "ssrc": 111},
                   {"type": "levels", "frames": frames(begin, end, 111, step=0.5)}]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n{cortada")

    def state(self, meeting=None):
        return {"started_at": self.start.isoformat(), "current_meeting": meeting or {}}

    def test_overlapping_session_is_cut_to_the_recording_window(self):
        self.session(CODE, self.t0 - 600, self.t0 + 60)
        snap = ms.snapshot(self.state(), self.t0 + 30, root=self.root)
        self.assertEqual(snap["meeting_code"], CODE)
        self.assertEqual(snap["devices"], {ANA: "Ana Teste"})
        self.assertGreaterEqual(snap["frames"][0][0], self.t0 - ms.SNAPSHOT_MARGIN)
        self.assertLessEqual(snap["frames"][-1][0], self.t0 + 30 + ms.SNAPSHOT_MARGIN)

    def test_no_overlap_means_no_snapshot(self):
        self.session(CODE, self.t0 - 7200, self.t0 - 3600)
        self.assertIsNone(ms.snapshot(self.state(), self.t0 + 30, root=self.root))

    def test_two_calls_at_once_need_the_event_code_to_pick_one(self):
        self.session(CODE, self.t0, self.t0 + 60)
        self.session("xyz-wxyz-xyz", self.t0, self.t0 + 60, name="Outra Pessoa")
        self.assertIsNone(ms.snapshot(self.state(), self.t0 + 30, root=self.root))
        meeting = {"conference_url": f"https://meet.google.com/{CODE}?authuser=0"}
        self.assertEqual(ms.snapshot(self.state(meeting), self.t0 + 30, root=self.root)["meeting_code"], CODE)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.flags = self.root / "chromium-flags.conf"
        self.manifest = self.root / "hosts" / f"{ms.HOST_NAME}.json"
        self.project = self.root / "castanha"
        self.original = "--ozone-platform=wayland\n--load-extension=/usr/share/omarchy/a,/usr/share/omarchy/b\n"
        self.flags.write_text(self.original)

    def test_install_appends_to_the_existing_list_and_is_idempotent(self):
        with patch.object(ms, "enabled", return_value=True):
            ms.install(self.project, self.flags, self.manifest)
            info = ms.install(self.project, self.flags, self.manifest)
        ext = str(self.project / "browser/meet-speakers")
        self.assertEqual(self.flags.read_text().splitlines()[1],
                         f"--load-extension=/usr/share/omarchy/a,/usr/share/omarchy/b,{ext}")
        self.assertEqual((self.root / "chromium-flags.conf.pre-castanha-meet").read_text(), self.original)
        host = json.loads(self.manifest.read_text())
        self.assertEqual(host["allowed_origins"], [f"chrome-extension://{ms.EXTENSION_ID}/"])
        self.assertTrue(info["flags"] and info["native_host"] and info["enabled"])

    def test_uninstall_restores_only_what_was_added(self):
        ms.install(self.project, self.flags, self.manifest)
        ms.uninstall(self.flags, self.manifest)
        self.assertEqual(self.flags.read_text(), self.original)
        self.assertFalse(self.manifest.exists())

    def test_install_without_previous_extensions_creates_the_flag(self):
        self.flags.write_text("--ozone-platform=wayland\n")
        ms.install(self.project, self.flags, self.manifest)
        ms.uninstall(self.flags, self.manifest)
        self.assertEqual(self.flags.read_text(), "--ozone-platform=wayland\n")

    def test_extension_id_matches_the_manifest_key(self):
        import hashlib
        manifest = json.loads((Path(__file__).resolve().parent.parent / "browser/meet-speakers/manifest.json").read_text())
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in digest), ms.EXTENSION_ID)


class FrontmatterTest(unittest.TestCase):
    def test_speakers_block_marks_observed_presence_and_voice(self):
        from castanha.summarizer import silver_frontmatter
        text = silver_frontmatter({"title": "T", "speaker_attribution": {
            "source": ms.EVIDENCE_SOURCE,
            "participants": [{"name": "Ana Teste", "utterances": 3}, {"name": "Beto Teste", "utterances": 0}]}})
        self.assertIn('speakers:\n  source: "google_meet_webrtc"', text)
        self.assertIn('- name: "Ana Teste"\n      presence: "observed"\n      speech: "meet_audio_level"', text)
        self.assertIn('- name: "Beto Teste"\n      presence: "observed"\n      speech: "not_observed"', text)
        self.assertNotIn("speakers:", silver_frontmatter({"title": "T"}))


class EngineIntegrationTest(unittest.TestCase):
    """Do stop à transcrição: a sessão do Meet vira nome no canal do sistema."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / "config/castanha"
        config.mkdir(parents=True)
        base = self.root / "meetings"
        self.config_file = config / "config.json"
        self.config_file.write_text(json.dumps({
            "storage": {"base_dir": str(base), "bronze_dir": str(base / "bronze"),
                        "silver_dir": str(base / "silver"), "gold_dir": str(base / "gold")},
            "llm": {"provider": "groq", "api_key": ""}, "zinom": {"enabled": False, "token": ""},
            "transcription": {"groq_api_key": "", "vps_ssh_host": "", "por_canal": True},
            "meet": {"speakers_enabled": True},
        }))
        for patcher in (
            patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.root / "config"),
                                      "XDG_STATE_HOME": str(self.root / "state"), "CASTANHA_LANG": "pt_BR"}),
            patch("castanha.engine.notify"),
            patch("castanha.engine.measure_channel_levels", return_value=[ChannelLevels(0, "mic", -30, -10, False)]),
            patch("castanha.engine.probe_duration_seconds", return_value=10),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Canal 1 com som em 1–4 s (Ana) e 5–8 s (Beto), como a chamada abaixo.
        self.audio = self.root / "estereo.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "aevalsrc=0.2*sin(2*PI*300*t)|0.3*sin(2*PI*440*t)*(between(t\\,1\\,4)+between(t\\,5\\,8)):d=10:s=16000",
                        str(self.audio)], check=True, capture_output=True)
        # O stop usa o relógio real: a gravação "começou" há 20 s.
        self.started = datetime.fromtimestamp(round(time.time()) - 20)
        t0 = self.started.timestamp()
        meet = self.root / "state/castanha/meet"
        meet.mkdir(parents=True)
        records = [{"type": "session", "code": CODE, "t": t0 - 60},
                   {"type": "device", "device": ANA, "name": "Ana Teste"},
                   {"type": "device", "device": BETO, "name": "Beto Teste"},
                   {"type": "source", "device": ANA, "ssrc": 111},
                   {"type": "source", "device": BETO, "ssrc": 222},
                   {"type": "levels", "frames": frames(t0 + 1, t0 + 4, 111) + frames(t0 + 5, t0 + 8, 222)}]
        (meet / f"{CODE}-x.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def engine(self):
        from castanha.engine import CastanhaEngine
        engine = CastanhaEngine()
        engine.state_mgr.write({"status": "recording", "pid": None, "audio_path": str(self.audio), "mode": "dual",
                                "started_at": self.started.isoformat(), "pauses": [],
                                "current_meeting": {"title": "Fixture",
                                                    "conference_url": f"https://meet.google.com/{CODE}"}})
        return engine

    def stop(self, engine):
        from castanha.transcription import TranscriptionResult, Utterance

        class Provider:
            def transcribe(self, path, mode="dual"):
                if int(Path(path).stem[-1]) == 0:
                    return TranscriptionResult("oi", [Utterance("Falante", "oi", 0.2, 0.9)], "fixture", {})
                return TranscriptionResult("ana fala beto fala", [Utterance("Falante", "ana fala", 1.1, 3.9),
                                                                   Utterance("Falante", "beto fala", 5.1, 7.9)],
                                           "fixture", {})

        with patch("castanha.engine.get_transcriber", return_value=Provider()), \
             patch.object(engine.summarizer, "generate_silver", return_value="## Resumo\n\nx"), \
             patch.object(engine.summarizer, "generate_gold", return_value={"facts": []}):
            return engine.stop_recording()

    def test_names_reach_transcript_segments_and_metadata(self):
        engine = self.engine()
        result = self.stop(engine)
        bronze = engine.storage.bronze_dir / result["result"]["slug"]
        text = (bronze / "transcript_raw.txt").read_text()
        self.assertIn("Microfone local: oi", text)
        self.assertIn("Ana Teste (Meet): ana fala", text)
        self.assertIn("Beto Teste (Meet): beto fala", text)
        utterances = json.loads((bronze / "transcript_segments.json").read_text())["recordings"][0]["utterances"]
        self.assertEqual([u.get("attributed_name") for u in utterances], [None, "Ana Teste", "Beto Teste"])
        # Origem e rótulo técnico continuam os do canal.
        self.assertEqual([u["origin"] for u in utterances], ["microfone_local", "audio_sistema", "audio_sistema"])
        self.assertEqual(utterances[1]["speaker"], "Áudio do sistema")
        meta = engine.storage._read_bronze_metadata(result["result"]["slug"])
        self.assertEqual(meta["speaker_attribution"]["attributed"], 2)
        self.assertEqual(meta["speaker_attribution"]["meeting_codes"], [CODE])
        from castanha.library import MeetingLibrary
        labels = [seg["channel"] for seg in MeetingLibrary(engine.storage).detail(result["result"]["slug"])["meeting"]["segments"]]
        self.assertEqual(labels, ["Microfone local", "Ana Teste", "Beto Teste"])
        snap = next((bronze / ".speakers").glob("*[0-9a-f].json"))
        self.assertEqual(snap.stat().st_mode & 0o077, 0)

    def test_disabled_flag_keeps_channel_labels(self):
        data = json.loads(self.config_file.read_text())
        data["meet"]["speakers_enabled"] = False
        self.config_file.write_text(json.dumps(data))
        engine = self.engine()
        result = self.stop(engine)
        bronze = engine.storage.bronze_dir / result["result"]["slug"]
        self.assertNotIn("(Meet)", (bronze / "transcript_raw.txt").read_text())
        self.assertFalse((bronze / ".speakers").exists())

    def test_pause_and_resume_are_recorded(self):
        engine = self.engine()
        with patch("os.kill"):
            engine.pause_recording()
            engine.resume_recording()
        pauses = engine.state_mgr.read()["pauses"]
        self.assertEqual(len(pauses), 1)
        self.assertLessEqual(pauses[0]["paused_at"], pauses[0]["resumed_at"])


if __name__ == "__main__":
    unittest.main()
