"""Quem fala no Meet: decodificador, host nativo, recorte, alinhamento e atribuição.

As mensagens do Meet são recriadas aqui com o mesmo formato observado numa
chamada de teste em 23/09/2026, com nomes e códigos fictícios.
"""

import base64
import gzip
import io
import json
import os
import stat
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


def attribute(utterances, snap, start, pauses=None, lags=None):
    """Correlação boa em todos os trechos, salvo quando o teste escolhe outra."""
    return ms.label_voices(ms.attribute(utterances, snap, start, pauses, [(0.0, 1.0)] * 8 if lags is None else lags))


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

    def test_truncated_message_is_an_error_not_an_empty_update(self):
        with self.assertRaises(ValueError):
            ms.decode_collections(source_message(BETO, 111)[:-12])

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
                                  clock=lambda: self.now, root=self.root, check_interval=0)

    def send(self, **message):
        self.session.handle({"code": CODE, **message})

    def collections(self, raw):
        self.send(type="collections", t=self.now * 1000, b64=base64.b64encode(raw).decode())

    def test_nothing_is_written_while_castanha_is_not_recording(self):
        self.collections(device_message(ANA, "Ana Teste"))
        self.collections(source_message(ANA, 111))
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.assertFalse(self.root.exists())
        self.assertEqual({d: v[0] for d, v in self.session.devices.items()}, {ANA: "Ana Teste"})

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

    def test_after_the_recording_stops_nothing_else_is_written(self):
        self.collections(device_message(ANA, "Ana Teste"))
        self.active = True
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        first = next(self.root.glob("*.jsonl"))
        self.active, self.now = False, self.now + 5
        self.collections(device_message(BETO, "Beto Teste"))
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.assertNotIn("Beto", first.read_text())
        self.assertIsNone(self.session.file)
        # A próxima gravação abre outro arquivo, já com o mapa atual.
        self.active, self.now = True, self.now + 100
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        second = [p for p in self.root.glob("*.jsonl") if p != first]
        self.assertEqual(len(second), 1)
        self.assertIn("Beto Teste", second[0].read_text())

    def test_oversized_message_is_skipped_without_losing_the_next_one(self):
        def frame(obj):
            body = json.dumps(obj).encode()
            return struct.pack("=I", len(body)) + body
        big = ms.MAX_MESSAGE_BYTES + 10
        stream = io.BytesIO(struct.pack("=I", big) + b"x" * big
                            + frame({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 1, 0.3]]}))
        self.active = True
        with patch.object(ms, "enabled", return_value=True), patch.object(ms, "prune"):
            ms.run_host(stdin=stream, session=self.session)
        self.assertEqual(self.session.errors, 1)
        self.assertEqual(len(list(self.root.glob("*.jsonl"))), 1)

    def test_last_batch_before_the_stop_still_enters(self):
        state = {"status": "recording"}
        session = ms.Session(recording_active=lambda: state, clock=lambda: self.now, root=self.root, check_interval=0)
        session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 111, 0.2]]})
        state.update(status="processing", stopped_at=self.now + 1)
        self.now += 1.5
        session.handle({"code": CODE, "type": "levels", "frames": [[(self.now - 1.0) * 1000, 222, 0.2],
                                                                   [(self.now - 0.2) * 1000, 333, 0.2]]})
        text = next(self.root.glob("*.jsonl")).read_text()
        self.assertIn("222", text)
        self.assertNotIn("333", text)
        # Passado o prazo do último lote, o arquivo fecha.
        self.now += ms.STOP_GRACE
        session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 444, 0.2]]})
        self.assertIsNone(session.file)

    def test_batches_around_a_pause_keep_only_recorded_moments(self):
        state = {"status": "recording", "pauses": []}
        session = ms.Session(recording_active=lambda: state, clock=lambda: self.now, root=self.root, check_interval=0)
        session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 111, 0.2]]})
        state.update(status="paused", pauses=[{"paused_at": self.now + 1, "resumed_at": None}])
        self.now += 1.5
        # Lote em trânsito: 0,8 s antes da pausa entra; 0,2 s depois dela, não.
        session.handle({"code": CODE, "type": "levels", "frames": [[(self.now - 0.7) * 1000, 222, 0.2],
                                                                   [(self.now - 0.3) * 1000, 333, 0.2]]})
        state.update(status="recording", pauses=[{"paused_at": self.now - 0.5, "resumed_at": self.now + 10}])
        self.now += 10.2
        # Primeiro lote depois de retomar ainda traz um pacote da pausa.
        session.handle({"code": CODE, "type": "levels", "frames": [[(self.now - 0.5) * 1000, 444, 0.2],
                                                                   [(self.now - 0.1) * 1000, 555, 0.2]]})
        text = next(self.root.glob("*.jsonl")).read_text()
        self.assertIn(" 222,", text)
        self.assertIn(" 555,", text)
        self.assertNotIn(" 333,", text)
        self.assertNotIn(" 444,", text)

    def test_pause_keeps_the_map_but_not_the_voices(self):
        state = {"status": "recording"}
        session = ms.Session(recording_active=lambda: state, clock=lambda: self.now, root=self.root, check_interval=0)
        session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 111, 0.2]]})
        state["status"] = "paused"
        session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 222, 0.2]]})
        session.handle({"code": CODE, "type": "collections", "b64": base64.b64encode(device_message(BETO, "Beto Teste")).decode()})
        text = next(self.root.glob("*.jsonl")).read_text()
        self.assertNotIn("222", text)
        self.assertIn("Beto Teste", text)

    def test_lost_message_is_recorded_and_the_map_keeps_the_learned_time(self):
        self.collections(source_message(ANA, 111))
        learned = self.now
        self.active, self.now = True, self.now + 30
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.send(type="collections", b64="!!!")
        records = [json.loads(line) for line in next(self.root.glob("*.jsonl")).read_text().splitlines()]
        self.assertEqual([r["t"] for r in records if r["type"] == "source"], [learned])
        self.assertEqual([r["reason"] for r in records if r["type"] == "error"], ["decode"])

    def test_page_stall_is_recorded(self):
        self.active = True
        self.send(type="levels", frames=[], gaps=[[self.now * 1000 - 3000, self.now * 1000]])
        text = next(self.root.glob("*.jsonl")).read_text()
        self.assertIn('"type": "gap"', text)

    def test_incomplete_replay_is_recorded_as_error(self):
        self.active = True
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.send(type="replay_incomplete")
        self.assertIn('"reason": "replay"', next(self.root.glob("*.jsonl")).read_text())

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
        return {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"},
                "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
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
        names = attribute([self.utt(1.1, 3.9), self.utt(5.2, 7.8)], self.snap(), self.start)
        self.assertEqual({i: v["name"] for i, v in names.items()}, {0: "Ana Teste", 1: "Beto Teste"})

    def test_without_measured_correlation_nothing_is_named(self):
        self.assertEqual(ms.attribute([self.utt(1.1, 3.9)], self.snap(), self.start), {})

    def test_voice_before_the_binding_arrived_belongs_to_the_only_owner_the_source_had(self):
        # O Meet só anuncia o vínculo quando alguém entra ou muda: a fonte que teve
        # um dono só na chamada inteira era dele desde o primeiro pacote.
        snap = dict(self.snap(), sources={"111": [[self.start + 9, ANA]], "222": [[0, BETO]]})
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start)[0]["name"], "Ana Teste")

    def test_name_announced_late_names_what_the_person_said_before(self):
        # Medido em reuniões reais: o nome costuma chegar só quando a pessoa sai da chamada.
        snap = dict(self.snap(), devices={ANA: [[self.start + 3000, "Ana Teste"]], BETO: [[0, "Beto Teste"]]})
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start)[0]["name"], "Ana Teste")

    def test_participant_the_meet_never_named_is_one_numbered_person(self):
        # Dois dispositivos sem nome: cada um é sempre o mesmo "Participante N", na ordem em que falaram.
        snap = {"devices": {}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 1, self.start + 4, 111) + frames(self.start + 5, self.start + 8, 222)
                + frames(self.start + 9, self.start + 12, 111)}
        names = attribute([self.utt(1.1, 3.9), self.utt(5.2, 7.8), self.utt(9.1, 11.9)], snap, self.start)
        self.assertEqual([(names[i]["name"], names[i]["named"]) for i in range(3)],
                         [("Participante 1", False), ("Participante 2", False), ("Participante 1", False)])

    def test_same_person_on_two_devices_is_one_name(self):
        # Quem cai e volta ganha outro dispositivo com o mesmo nome.
        snap = {"devices": {ANA: "Ana Teste", BETO: "Ana Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 1, self.start + 2.5, 111) + frames(self.start + 2.5, self.start + 4, 222)}
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start)[0]["name"], "Ana Teste")

    def test_source_bound_late_competes_even_with_someone_named_talking(self):
        snap = self.snap(extra_frames=frames(self.start + 1, self.start + 4, 333))
        snap["sources"]["333"] = [[self.start + 9, f"{SPACE}/devices/206"]]
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start), {})

    def test_renaming_a_device_does_not_rewrite_the_past(self):
        snap = dict(self.snap(), devices={ANA: [[0, "Ana Teste"], [self.start + 10, "Outro Nome"]],
                                          BETO: [[0, "Beto Teste"]]})
        names = attribute([self.utt(1.1, 3.9)], snap, self.start)
        self.assertEqual(names[0]["name"], "Ana Teste")

    def test_unbound_voice_alone_competes_with_a_named_one(self):
        # Host reiniciado: a fonte 777 não tem dono no mapa e fala sozinha 6 s.
        snap = self.snap(extra_frames=frames(self.start + 20, self.start + 23, 111)
                         + frames(self.start + 23, self.start + 29, 777))
        self.assertEqual(attribute([self.utt(20, 29)], snap, self.start), {})

    def test_utterance_over_a_page_stall_gets_no_name(self):
        snap = dict(self.snap(), gaps=[[self.start + 2.0, self.start + 3.0]])
        names = attribute([self.utt(1.1, 3.9), self.utt(5.2, 7.8)], snap, self.start)
        self.assertEqual(list(names), [1])

    def test_microphone_never_gets_a_name(self):
        self.assertEqual(attribute([self.utt(1.1, 3.9, origin="microfone_local")], self.snap(), self.start), {})

    def test_mixed_or_silent_interval_keeps_the_channel_label(self):
        # 3.0–6.0 pega Ana e Beto por igual; 9–12 ninguém falava.
        self.assertEqual(attribute([self.utt(3.0, 6.0), self.utt(9, 12)], self.snap(), self.start), {})

    def test_unknown_source_alone_is_a_numbered_participant_and_a_quiet_voice_alone_is_named(self):
        # Fonte sem dono falando sozinha é gente (host reiniciado, mapa perdido): não ganha
        # nome, mas é uma pessoa. Voz baixa e sozinha no trecho é de quem o Meet diz que é.
        snap = self.snap(extra_frames=frames(self.start + 9, self.start + 12, 999)
                         + frames(self.start + 13, self.start + 16, 111, level=0.01))
        names = attribute([self.utt(9.1, 11.9), self.utt(13.1, 15.9)], snap, self.start)
        self.assertEqual([(names[0]["name"], names[0]["named"]), (names[1]["name"], names[1]["named"])],
                         [("Participante 1", False), ("Ana Teste", True)])

    def test_utterance_across_a_pause_gets_no_name(self):
        # Ana fala 1–4 no arquivo; a pausa de 60 s cai no meio e Beto só fala durante ela.
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 1, self.start + 3, 111) + frames(self.start + 3.5, self.start + 62, 222)
                + frames(self.start + 63, self.start + 64, 111)}
        pauses = [{"paused_at": self.start + 3, "resumed_at": self.start + 63}]
        self.assertEqual(attribute([self.utt(1.0, 4.0)], snap, self.start, pauses), {})
        # Logo depois da pausa, mesmo com atraso, a voz de Beto na pausa não conta.
        names = attribute([self.utt(3.1, 3.9)], snap, self.start, pauses, lags=[(0, 1), (1.0, 1)])
        self.assertEqual(names, {})

    def test_participant_voice_without_name_blocks_attribution(self):
        # Dispositivo sem nome conhecido e fonte cujo dono só chegou depois da gravação.
        nameless = self.snap(extra_frames=frames(self.start + 2, self.start + 4, 333))
        nameless["sources"]["333"] = [[0, f"{SPACE}/devices/999"]]
        self.assertEqual(attribute([self.utt(1.1, 3.9)], nameless, self.start), {})
        late = dict(self.snap(extra_frames=frames(self.start + 2, self.start + 4, 444)), late_sources=["444"])
        self.assertEqual(attribute([self.utt(1.1, 3.9)], late, self.start), {})

    def test_quiet_named_voice_still_competes(self):
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 1, self.start + 4.5, 111) + frames(self.start + 4.5, self.start + 10, 222, level=0.04)}
        self.assertEqual(attribute([self.utt(1, 10)], snap, self.start), {})

    def test_unknown_voice_that_spoke_alone_keeps_competing_when_quieter(self):
        snap = self.snap(extra_frames=frames(self.start + 20, self.start + 22, 777)
                         + frames(self.start + 23, self.start + 28, 111, level=0.3)
                         + frames(self.start + 25, self.start + 28, 777, level=0.1))
        self.assertEqual(attribute([self.utt(23, 28)], snap, self.start), {})

    def test_three_seconds_of_someone_else_in_a_long_segment_is_not_absorbed(self):
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 30, self.start + 57, 111) + frames(self.start + 57, self.start + 60, 222)}
        self.assertEqual(attribute([self.utt(30, 60)], snap, self.start), {})

    def test_recording_that_stops_following_the_meet_locally_gets_no_name(self):
        # Ana fala 1–4 no Meet, mas o canal gravado tem outro som, sem relação (Meet mudo).
        envelope = [0.0] * 100
        for i in range(100):
            envelope[i] = 0.3 if (i // 3) % 2 == 0 else 0.0
        self.assertEqual(ms.attribute([self.utt(1.1, 3.9)], self.snap(), self.start, lags=[(0, 1)], envelope=envelope), {})
        following = [0.3 if 1 <= i * 0.1 < 4 or 5 <= i * 0.1 < 8 else 0.0 for i in range(100)]
        names = ms.label_voices(ms.attribute([self.utt(1.1, 3.9)], self.snap(), self.start, lags=[(0, 1)], envelope=following))
        self.assertEqual(names[0]["name"], "Ana Teste")

    def test_unknown_voice_louder_than_the_named_one_competes(self):
        snap = self.snap(extra_frames=frames(self.start + 1, self.start + 4, 777, level=0.6))
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start), {})

    def test_meet_mix_source_that_belongs_to_nobody_is_ignored(self):
        # Como na chamada real: a mistura acompanha toda a fala com dono, nunca sozinha.
        base = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]}}
        talk = [f for k in range(0, 60, 8) for f in frames(self.start + k + 1, self.start + k + 4, 111)
                + frames(self.start + k + 5, self.start + k + 8, 222)]
        mix = [[t, 42, level] for t, _, level in talk]
        names = attribute([self.utt(1.1, 3.9), self.utt(5.2, 7.8)], dict(base, frames=talk + mix), self.start)
        self.assertEqual([names[0]["name"], names[1]["name"]], ["Ana Teste", "Beto Teste"])

    def test_unknown_source_only_over_someone_is_a_person_not_the_mix(self):
        # Só 3 s por cima de Ana: não acompanha a fala toda, então é gente.
        snap = self.snap(extra_frames=frames(self.start + 1, self.start + 4, 777, level=0.1))
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start), {})

    def test_very_quiet_reply_still_competes(self):
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                "frames": frames(self.start + 1, self.start + 4.6, 111) + frames(self.start + 4.6, self.start + 11, 222, level=0.012)}
        self.assertEqual(attribute([self.utt(1, 11)], snap, self.start), {})

    def test_short_interjection_is_tolerated_but_a_real_second_voice_is_not(self):
        snap = self.snap()
        # Beto 5–8; Ana entra 0,3 s (interjeição) e depois 2 s (outra fala).
        quick = dict(snap, frames=snap["frames"] + frames(self.start + 6.0, self.start + 6.2, 111))
        self.assertEqual(attribute([self.utt(5.2, 7.8)], quick, self.start)[0]["name"], "Beto Teste")
        mixed = {"devices": snap["devices"], "sources": snap["sources"],
                 "frames": frames(self.start + 20, self.start + 28, 111) + frames(self.start + 28, self.start + 30, 222)}
        self.assertEqual(attribute([self.utt(20, 30)], mixed, self.start), {})

    def test_source_reassigned_mid_call_keeps_the_owner_of_each_moment(self):
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"},
                "sources": {"111": [[0, ANA], [self.start + 10, BETO]]},
                "frames": frames(self.start + 1, self.start + 4, 111) + frames(self.start + 11, self.start + 14, 111)}
        names = attribute([self.utt(1.1, 3.9), self.utt(11.1, 13.9)], snap, self.start)
        self.assertEqual([names[0]["name"], names[1]["name"]], ["Ana Teste", "Beto Teste"])

    def test_weak_correlation_in_a_segment_cancels_only_that_segment(self):
        pauses = [{"paused_at": self.start + 4.5, "resumed_at": self.start + 5.5}]
        names = attribute([self.utt(1.1, 3.9), self.utt(4.6, 6.9)], self.snap(), self.start, pauses,
                             lags=[(0, 0.9), (0, 0.05)])
        self.assertEqual(list(names), [0])

    def test_pause_moves_later_utterances_to_the_right_speaker(self):
        pauses = [{"paused_at": self.start + 4.5, "resumed_at": self.start + 5.5}]
        # Depois da pausa de 1 s, o segundo 4,6 do arquivo é o 5,6 da chamada (Beto).
        names = attribute([self.utt(4.6, 6.9)], self.snap(), self.start, pauses)
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

    def test_periodic_signal_with_two_equal_lags_is_refused(self):
        # Blocos de 1 s de voz e 1 s de silêncio: atraso 0 e 2 s se encaixam igual.
        snap = {"frames": [f for k in range(-6, 30, 2) for f in frames(self.start + k, self.start + k + 1, 111)]}
        envelope = [0.3 if int(i * 0.1) % 2 == 0 else 0.0 for i in range(200)]
        lag, r = ms.estimate_lag(envelope, snap, self.start)
        self.assertEqual(r, 0.0)

    def test_each_segment_between_pauses_finds_its_own_lag(self):
        # Depois da pausa o arquivo ficou 1 s mais atrasado (áudio acumulado na volta).
        pauses = [{"paused_at": self.start + 4.5, "resumed_at": self.start + 5.0}]
        snap = {"frames": frames(self.start + 1, self.start + 3, 111) + frames(self.start + 7, self.start + 9, 222)
                + frames(self.start + 12, self.start + 14, 111)}
        envelope = []
        for i in range(140):
            off = i * 0.1
            wall = self.start + off if off < 4.5 else self.start + off + 0.5 - 1.0
            active = (1 <= wall - self.start < 3) or (7 <= wall - self.start < 9) or (12 <= wall - self.start < 14)
            envelope.append(0.3 if active else 0.0)
        (lag0, _), (lag1, r1) = ms.estimate_lags(envelope, snap, self.start, pauses)
        self.assertAlmostEqual(lag0, 0.0, delta=0.11)
        self.assertAlmostEqual(lag1, 1.0, delta=0.11)
        self.assertGreater(r1, 0.8)


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
                   {"type": "device", "t": begin, "device": ANA, "name": name},
                   {"type": "source", "t": begin, "device": ANA, "ssrc": 111},
                   {"type": "levels", "frames": frames(begin, end, 111, step=0.5)}]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n{cortada")

    def state(self, meeting=None):
        return {"started_at": self.start.isoformat(), "current_meeting": meeting or {}}

    def test_overlapping_session_is_cut_to_the_recording_window(self):
        self.session(CODE, self.t0 - 600, self.t0 + 60)
        snap = ms.snapshot(self.state(), self.t0 + 30, root=self.root)
        self.assertEqual(snap["meeting_code"], CODE)
        self.assertEqual(snap["devices"], {ANA: [[self.t0 - 600, "Ana Teste"]]})
        self.assertGreaterEqual(snap["frames"][0][0], self.t0 - ms.SNAPSHOT_MARGIN)
        self.assertLessEqual(snap["frames"][-1][0], self.t0 + 30 + ms.SNAPSHOT_MARGIN)

    def test_mapping_changes_after_the_recording_are_ignored(self):
        self.session(CODE, self.t0, self.t0 + 60)
        path = next(self.root.glob("*.jsonl"))
        with open(path, "a") as handle:
            handle.write("\n" + json.dumps({"type": "source", "t": self.t0 + 3600, "device": BETO, "ssrc": 111}) + "\n")
            handle.write(json.dumps({"type": "source", "t": self.t0 + 3600, "device": BETO, "ssrc": 555}) + "\n")
        snap = ms.snapshot(self.state(), self.t0 + 30, root=self.root)
        self.assertEqual([d for _, d in snap["sources"]["111"]], [ANA])
        self.assertEqual(snap["late_sources"], ["555"])

    def test_voices_during_a_pause_and_errors_are_accounted(self):
        self.session(CODE, self.t0, self.t0 + 60)
        path = next(self.root.glob("*.jsonl"))
        with open(path, "a") as handle:
            handle.write("\n" + json.dumps({"type": "error", "t": self.t0 + 10, "reason": "message"}) + "\n")
        state = dict(self.state(), pauses=[{"paused_at": self.t0 + 10, "resumed_at": self.t0 + 20}])
        snap = ms.snapshot(state, self.t0 + 30, root=self.root)
        self.assertFalse(any(self.t0 + 10 <= f[0] <= self.t0 + 20 for f in snap["frames"]))
        self.assertEqual(snap["errors"], 1)
        # Parou pausado: da pausa ao stop nada entra.
        stopped = dict(self.state(), pauses=[{"paused_at": self.t0 + 25, "resumed_at": None}])
        snap = ms.snapshot(stopped, self.t0 + 30, root=self.root)
        self.assertFalse(any(self.t0 + 25 <= f[0] <= self.t0 + 30 for f in snap["frames"]))

    def test_no_overlap_means_no_snapshot(self):
        self.session(CODE, self.t0 - 7200, self.t0 - 3600)
        self.assertIsNone(ms.snapshot(self.state(), self.t0 + 30, root=self.root))

    def test_two_calls_at_once_are_refused_even_with_the_event_code(self):
        # O gravador pega o som das duas chamadas; o código do evento não isola o áudio.
        self.session(CODE, self.t0, self.t0 + 60)
        self.session("xyz-wxyz-xyz", self.t0, self.t0 + 60, name="Outra Pessoa")
        meeting = {"conference_url": f"https://meet.google.com/{CODE}?authuser=0"}
        self.assertIsNone(ms.snapshot(self.state(meeting), self.t0 + 30, root=self.root))


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

    def test_running_chromium_is_found_with_rewritten_argv(self):
        proc = self.root / "proc" / "123"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"/usr/lib/chromium/chromium --ozone-platform=wayland --load-extension=/x,/ext/meet\0")
        with patch.object(ms, "Path", side_effect=lambda p="": self.root / "proc" if p == "/proc" else Path(p)):
            self.assertTrue(ms._running_chromium_has("/ext/meet"))
            self.assertFalse(ms._running_chromium_has("/outra"))

    def test_default_paths_follow_xdg_config_home(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            self.assertEqual(ms._flags_file(), self.root / "xdg" / "chromium-flags.conf")
            self.assertEqual(ms._native_manifest().parent, self.root / "xdg" / "chromium" / "NativeMessagingHosts")

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
            "participants": [{"name": "Ana Teste", "utterances": 3}, {"name": "Beto Teste", "utterances": 0},
                             {"name": "Participante 1", "utterances": 2, "named": False}]}})
        self.assertIn('speakers:\n  source: "google_meet_webrtc"', text)
        self.assertIn('- name: "Ana Teste"\n      named: true\n      presence: "observed"\n      speech: "meet_audio_level"', text)
        self.assertIn('- name: "Beto Teste"\n      named: true\n      presence: "observed"\n      speech: "not_observed"', text)
        # Pessoa real que o Meet não nomeou: fica marcada, para ninguém tratar o rótulo como nome.
        self.assertIn('- name: "Participante 1"\n      named: false\n      presence: "observed"', text)
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
                   {"type": "device", "t": t0 - 60, "device": ANA, "name": "Ana Teste"},
                   {"type": "device", "t": t0 - 60, "device": BETO, "name": "Beto Teste"},
                   {"type": "source", "t": t0 - 60, "device": ANA, "ssrc": 111},
                   {"type": "source", "t": t0 - 60, "device": BETO, "ssrc": 222},
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

    def test_deleting_the_recording_removes_who_spoke_and_restore_brings_it_back(self):
        from castanha.recording_exclusion import restore_recording
        engine = self.engine()
        slug = self.stop(engine)["result"]["slug"]
        bronze = engine.storage.bronze_dir / slug
        before = sorted(p.name for p in (bronze / ".speakers").iterdir())
        self.assertEqual(len(before), 2)
        name = engine.storage._read_bronze_metadata(slug)["recordings"][0]["filename"]
        from castanha.recording_exclusion import exclude_recording
        result = exclude_recording(slug, name, engine.storage)
        self.assertEqual(sorted(p.name for p in (bronze / ".speakers").iterdir()), [], result)
        self.assertIsNone(engine.storage._read_bronze_metadata(slug).get("speaker_attribution"))
        identity = engine.storage._read_bronze_metadata(slug)["exclusion_id"]
        restore_recording(slug, identity, engine.storage)
        self.assertEqual(sorted(p.name for p in (bronze / ".speakers").iterdir()), before)

    def test_voice_only_in_the_snapshot_margin_is_not_presence(self):
        # Carla fala 3 s antes do início (dentro da margem de 5 s) e nunca mais.
        meet = self.root / "state/castanha/meet"
        path = next(meet.glob("*.jsonl"))
        t0 = self.started.timestamp()
        carla = f"{SPACE}/devices/207"
        with open(path, "a") as handle:
            for record in ({"type": "device", "t": t0 - 60, "device": carla, "name": "Carla Teste"},
                           {"type": "source", "t": t0 - 60, "device": carla, "ssrc": 333},
                           {"type": "levels", "frames": frames(t0 - 4, t0 - 3, 333)}):
                handle.write(json.dumps(record) + "\n")
        engine = self.engine()
        slug = self.stop(engine)["result"]["slug"]
        names = [p["name"] for p in engine.storage._read_bronze_metadata(slug)["speaker_attribution"]["participants"]]
        self.assertEqual(names, ["Ana Teste", "Beto Teste"])

    def test_unmeasured_channel_is_not_cached_as_no_names(self):
        engine = self.engine()
        with patch.object(ms, "channel_envelope", return_value=None):
            slug = self.stop(engine)["result"]["slug"]
        bronze = engine.storage.bronze_dir / slug
        self.assertNotIn("(Meet)", (bronze / "transcript_raw.txt").read_text())
        self.assertEqual(list((bronze / ".speakers").glob("*.result.json")), [])

    def test_snapshot_is_taken_on_retry_if_the_stop_died_before_it(self):
        from castanha.transcription import TranscriptionResult, Utterance
        engine = self.engine()
        with patch.object(engine, "_save_meet_snapshot", side_effect=RuntimeError("queda")):
            with self.assertRaises(RuntimeError):
                self.stop(engine)
        slug = engine.state_mgr.read()["capture_slug"]
        bronze = engine.storage.bronze_dir / slug
        self.assertFalse((bronze / ".speakers").exists())
        engine.state_mgr.write({"status": "idle"})
        # Entrada de recuperação que não passa pelo stop: castanha retry.
        with patch("castanha.engine.get_transcriber") as provider, \
             patch.object(engine.summarizer, "generate_silver", return_value="## Resumo\n\nx"), \
             patch.object(engine.summarizer, "generate_gold", return_value={"facts": []}):
            provider.return_value.transcribe.side_effect = lambda path, mode="dual": (
                TranscriptionResult("oi", [Utterance("Falante", "oi", 0.2, 0.9)], "fixture", {})
                if int(Path(path).stem[-1]) == 0 else
                TranscriptionResult("ana fala beto fala", [Utterance("Falante", "ana fala", 1.1, 3.9),
                                                           Utterance("Falante", "beto fala", 5.1, 7.9)], "fixture", {}))
            engine.reprocess_meeting(slug)
        self.assertIn("Ana Teste (Meet): ana fala", (bronze / "transcript_raw.txt").read_text())

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


from tests.test_relocation import RelocationFixture  # noqa: E402


class RelocationTest(RelocationFixture):
    def test_moved_recording_takes_its_meet_timeline(self):
        from castanha.durability import file_sha256, write_json
        sha = file_sha256(self.storage.bronze_dir / self.a / "capture_job1.ogg")
        snap = {"version": 2, "devices": {ANA: "Ana Teste"}, "sources": {"111": [[0, ANA]]}, "frames": [[1.0, 111, 0.2]]}
        write_json(self.storage.bronze_dir / self.a / ".speakers" / f"{sha}.json", snap)
        self.assertEqual(self.move(to=self.b)["status"], "ok")
        moved = self.storage.bronze_dir / self.b / ".speakers" / f"{sha}.json"
        self.assertEqual(json.loads(moved.read_text()), snap)
        self.assertFalse((self.storage.bronze_dir / self.a / ".speakers" / f"{sha}.json").exists())
        self.assertNotIn("__speakers", json.dumps(self.jobs(self.b)))


class TeamsTest(unittest.TestCase):
    """Teams: a página entrega o mapa pronto; o resto do caminho é o mesmo do Meet."""

    TEAMS = "teams-0a1b2c3d"
    ANA_ID, BETO_ID = "8:orgid:0000-ana", "8:orgid:0000-beto"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "meet"
        self.now = 1_790_000_000.0
        self.session = ms.Session(recording_active=lambda: {"status": "recording"},
                                  clock=lambda: self.now, root=self.root, check_interval=0)

    def test_mapping_message_feeds_the_same_timeline(self):
        self.session.handle({"code": self.TEAMS, "type": "mapping", "t": self.now * 1000,
                             "devices": [[self.ANA_ID, "Ana Teste"], [self.BETO_ID, "Beto Teste"], ["x y", "inválido"]],
                             "sources": [[414, self.ANA_ID], [415, self.BETO_ID], [True, self.ANA_ID], [0, self.ANA_ID]]})
        self.session.handle({"code": self.TEAMS, "type": "levels",
                             "frames": [[(self.now + t) * 1000, 414, 0.2] for t in (1.0, 1.1)]})
        self.session.close()
        records = [json.loads(line) for line in next(self.root.glob("*.jsonl")).read_text().splitlines()]
        self.assertEqual(sorted(r["device"] for r in records if r["type"] == "device"), [self.ANA_ID, self.BETO_ID])
        self.assertEqual(sorted(r["ssrc"] for r in records if r["type"] == "source"), [414, 415])

    def test_mapping_is_ignored_on_a_meet_session(self):
        self.session.handle({"code": CODE, "type": "mapping", "t": self.now * 1000,
                             "devices": [[self.ANA_ID, "Ana Teste"]], "sources": [[414, self.ANA_ID]]})
        self.assertEqual(self.session.devices, {})

    def test_revoked_source_competes_without_a_name(self):
        start = self.now
        snap = {"meeting_code": self.TEAMS, "devices": {self.ANA_ID: [[0, "Ana Teste"]], self.BETO_ID: [[0, "Beto Teste"]]},
                "sources": {"414": [[0, self.ANA_ID], [start + 5, None]], "415": [[0, self.BETO_ID]]},
                "frames": frames(start + 6, start + 9, 414) + frames(start + 6, start + 7, 415)}
        self.assertEqual(attribute([{"origin": "audio_sistema", "start": 6.1, "end": 8.9}], snap, start), {})

    def test_teams_has_no_mix_source(self):
        # Fonte sem vínculo sempre junto de Ana (mesmo nível artificial): no Teams é gente.
        start = self.now
        talk = [f for k in range(0, 60, 8) for f in frames(start + k + 1, start + k + 4, 414)]
        snap = {"meeting_code": self.TEAMS, "devices": {self.ANA_ID: [[0, "Ana Teste"]]}, "sources": {"414": [[0, self.ANA_ID]]},
                "frames": talk + [[t, 999, lv] for t, _, lv in talk]}
        self.assertEqual(attribute([{"origin": "audio_sistema", "start": 1.1, "end": 3.9}], snap, start), {})

    def test_presence_signal_passes_local_check_when_the_channel_has_sound(self):
        start = self.now
        snap = {"meeting_code": self.TEAMS, "devices": {self.ANA_ID: [[0, "Ana Teste"]]}, "sources": {"414": [[0, self.ANA_ID]]},
                "frames": frames(start + 0, start + 20, 414), "presence_only": True}
        utt = [{"origin": "audio_sistema", "start": 5.0, "end": 9.0}]
        # Com nível medido (não só presença), vale a conferência por amplitude.
        measured = dict(snap, presence_only=False)
        self.assertEqual(ms.attribute(utt, measured, start, lags=[(0, 1)], envelope=[0.2 + 0.1 * ((i // 2) % 2) for i in range(200)]), {})
        loud = [0.2 + 0.1 * ((i // 2) % 2) for i in range(200)]
        self.assertEqual(ms.label_voices(ms.attribute(utt, snap, start, lags=[(0, 1)], envelope=loud))[0]["name"], "Ana Teste")
        self.assertEqual(ms.attribute(utt, snap, start, lags=[(0, 1)], envelope=[0.0] * 200), {})

    def test_codes_and_labels(self):
        self.assertTrue(ms.MEET_CODE_RE.match(self.TEAMS))
        self.assertFalse(ms.MEET_CODE_RE.match("teams-xyz"))
        self.assertEqual((ms.label(self.TEAMS), ms.label(CODE)), ("Teams", "Meet"))
        self.assertEqual(ms.evidence_source(self.TEAMS), "microsoft_teams_webrtc")

    def test_teams_names_are_rendered_with_their_own_tag(self):
        from castanha.durability import write_json
        bronze = Path(self.temp.name) / "bronze"
        start = self.now
        snap = {"version": 2, "meeting_code": self.TEAMS, "decoder": ms.DECODER_VERSION,
                "devices": {self.ANA_ID: [[0, "Ana Teste"]]}, "sources": {"414": [[0, self.ANA_ID]]},
                "frames": frames(start + 1, start + 4, 414), "recording": [start, start + 10]}
        sha = "a" * 64
        write_json(bronze / ".speakers" / f"{sha}.json", snap)
        job = {"id": "j", "sha256": sha, "channel_provenance": True, "recorded_at": start, "audio_path": "x",
               "utterances": [{"speaker": "Áudio do sistema", "text": "oi", "start": 1.1, "end": 3.9,
                               "channel": 1, "origin": "audio_sistema"}]}
        envelope = [0.3 if 1 <= i * 0.1 < 4 else 0.0 for i in range(100)]
        result = ms.attribution_for_job(bronze, job, envelope_fn=lambda _: envelope)
        self.assertIn("Ana Teste (Teams): oi", result["text"])
        self.assertEqual(result["source"], "microsoft_teams_webrtc")
        self.assertEqual(ms.annotate(job["utterances"], result)[0]["attribution_source"], "microsoft_teams_webrtc")


class TeamsPageScriptTest(unittest.TestCase):
    """page.js num Teams simulado: mapa pronto e níveis por presença da fonte."""

    def test_page_script_sends_teams_mapping_and_levels(self):
        harness = Path(__file__).resolve().parent / "meet_page_harness.js"
        out = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=30, check=True)
        posted_all = json.loads(out.stdout)
        codes = [m.get("code") for m in posted_all]
        first = codes[0]
        self.assertEqual(len(set(codes)), 2)
        posted = [m for m in posted_all if m.get("code") == first]
        second = [m for m in posted_all if m.get("code") != first]
        # Reunião nova recomeça do zero: só Carla, sem nada da anterior.
        second_devices = {d for m in second if m["type"] == "mapping" for d, _ in m["devices"]}
        self.assertEqual(second_devices, {"8:orgid:carla"})
        code = first
        self.assertTrue(ms.MEET_CODE_RE.match(code) and code.startswith("teams-"))
        mappings = [m for m in posted if m["type"] == "mapping"]
        devices = {d for m in mappings for d, _ in m["devices"]}
        history = [(s, o) for m in mappings for s, o in m["sources"]]
        self.assertEqual(devices, {"8:orgid:ana", "8:orgid:beto"})
        self.assertEqual(history, [(414, "8:orgid:ana"), (415, "8:orgid:beto"), (415, None), (414, None)])
        frames_ = [f for m in posted if m["type"] == "levels" for f in m["frames"]]
        self.assertEqual({f[1] for f in frames_}, {414, 415})
        self.assertTrue(all(f[2] == 0.2 for f in frames_))
        self.assertTrue(all(m.get("presence") for m in posted if m["type"] == "levels" and m["frames"]))
        # O que a página manda o host aceita e transforma na mesma linha do tempo.
        with tempfile.TemporaryDirectory() as tmp:
            session = ms.Session(recording_active=lambda: {"status": "recording"}, clock=lambda: 1790000005.0,
                                 root=Path(tmp), check_interval=0)
            for message in posted:
                session.handle(message)
            session.close()
            text = next(Path(tmp).glob("*.jsonl")).read_text()
            self.assertIn("Beto Teste", text)
            self.assertIn('"ssrc": 415', text)


class ScreenNamesTest(unittest.TestCase):
    """Nome lido da tela do Meet: cobre quem já estava na chamada, que o canal não anuncia."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "meet"
        self.now = 1_790_000_000.0
        self.session = ms.Session(recording_active=lambda: {"status": "recording"}, clock=lambda: self.now,
                                  root=self.root, check_interval=0)

    def send(self, **message):
        self.session.handle({"code": CODE, **message})

    def test_page_reads_the_name_shown_on_each_tile_and_skips_the_self_label(self):
        harness = Path(__file__).resolve().parent / "meet_screen_harness.js"
        out = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=30, check=True)
        posted = json.loads(out.stdout)
        self.assertEqual([m["devices"] for m in posted], [[[ANA, "Ana Teste"]], [[f"{SPACE}/devices/206", "Beto Teste"]]])
        self.assertEqual(posted[-1]["probe"], {"nodes": 4, "devices": 3, "named": 2})
        # O que a página manda o host aceita.
        for message in posted:
            self.session.handle(message | {"code": CODE})
        self.assertEqual(self.session.devices[ANA][0], "Ana Teste")

    def test_screen_name_fills_a_device_the_channel_never_named(self):
        self.send(type="collections", t=self.now * 1000, b64=base64.b64encode(source_message(ANA, 111)).decode())
        self.send(type="screen", t=self.now * 1000, devices=[[ANA, "Ana Teste"]], probe={"nodes": 2, "devices": 1, "named": 1})
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.send(type="bye")
        records = [json.loads(line) for line in next(self.root.glob("*.jsonl")).read_text().splitlines()]
        device = next(r for r in records if r["type"] == "device")
        self.assertEqual((device["name"], device.get("via")), ("Ana Teste", "screen"))

    def test_channel_name_wins_over_the_screen_and_the_screen_never_replaces_it(self):
        self.send(type="screen", t=self.now * 1000, devices=[[ANA, "Ana da Tela"]])
        self.send(type="collections", t=self.now * 1000, b64=base64.b64encode(device_message(ANA, "Ana Teste")).decode())
        self.assertEqual(self.session.devices[ANA][0], "Ana Teste")
        self.send(type="screen", t=self.now * 1000, devices=[[ANA, "Outra Coisa"]])
        self.assertEqual(self.session.devices[ANA][0], "Ana Teste")

    def test_screen_message_with_anything_but_a_meet_device_is_dropped(self):
        self.send(type="screen", t=self.now * 1000,
                  devices=[["8:orgid:ana", "Ana"], [ANA, ""], [ANA, "linha\nquebrada"], ["x", "y", "z"], 7])
        self.assertEqual(self.session.devices, {})


class CoverageTest(unittest.TestCase):
    """O que as 25 reuniões reais de set e out/2026 mostraram que derrubava a atribuição."""

    start = 1_790_000_000.0

    def utt(self, start, end, origin="audio_sistema", text="fala"):
        return {"speaker": "Áudio do sistema" if origin == "audio_sistema" else "Microfone local", "text": text,
                "start": start, "end": end, "channel": 1 if origin == "audio_sistema" else 0, "origin": origin}

    def test_meet_mix_is_ignored_even_in_a_short_recording(self):
        # Gravação curta não dá estatística para reconhecer a mistura: a fonte 42 sem dono já basta.
        talk = frames(self.start + 1, self.start + 4, 111)
        snap = {"devices": {ANA: "Ana Teste"}, "sources": {"111": [[0, ANA]]},
                "frames": talk + [[t, 42, level] for t, _, level in talk]}
        self.assertEqual(ms.mix_sources(snap), {"42"})
        self.assertEqual(attribute([self.utt(1.1, 3.9)], snap, self.start)[0]["name"], "Ana Teste")
        # A mesma fonte 42 com dono é participante, não mistura.
        owned = dict(snap, sources={"111": [[0, ANA]], "42": [[0, BETO]]})
        self.assertEqual(ms.mix_sources(owned), set())

    def test_unowned_source_that_mirrors_everyone_is_the_mix(self):
        base = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]}}
        talk = [f for k in range(0, 80, 8) for f in frames(self.start + k + 1, self.start + k + 4, 111)
                + frames(self.start + k + 5, self.start + k + 8, 222)]
        mirror = [[t, 4242, level] for t, _, level in talk]
        self.assertEqual(ms.mix_sources(dict(base, frames=talk + mirror)), {"4242"})

    def test_lag_that_drifts_along_the_recording_is_followed_window_by_window(self):
        # Ana e Beto alternam 2 s cada, com silêncio irregular; a gravação começa 0,2 s
        # atrasada e termina 1,0 s atrasada (medido: de -0,3 a -1,1 s numa reunião de 40 min).
        total = 600.0
        fr, turns, t, k = [], [], 2.0, 0
        while t < total - 5:
            who = 111 if k % 2 == 0 else 222
            fr += frames(self.start + t, self.start + t + 2.0, who, step=0.1)
            turns.append((t, t + 2.0, "Ana Teste" if who == 111 else "Beto Teste"))
            t += 2.0 + 0.7 + (k * 37 % 11) / 10.0
            k += 1
        snap = {"devices": {ANA: "Ana Teste", BETO: "Beto Teste"}, "sources": {"111": [[0, ANA]], "222": [[0, BETO]]}, "frames": fr}
        delay = lambda offset: 0.2 + 0.8 * offset / total  # noqa: E731
        envelope = []
        for i in range(int(total / 0.1)):
            offset = i * 0.1
            wall = offset - delay(offset)  # o som deste instante do arquivo saiu do Meet antes
            envelope.append(0.3 if any(a <= wall < b for a, b, _ in turns) else 0.0)
        track = ms.lag_track(envelope, snap, self.start)
        lags = [lag for _, _, lag, _ in track[0]]
        self.assertAlmostEqual(lags[0], 0.25, delta=0.25)
        self.assertAlmostEqual(lags[-1], 0.95, delta=0.25)
        # Fala do fim da gravação: com um atraso só para o arquivo inteiro cairia na pessoa errada.
        last = [(a, b, who) for a, b, who in turns if a > 560][0]
        utterance = self.utt(last[0] + delay(last[0]) + 0.1, last[1] + delay(last[1]) - 0.1)
        named = ms.label_voices(ms.attribute([utterance], snap, self.start, envelope=envelope, track=track))
        self.assertEqual(named[0]["name"], last[2])

    def test_window_without_alignment_leaves_its_utterances_unnamed(self):
        snap = {"devices": {ANA: "Ana Teste"}, "sources": {"111": [[0, ANA]]}, "frames": frames(self.start + 1, self.start + 4, 111)}
        self.assertEqual(ms.attribute([self.utt(1.1, 3.9)], snap, self.start, track=[[]]), {})
        self.assertEqual(ms.attribute([self.utt(1.1, 3.9)], snap, self.start, track=[[(0.0, 10.0, 0.0, 0.1)]]), {})

    def job(self, bronze, snap, utterances):
        from castanha.durability import write_json
        sha = "b" * 64
        write_json(bronze / ".speakers" / f"{sha}.json", snap)
        return {"id": "j", "sha256": sha, "channel_provenance": True, "recorded_at": self.start, "audio_path": "x",
                "utterances": utterances}

    def test_result_counts_named_and_numbered_participants_and_names_the_microphone_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            bronze = Path(tmp) / "bronze"
            snap = {"version": 2, "meeting_code": CODE, "decoder": ms.DECODER_VERSION,
                    "devices": {ANA: [[self.start + 500, "Ana Teste"]]},
                    "sources": {"111": [[0, ANA]], "222": [[0, BETO]]},
                    "frames": frames(self.start + 1, self.start + 4, 111) + frames(self.start + 5, self.start + 8, 222),
                    "recording": [self.start, self.start + 10]}
            job = self.job(bronze, snap, [self.utt(1.1, 3.9, text="oi"), self.utt(5.2, 7.8, text="tudo bem"),
                                          self.utt(8.5, 9.5, origin="microfone_local", text="tudo")])
            envelope = [0.3 if 1 <= i * 0.1 < 4 or 5 <= i * 0.1 < 8 else 0.0 for i in range(100)]
            result = ms.attribution_for_job(bronze, job, envelope_fn=lambda _: envelope, owner_name="Dona Teste")
            self.assertEqual((result["remote_utterances"], result["attributed"], result["named"]), (2, 2, 1))
            self.assertEqual(result["participants"], [{"name": "Ana Teste", "utterances": 1, "named": True},
                                                      {"name": "Participante 1", "utterances": 1, "named": False}])
            self.assertIn("Ana Teste (Meet): oi", result["text"])
            self.assertIn("Participante 1 (Meet): tudo bem", result["text"])
            self.assertIn("Dona Teste (microfone): tudo", result["text"])
            annotated = ms.annotate(job["utterances"], result)
            self.assertEqual([(u.get("attributed_name"), u.get("attribution_source"), u.get("attribution_named")) for u in annotated],
                             [("Ana Teste", "google_meet_webrtc", None), ("Participante 1", "google_meet_webrtc", False),
                              ("Dona Teste", "local_microphone_owner", None)])
            # Sem dono configurado o microfone segue com o rótulo do canal.
            plain = ms.attribution_for_job(bronze, job, envelope_fn=lambda _: envelope)
            self.assertIn("Microfone local: tudo", plain["text"])

    def test_microphone_owner_is_not_named_when_the_meet_identified_nobody(self):
        with tempfile.TemporaryDirectory() as tmp:
            bronze = Path(tmp) / "bronze"
            snap = {"version": 2, "meeting_code": CODE, "decoder": ms.DECODER_VERSION, "devices": {}, "sources": {},
                    "frames": frames(self.start + 20, self.start + 21, 111), "recording": [self.start, self.start + 30]}
            job = self.job(bronze, snap, [self.utt(1.1, 3.9), self.utt(5.0, 6.0, origin="microfone_local")])
            envelope = [0.3 if 20 <= i * 0.1 < 21 else 0.0 for i in range(300)]
            self.assertIsNone(ms.attribution_for_job(bronze, job, envelope_fn=lambda _: envelope, owner_name="Dona Teste"))


class MissingCaptureTest(unittest.TestCase):
    """Chamada do Meet gravada sem sinal da extensão: avisa uma vez e deixa o motivo no Bronze."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bronze = self.root / "bronze"
        self.bronze.mkdir()
        self.sha = "c" * 64
        self.job_file = self.root / "job.json"

    def save(self, meeting):
        from castanha import engine as eng
        now = time.time()
        self.job_file.write_text(json.dumps({"sha256": self.sha, "stopped_at": now - 10,
                                             "state": {"started_at": now - 600, "current_meeting": meeting}}))
        instance = eng.CastanhaEngine.__new__(eng.CastanhaEngine)
        instance.config = {"meet": {"speakers_enabled": True}}
        with patch.object(ms, "sessions_dir", return_value=self.root / "sem-sessoes"), \
                patch.object(eng, "notify") as notify:
            instance._save_meet_snapshot(self.bronze, self.job_file)
        return notify.call_count

    def test_snapshot_says_why_there_is_nothing(self):
        why = []
        self.assertIsNone(ms.snapshot({"started_at": 1_790_000_000.0}, 1_790_000_600.0, root=self.root / "vazio", why=why))
        self.assertEqual(why, ["no_session"])

    def test_meet_call_without_extension_signal_warns_once(self):
        meeting = {"conference_url": "https://meet.google.com/abc-defg-hij"}
        self.assertEqual(self.save(meeting), 1)
        marker = self.bronze / ".speakers" / f"{self.sha}.missing.json"
        self.assertEqual(json.loads(marker.read_text())["reason"], "no_session")
        self.assertEqual(self.save(meeting), 0)  # a retomada chama de novo: não repete o aviso

    def test_recording_that_was_not_a_meet_call_does_not_warn(self):
        self.assertEqual(self.save({"title": "Reunião presencial"}), 0)
        self.assertFalse((self.bronze / ".speakers").exists())


class TrackAudioTest(unittest.TestCase):
    """Trilhas de áudio separadas: o host pede a gravação à página e guarda os pedaços na ordem."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "meet"
        self.now = 1_790_000_000.0
        self.status = "recording"
        self.sent = []
        self.session = self.make(True)

    def make(self, track_audio):
        return ms.Session(recording_active=lambda: {"status": self.status} if self.status else None, clock=lambda: self.now,
                          root=self.root, check_interval=0, track_audio=track_audio, send=self.sent.append)

    def audio(self, seq, data, track=0, started=None, session=None):
        (session or self.session).handle({"code": CODE, "type": "audio", "t": self.now * 1000, "track": track,
                                          "started": (started or self.now) * 1000, "seq": seq,
                                          "b64": base64.b64encode(data).decode()})

    def files(self):
        return sorted(self.root.glob("*.audio/*.webm"))

    def test_page_is_asked_to_record_only_while_castanha_records(self):
        self.session.handle({"code": CODE, "type": "levels", "frames": []})
        self.status = "paused"
        self.session.handle({"code": CODE, "type": "levels", "frames": []})
        self.status = "recording"
        self.session.handle({"code": CODE, "type": "levels", "frames": []})
        self.status = None
        self.session.handle({"code": CODE, "type": "levels", "frames": []})
        self.assertEqual(self.sent, [{"type": "capture", "on": True}, {"type": "capture", "on": False},
                                     {"type": "capture", "on": True}, {"type": "capture", "on": False}])

    def test_option_off_never_asks_and_never_stores(self):
        self.sent.clear()
        off = self.make(False)
        off.handle({"code": CODE, "type": "levels", "frames": []})
        self.audio(0, b"abc", session=off)
        self.assertEqual(self.sent, [{"type": "capture", "on": False}])
        self.assertEqual(self.files(), [])

    def test_chunks_are_stored_in_recording_order_in_a_private_file_per_track(self):
        self.audio(1, b"BBB")           # chegou antes do primeiro
        self.audio(0, b"AAA")
        self.audio(0, b"AAA")           # repetido
        self.audio(2, b"CCC")
        self.audio(0, b"xyz", track=1)  # outra trilha, outro arquivo
        self.session.close()
        files = self.files()
        self.assertEqual([f.name for f in files], [f"track0-{int(self.now * 1000)}.webm", f"track1-{int(self.now * 1000)}.webm"])
        self.assertEqual(files[0].read_bytes(), b"AAABBBCCC")
        self.assertEqual(files[1].read_bytes(), b"xyz")
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].parent.stat().st_mode & 0o777, 0o700)
        records = [json.loads(line) for line in next(self.root.glob("*.jsonl")).read_text().splitlines()]
        self.assertEqual([(r["track"], r["first_seq"]) for r in records if r["type"] == "audio"], [(0, 1), (1, 0)])

    def test_nothing_is_stored_when_castanha_is_not_recording_and_bad_chunks_are_dropped(self):
        self.status = None
        self.audio(0, b"AAA")
        self.assertEqual(self.files(), [])
        self.status = "recording"
        self.session.handle({"code": CODE, "type": "audio", "track": 0, "started": self.now * 1000, "seq": 0, "b64": "!!!"})
        self.session.handle({"code": CODE, "type": "audio", "track": 99, "started": self.now * 1000, "seq": 0, "b64": "QUFB"})
        self.session.handle({"code": CODE, "type": "audio", "track": 0, "started": "x", "seq": 0, "b64": "QUFB"})
        self.assertEqual(self.files(), [])

    def test_last_chunk_right_after_the_stop_still_closes_the_file(self):
        self.audio(0, b"AAA")
        state = {"status": "processing", "stopped_at": self.now}
        stopping = ms.Session(recording_active=lambda: state, clock=lambda: self.now, root=self.root, check_interval=0,
                              track_audio=True, send=self.sent.append)
        self.session.close()
        self.audio(0, b"AAA", session=stopping)
        self.audio(1, b"FIM", session=stopping)
        self.now += ms.STOP_GRACE + 1   # passada a folga, mais nada entra
        self.audio(2, b"tarde", session=stopping)
        stopping.close()
        self.assertTrue(any(f.read_bytes().endswith(b"AAAFIM") for f in self.files()))
        self.assertFalse(any(b"tarde" in f.read_bytes() for f in self.files()))

    def test_levels_by_track_are_recorded_next_to_the_levels(self):
        ts = self.now * 1000
        self.session.handle({"code": CODE, "type": "levels", "frames": [[ts, 111, 0.2]],
                             "tracks": [[ts, 0, 111, 0.2], [ts, 99, 111, 0.2], [ts, 0, 111], "x"]})
        self.session.close()
        records = [json.loads(line) for line in next(self.root.glob("*.jsonl")).read_text().splitlines()]
        self.assertEqual([r["frames"] for r in records if r["type"] == "track_levels"], [[[self.now, 0, 111, 0.2]]])

    def test_old_audio_folders_are_pruned_with_the_sessions(self):
        self.audio(0, b"AAA")
        self.session.close()
        folder = self.files()[0].parent
        with patch.object(ms, "sessions_dir", return_value=self.root):
            ms.prune(now=time.time() + 8 * 86400)
        self.assertFalse(folder.exists())


class RecordReminderTest(unittest.TestCase):
    """Lembrete de gravação: uma vez por chamada, só depois de entrar e só sem gravação."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "meet"
        self.now = 1_790_000_000.0
        self.status = None
        self.calls = []
        self.session = ms.Session(recording_active=lambda: {"status": self.status} if self.status else None,
                                  clock=lambda: self.now, root=self.root, check_interval=0,
                                  remind=lambda code, signal: self.calls.append((code, signal)))

    def send(self, **message):
        self.session.handle({"code": CODE, **message})

    def test_lobby_and_left_page_messages_are_not_a_join(self):
        # A página manda `levels` vazio a cada segundo em qualquer URL com código: sala de espera e "você saiu".
        for message in ({"type": "levels", "frames": []}, {"type": "levels"}, {"type": "screen", "devices": []},
                        {"type": "bye"}, {"type": "replay_incomplete"}, {"type": "audio"}, {"type": "mapping", "devices": []}):
            self.send(**message)
        self.session.handle("lixo")
        fresh = ms.Session(recording_active=lambda: None, clock=lambda: self.now, root=self.root, check_interval=0,
                           remind=lambda code, signal: self.calls.append((code, signal)))
        fresh.handle({"type": "collections", "b64": ""})  # sem código de chamada não há sessão
        fresh.handle({"code": "https://meet.google.com", "type": "levels", "frames": [[1, 1, 0.2]]})
        self.assertEqual(self.calls, [])

    def test_first_join_signal_reminds_once_per_tab(self):
        self.send(type="levels", frames=[])
        self.send(type="collections", t=self.now * 1000, b64=base64.b64encode(device_message(ANA, "Ana Teste")).decode())
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.send(type="collections", b64="!!!")
        self.assertEqual(self.calls, [(CODE, "collections")])

    def test_voice_arriving_or_a_broken_participant_message_also_count_as_joined(self):
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.assertEqual(self.calls, [(CODE, "levels")])
        other = ms.Session(recording_active=lambda: None, clock=lambda: self.now, root=self.root, check_interval=0,
                           remind=lambda code, signal: self.calls.append((code, signal)))
        other.handle({"code": CODE, "type": "collections", "b64": "!!!"})
        self.assertEqual(self.calls[1:], [(CODE, "collections")])

    def test_joining_while_recording_or_paused_never_reminds_even_after_the_stop(self):
        for status in ("recording", "paused"):
            self.status = status
            session = ms.Session(recording_active=lambda: {"status": self.status}, clock=lambda: self.now,
                                 root=self.root, check_interval=0, remind=lambda code, signal: self.calls.append(code))
            session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 111, 0.2]]})
            self.status = None
            session.handle({"code": CODE, "type": "levels", "frames": [[self.now * 1000, 111, 0.2]]})
        self.assertEqual(self.calls, [])

    def test_previous_recording_still_processing_is_not_recording(self):
        self.status = "processing"
        self.send(type="levels", frames=[[self.now * 1000, 111, 0.2]])
        self.assertEqual(self.calls, [(CODE, "levels")])

    def test_failing_reminder_never_breaks_the_capture(self):
        def boom(code, signal):
            raise RuntimeError("sem notificação")
        session = ms.Session(recording_active=lambda: {"status": "recording"} if self.status else None,
                             clock=lambda: self.now, root=self.root, check_interval=0, remind=boom)
        session.handle({"code": CODE, "type": "collections", "t": self.now * 1000,
                        "b64": base64.b64encode(device_message(ANA, "Ana Teste")).decode()})
        self.assertEqual({d: v[0] for d, v in session.devices.items()}, {ANA: "Ana Teste"})

    def test_reminder_rechecks_recording_before_notifying_and_before_starting(self):
        started, shown, slept = [], [], []
        state = {"status": None}
        active = lambda: {"status": state["status"]} if state["status"] else None  # noqa: E731

        def run(answer, during=None):
            def notification(code):
                shown.append(code)
                if during:
                    state["status"] = during
                return answer
            return ms.remind(CODE, notification=notification, start=lambda: started.append(1),
                             recording_active=active, sleep=slept.append)

        self.assertEqual((run("record"), started, shown), ("started", [1], [CODE]))
        self.assertEqual(slept, [ms.REMINDER_WAIT])
        for answer in ("", "Wait timeout expired", "other"):
            self.assertEqual(run(answer), "dismissed")
        self.assertEqual(run("record", during="recording"), "recording")  # começou a gravar pelo painel antes do clique
        self.assertEqual(started, [1])
        state["status"] = "paused"
        before = len(shown)
        self.assertEqual((run("record"), len(shown)), ("recording", before))  # já gravando: nem aparece
        state["status"] = "processing"
        self.assertEqual(run(""), "dismissed")  # gravação anterior fechando: o lembrete aparece

    def test_spawn_skips_a_second_reminder_for_the_same_call_and_never_shares_stdio(self):
        spawned = []
        with patch.object(ms, "sessions_dir", return_value=self.root), \
             patch.object(ms.subprocess, "Popen", side_effect=lambda *a, **k: spawned.append((a[0], k))):
            ms.spawn_reminder(CODE, "collections")
            ms.spawn_reminder(CODE, "levels")           # aba recarregada no meio da chamada
            ms.spawn_reminder("zzz-zzzz-zzz", "levels")  # outra chamada
            marker = self.root / f".reminded-{CODE}"
            os.utime(marker, (time.time() - ms.REMINDER_GAP - 5,) * 2)
            ms.spawn_reminder(CODE, "levels")           # a mesma daily, outro dia
        self.assertEqual([call[0][-2:] for call in spawned], [["remind", CODE], ["remind", "zzz-zzzz-zzz"], ["remind", CODE]])
        for _command, options in spawned:
            self.assertEqual((options["stdin"], options["stdout"], options["stderr"], options["start_new_session"]),
                             (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL, True))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        ms.prune(now=time.time() + (ms.RETENTION_DAYS + 1) * 86400)

    def test_host_arguments_from_the_browser_never_reach_the_reminder(self):
        with patch.object(ms, "remind") as remind, patch.object(ms, "run_host", return_value=0) as host:
            for argv in (["chrome-extension://fmiffekiigfbnmidnclnpfhndjbnlegj/"], [], ["remind"], ["remind", "../x"],
                         ["remind", CODE, "extra"], ["chrome-extension://x/", "--parent-window=0"]):
                self.assertEqual(ms.main(argv), 0)
            self.assertEqual((remind.call_count, host.call_count), (0, 6))
            self.assertEqual(ms.main(["remind", CODE]), 0)
            remind.assert_called_once_with(CODE)

    def test_record_button_runs_the_same_start_command_as_the_panel(self):
        with patch.object(ms.subprocess, "run") as run:
            ms._start_recording()
        command, options = run.call_args[0][0], run.call_args[1]
        self.assertEqual((command[1], command[3:]), ("-B", ["start"]))
        self.assertTrue(Path(command[2]).is_file() and Path(command[2]).name == "castanha")
        self.assertEqual((options["stdin"], options["stdout"], options["stderr"]), (subprocess.DEVNULL,) * 3)

    def test_config_switch_and_default(self):
        self.assertTrue(ms.reminder_enabled({"meet": {}}))
        self.assertTrue(ms.reminder_enabled({}))
        self.assertFalse(ms.reminder_enabled({"meet": {"record_reminder": False}}))
        self.assertFalse(ms.reminder_enabled({"meet": {"record_reminder": "yes"}}))
