"""Quem fala no Google Meet: linha do tempo por participante, gravada só durante a gravação.

A extensão em `browser/meet-speakers` roda dentro da página do Meet e repassa
duas coisas por native messaging: as mensagens do canal WebRTC "collections"
(que ligam o dispositivo ao nome e à fonte de áudio) e o nível de áudio de cada
fonte contribuinte (CSRC), com o instante do pacote. Este módulo é o host
nativo que recebe isso, o recorte que vai para o Bronze ao parar a gravação e a
atribuição de cada fala do canal do sistema ao participante que falava.

Nada disso sai da máquina. O host só grava enquanto o Castanha grava; fora
disso ele guarda em memória o mapa dispositivo/nome e descarta os níveis.
O formato do Meet não é documentado: qualquer falha de leitura deixa a
transcrição como está (rótulo do canal), nunca inventa um nome.
"""

import base64
import bisect
import datetime as _dt
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HOST_NAME = "io.github.brunoomoniz.castanha.meet"
EXTENSION_ID = "fmiffekiigfbnmidnclnpfhndjbnlegj"
DECODER_VERSION = "meet-collections-v1"
ALGORITHM_VERSION = "meet-speakers-v2"
EVIDENCE_SOURCE = "google_meet_webrtc"
TEAMS_EVIDENCE_SOURCE = "microsoft_teams_webrtc"
EVIDENCE_SOURCES = (EVIDENCE_SOURCE, TEAMS_EVIDENCE_SOURCE)

# Meet: código da sala. Teams: "teams-" + hash do thread da reunião (feito na página).
MEET_CODE_RE = re.compile(r"^(?:[a-z]{3,}-[a-z]{3,}-[a-z]{3,}|teams-[0-9a-f]{8})$")
TEAMS_ID_RE = re.compile(r"^[\w:.@-]{1,200}$")


def platform(code: Optional[str]) -> str:
    return "teams" if isinstance(code, str) and code.startswith("teams-") else "meet"


def evidence_source(code: Optional[str]) -> str:
    return TEAMS_EVIDENCE_SOURCE if platform(code) == "teams" else EVIDENCE_SOURCE


def label(code: Optional[str]) -> str:
    return "Teams" if platform(code) == "teams" else "Meet"
DEVICE_RE = re.compile(r"^spaces/[\w-]+/devices/\d+$")
MAX_MESSAGE_BYTES = 1 << 20
# A extensão manda níveis a cada 1 s: o recorte espera o último lote.
STOP_GRACE = 5.0
FINAL_BATCH_WAIT = 1.5
MAX_INFLATED_BYTES = 4 << 20
RETENTION_DAYS = 7
# Nível linear (0..1, convertido de dBov pelo navegador). Fala medida no teste
# de 23/09/2026 ficou entre 0,14 e 0,28; fonte calada, 0.
SPEAKING_LEVEL = 0.05
# Voz baixa (microfone fraco) não dá nome a ninguém, mas concorre.
COMPETING_LEVEL = 0.01  # o piso que a extensão manda: tudo que chega concorre
FRAME_SPAN = 0.12
MERGE_GAP = 0.8
SNAPSHOT_MARGIN = 5.0
# Atribuição: a fala precisa estar coberta pelo participante e ele precisa
# dominar o intervalo; empate ou cobertura rala mantém o rótulo do canal.
EDGE_TOLERANCE = 0.3
MIN_COVERAGE = 0.5
DOMINANCE = 3.0
# Soma das outras vozes (com ou sem nome) no intervalo, como fração dele.
MAX_OTHER_SHARE = 0.15
# Ajuste fino do relógio: a correlação entre o canal do sistema e os níveis
# do Meet acha o atraso da gravação. Correlação fraca = sessão errada ou
# áudio de outra fonte, e aí nada é atribuído.
LAG_WINDOW = 3.0
LAG_STEP = 0.1
MIN_CORRELATION = 0.2
REMOTE_ORIGIN = "audio_sistema"
LOCAL_ORIGIN = "microfone_local"
OWNER_SOURCE = "local_microphone_owner"
MIX_MARGIN = 0.02
# A mistura acompanha TODA a fala com dono (100% nas 3 sessões reais medidas).
MIX_MIN_COVERAGE = 0.8
MIX_MIN_EVIDENCE = 300  # pacotes de 0,1 s com dono: 30 s de fala para concluir
MAX_SNAPSHOT_BYTES = 16 << 20
# Trilhas de áudio separadas: pedaços de 2 s em Opus (uns 8 kB cada).
MAX_AUDIO_CHUNK_BYTES = 512 << 10
MAX_AUDIO_SESSION_BYTES = 1 << 30   # teto por sessão: muito acima de 3 trilhas por 8 h
MAX_AUDIO_TRACKS = 16
MAX_AUDIO_PENDING = 50              # pedaços fora de ordem guardados por arquivo
# Outras vozes: no máximo 15% do intervalo e nunca mais que 1 s (uma frase
# curta de outra pessoa já é uma fala dela).
MAX_OTHER_SECONDS = 1.0
# Conferência local: o canal gravado precisa acompanhar o Meet naquela fala.
LOCAL_MARGIN = 1.0
LOCAL_MIN_BINS = 20
LOCAL_CORRELATION = 0.1
PRESENCE_MIN_RMS = 0.005
# Dois picos de correlação quase iguais em atrasos distantes: alinhamento ambíguo.
LAG_AMBIGUITY = 0.05
LAG_DISTINCT = 0.5
# Platô de correlação (atraso que deriva na gravação) mais largo que isto não tem meio confiável.
LAG_PLATEAU_MAX = 2.5
# Atraso por janela: cada dois minutos acham o seu, a até LAG_TRACK_REACH do atraso do trecho.
LAG_TRACK_WINDOW = 120.0
LAG_TRACK_HOP = 60.0
LAG_TRACK_REACH = 1.5
LAG_TRACK_AGREE = 1.0     # janela que destoa da mediana das outras não vale
LAG_TRACK_INHERIT = 150.0  # janela sem correlação herda da vizinha até esta distância (s)
# A fala vai para a voz que concentrou a energia do intervalo; abaixo disso são duas pessoas.
# Em 81% das falas reais uma voz só responde por mais de 99% da energia: o limiar alto custa pouco.
WINNER_SHARE = 0.85
MIN_VOICE_COVERAGE = 0.25
# A mistura do Meet quase nunca passa do nível de quem fala (tolerância a pacote fora de ordem).
MIX_OWN_WAY = 0.01
# Em 23 chamadas reais do Meet a mistura veio sempre com este identificador, e nunca com dono.
MEET_MIX_SOURCE = "42"
# Voz que está praticamente sozinha no intervalo pode cobrir menos dele (fala baixa manda poucos pacotes).
SOLO_SHARE = 0.9
SOLO_MIN_COVERAGE = 0.1
ANONYMOUS = "Participante"


def _state_dir() -> Path:
    from castanha.config import get_state_dir
    return get_state_dir()


def sessions_dir() -> Path:
    return _state_dir() / "meet"


def enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    if config is None:
        from castanha.config import load_config
        config = load_config()
    return (config.get("meet") or {}).get("speakers_enabled") is True


def track_audio_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Gravar as trilhas de áudio do Meet separadas (opção `meet.track_audio`, desligada por padrão)."""
    if config is None:
        from castanha.config import load_config
        config = load_config()
    meet = config.get("meet") or {}
    return meet.get("speakers_enabled") is True and meet.get("track_audio") is True


# ------------------------------------------------------------------ protobuf

def _varint(data: bytes, i: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("varint truncado")
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _fields(data: bytes) -> Optional[List[Tuple[int, int, Any]]]:
    """Campos de uma mensagem protobuf, ou None se os bytes não forem uma."""
    out = []
    i = 0
    try:
        while i < len(data):
            key, i = _varint(data, i)
            number, wire = key >> 3, key & 7
            if number == 0:
                return None
            if wire == 0:
                value, i = _varint(data, i)
            elif wire == 2:
                size, i = _varint(data, i)
                if i + size > len(data):
                    return None
                value = data[i:i + size]
                i += size
            elif wire == 1:
                value, i = data[i:i + 8], i + 8
            elif wire == 5:
                value, i = data[i:i + 4], i + 4
            else:
                return None
            if i > len(data):
                return None
            out.append((number, wire, value))
    except ValueError:
        return None
    return out


def _text(value) -> Optional[str]:
    if not isinstance(value, bytes):
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text and text.isprintable() else None


def _walk(data: bytes, depth: int = 0) -> Iterable[List[Tuple[int, int, Any]]]:
    fields = _fields(data)
    if fields is None:
        return
    yield fields
    if depth >= 12:
        return
    for _, wire, value in fields:
        if wire == 2 and value and _text(value) is None:
            yield from _walk(value, depth + 1)


def inflate(raw: bytes) -> bytes:
    """Mensagens do "collections" vêm em gzip; teto contra bomba de descompressão."""
    inflater = zlib.decompressobj(31)
    out = inflater.decompress(raw, MAX_INFLATED_BYTES)
    if inflater.unconsumed_tail:
        raise ValueError("mensagem do Meet grande demais")
    if not inflater.eof:
        raise ValueError("mensagem do Meet truncada")
    return out


def decode_collections(raw: bytes) -> List[Dict[str, Any]]:
    """Dispositivo↔nome e fonte de áudio↔dispositivo, pelo formato observado.

    Dispositivo: submensagem com campo 1 = "spaces/<id>/devices/<n>" e campo 2
    = nome exibido. Fonte: submensagem com campo 6 = dispositivo, campo 4 =
    SSRC em texto e campo 2 = tipo (1 = áudio). Casa pelo formato, não pelo
    caminho inteiro, para sobreviver a mudanças nos envelopes externos.
    """
    events = []
    for fields in _walk(inflate(raw)):
        by_number: Dict[int, List[Tuple[int, Any]]] = {}
        for number, wire, value in fields:
            by_number.setdefault(number, []).append((wire, value))

        def first_text(number):
            for wire, value in by_number.get(number, []):
                if wire == 2:
                    text = _text(value)
                    if text is not None:
                        return text
            return None

        def first_varint(number):
            for wire, value in by_number.get(number, []):
                if wire == 0:
                    return value
            return None

        device = first_text(1)
        name = first_text(2)
        if device and DEVICE_RE.match(device) and name and not name.startswith("http"):
            events.append({"type": "device", "device": device, "name": name[:200]})
            continue
        device = first_text(6)
        ssrc = first_text(4)
        kind = first_varint(2)
        if device and DEVICE_RE.match(device) and ssrc and ssrc.isdigit() and kind == 1:
            value = int(ssrc)
            if 0 < value < 2 ** 32:
                events.append({"type": "source", "device": device, "ssrc": value})
    return events


# ------------------------------------------------------------- host nativo

def _recording_active() -> Optional[Dict[str, Any]]:
    try:
        state = json.loads((_state_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if state.get("status") in ("recording", "paused", "processing") else None


def prune(now: Optional[float] = None, days: int = RETENTION_DAYS) -> None:
    now = time.time() if now is None else now
    root = sessions_dir()
    if not root.is_dir():
        return
    for path in root.glob("*.jsonl"):
        try:
            if now - path.stat().st_mtime > days * 86400:
                path.unlink()
        except OSError:
            pass
    for path in root.glob("*.audio"):
        try:
            if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime > days * 86400:
                import shutil
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


class Session:
    """Uma aba do Meet. Guarda o mapa sempre; escreve só com gravação ativa.

    Cada vínculo guarda o instante em que foi aprendido: o arquivo aberto no
    meio da chamada registra o mapa com esses instantes, não com a hora da
    abertura. Níveis só entram com a gravação rodando (pausa não grava voz).
    """

    def __init__(self, recording_active=_recording_active, clock=time.time, root: Optional[Path] = None,
                 check_interval: float = 0.5, track_audio: bool = False, send=None):
        # Trilhas separadas: `send` leva um pedido à página (ligar ou desligar a gravação das trilhas).
        self.track_audio = track_audio
        self.send = send
        self._capture: Optional[bool] = None
        self._audio: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._audio_bytes = 0
        self.check_interval = check_interval
        self.recording_active = recording_active
        self.clock = clock
        self.root = root
        self.code: Optional[str] = None
        self.devices: Dict[str, Tuple[str, float]] = {}
        # Dispositivos cujo nome veio da tela do Meet: o nome do canal, quando chegar, vale mais.
        self.screen: set = set()
        self.sources: Dict[int, Tuple[str, float]] = {}
        self.file = None
        self.path: Optional[Path] = None
        self.errors = 0
        self._checked_at = -1e9
        self._status: Optional[str] = None
        self._stopped_at: Optional[float] = None
        self._pauses: List[Tuple[float, float]] = []

    def _recording_status(self) -> Optional[str]:
        now = self.clock()
        if now - self._checked_at >= self.check_interval:
            state = self.recording_active()
            self._status = (state or {}).get("status") if state else None
            self._pauses = _pause_windows(0, (state or {}).get("pauses"), end=1e18)
            stopped = (state or {}).get("stopped_at")
            # Depois do stop, só o último lote (pacotes anteriores ao stop) ainda entra.
            self._stopped_at = stopped if _number(stopped) and now - stopped < STOP_GRACE else None
            self._checked_at = now
        return self._status

    def _write(self, record: Dict[str, Any]) -> None:
        if self.file is None:
            root = self.root or sessions_dir()
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            stamp = _dt.datetime.fromtimestamp(self.clock(), _dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
            self.path = root / f"{self.code}-{stamp}-{os.getpid()}.jsonl"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            self.file = os.fdopen(fd, "a", encoding="utf-8")
            header = {"type": "session", "code": self.code, "t": self.clock(), "decoder": DECODER_VERSION,
                      "errors_before": self.errors}
            self.file.write(json.dumps(header, ensure_ascii=False) + "\n")
            for device, (name, learned) in self.devices.items():
                known = {"type": "device", "t": learned, "device": device, "name": name}
                if device in self.screen:
                    known["via"] = "screen"
                self.file.write(json.dumps(known, ensure_ascii=False) + "\n")
            for ssrc, (device, learned) in self.sources.items():
                self.file.write(json.dumps({"type": "source", "t": learned, "device": device, "ssrc": ssrc}) + "\n")
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()

    def _apply_mapping(self, events, learned: float, status) -> None:
        for event in events:
            if event["type"] == "device":
                via_screen = event.get("via") == "screen"
                if via_screen and event["device"] in self.devices and event["device"] not in self.screen:
                    continue  # o canal já disse o nome: a tela não troca
                changed = (self.devices.get(event["device"]) or (None,))[0] != event["name"]
                if via_screen:
                    self.screen.add(event["device"])
                else:
                    self.screen.discard(event["device"])
                if changed:
                    self.devices[event["device"]] = (event["name"], learned)
            else:
                changed = (self.sources.get(event["ssrc"]) or (None,))[0] != event["device"]
                if changed:
                    self.sources[event["ssrc"]] = (event["device"], learned)
            if changed and status is not None and self.file is not None:
                self._write({**event, "t": learned})

    def note_error(self, reason: str) -> None:
        """Mapa possivelmente incompleto: fica registrado para o recorte recusar a janela."""
        self.errors += 1
        if self.file is not None and self.code is not None:
            self._write({"type": "error", "t": self.clock(), "reason": reason})

    def handle(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        code = msg.get("code")
        if isinstance(code, str) and MEET_CODE_RE.match(code):
            if self.code is None:
                self.code = code
            elif code != self.code:
                return  # uma sessão, uma chamada
        if self.code is None:
            return
        kind = msg.get("type")
        now = self.clock()
        status = self._recording_status()
        if status == "processing" and self._stopped_at is None:
            status = None
        if status is None:
            # Gravação acabou: o arquivo fecha e a próxima abre outro, com o mapa daquele momento.
            self.close()
        self._sync_capture(status)
        if kind == "audio":
            self._audio_chunk(msg, status, now)
            return
        if kind == "collections":
            try:
                raw = base64.b64decode(msg.get("b64") or "", validate=True)
                events = decode_collections(raw)
            except (ValueError, zlib.error, TypeError):
                self.note_error("decode")
                return
            # Instante em que a página recebeu a mensagem: vale também no
            # reenvio depois de um reinício do host.
            sent = msg.get("t")
            learned = sent / 1000.0 if _number(sent) and abs(sent / 1000.0 - now) < 86400 else now
            self._apply_mapping(events, learned, status)
        elif kind == "levels":
            frames = msg.get("frames") if isinstance(msg.get("frames"), list) else []
            gaps = [g for g in (msg.get("gaps") if isinstance(msg.get("gaps"), list) else [])[:100]
                    if isinstance(g, list) and len(g) == 2 and all(_number(x) for x in g) and g[0] <= g[1]]
            if gaps and status is not None:
                self._write({"type": "gap", "t": now, "gaps": [[g[0] / 1000.0, g[1] / 1000.0] for g in gaps]})
            if not frames:
                return
            stopped_at = self._stopped_at if status == "processing" else None
            if status == "paused":
                # Lote em trânsito na pausa: só os pacotes de antes dela.
                if not self._pauses:
                    return
                stopped_at = self._pauses[-1][0]
            if status not in ("recording", "paused") and stopped_at is None:
                return
            clean = []
            for frame in frames[:5000]:
                if (isinstance(frame, list) and len(frame) == 3
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in frame)
                        and all(math.isfinite(x) for x in frame)):
                    ts, ssrc, level = frame
                    if (0 < ssrc < 2 ** 32 and 0 <= level <= 1 and abs(ts / 1000.0 - now) < 3600
                            and (stopped_at is None or ts / 1000.0 <= stopped_at)
                            and not any(a <= ts / 1000.0 <= b for a, b in self._pauses)):
                        clean.append([round(ts / 1000.0, 3), int(ssrc), round(float(level), 3)])
            if clean:
                record = {"type": "levels", "t": now, "frames": clean}
                if msg.get("presence") is True and platform(self.code) == "teams":
                    record["presence"] = True  # nível artificial: só diz quem está presente
                self._write(record)
            # A mesma medida por trilha (trilha, fonte, nível): liga cada trilha gravada a quem falava nela.
            by_track = []
            for frame in (msg.get("tracks") if isinstance(msg.get("tracks"), list) else [])[:5000]:
                if (self.track_audio and isinstance(frame, list) and len(frame) == 4
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in frame)
                        and all(math.isfinite(x) for x in frame)):
                    ts, track, ssrc, level = frame
                    if (0 <= track < MAX_AUDIO_TRACKS and track == int(track) and 0 < ssrc < 2 ** 32 and 0 <= level <= 1
                            and abs(ts / 1000.0 - now) < 3600 and (stopped_at is None or ts / 1000.0 <= stopped_at)
                            and not any(a <= ts / 1000.0 <= b for a, b in self._pauses)):
                        by_track.append([round(ts / 1000.0, 3), int(track), int(ssrc), round(float(level), 3)])
            if by_track:
                self._write({"type": "track_levels", "t": now, "frames": by_track})
        elif kind == "mapping":
            # Teams: a página já entrega dispositivo→nome e fonte→dispositivo.
            if platform(self.code) != "teams":
                return
            sent = msg.get("t")
            learned = sent / 1000.0 if _number(sent) and abs(sent / 1000.0 - now) < 86400 else now
            events = []
            for pair in (msg.get("devices") if isinstance(msg.get("devices"), list) else [])[:500]:
                if (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and TEAMS_ID_RE.match(pair[0])
                        and isinstance(pair[1], str) and pair[1].strip() and pair[1].isprintable()):
                    events.append({"type": "device", "device": pair[0], "name": pair[1].strip()[:200]})
            for pair in (msg.get("sources") if isinstance(msg.get("sources"), list) else [])[:500]:
                # Dono None = vínculo revogado (a fonte deixou de pertencer a alguém conhecido).
                if (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], int) and not isinstance(pair[0], bool)
                        and 0 < pair[0] < 2 ** 32 and (pair[1] is None or (isinstance(pair[1], str) and TEAMS_ID_RE.match(pair[1])))):
                    events.append({"type": "source", "device": pair[1], "ssrc": pair[0]})
            self._apply_mapping(events, learned, status)
        elif kind == "screen":
            # Meet: nome lido da tela (quadro de vídeo ou lista de pessoas), preso ao dispositivo.
            if platform(self.code) != "meet":
                return
            sent = msg.get("t")
            learned = sent / 1000.0 if _number(sent) and abs(sent / 1000.0 - now) < 86400 else now
            events = []
            for pair in (msg.get("devices") if isinstance(msg.get("devices"), list) else [])[:500]:
                if (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and DEVICE_RE.match(pair[0])
                        and isinstance(pair[1], str) and pair[1].strip() and pair[1].isprintable()):
                    events.append({"type": "device", "device": pair[0], "name": pair[1].strip()[:200], "via": "screen"})
            self._apply_mapping(events, learned, status)
            probe = msg.get("probe")
            if (status is not None and self.file is not None and isinstance(probe, dict)
                    and all(isinstance(probe.get(k), int) and not isinstance(probe.get(k), bool) for k in ("nodes", "devices", "named"))):
                # Diagnóstico: quantos participantes a tela mostrou e quantos com nome legível.
                self._write({"type": "probe", "t": now, "nodes": min(probe["nodes"], 10000),
                             "devices": min(probe["devices"], 10000), "named": min(probe["named"], 10000)})
        elif kind == "replay_incomplete":
            # A página não guardou o histórico inteiro: o mapa reenviado pode estar velho.
            self.note_error("replay")
        elif kind == "bye":
            self.close()

    def _sync_capture(self, status) -> None:
        """Diz à página quando gravar as trilhas: só com o Castanha gravando (pausa para) e a opção ligada."""
        wanted = self.track_audio and status == "recording"
        if self.send is None or wanted == self._capture:
            return
        self._capture = wanted
        try:
            self.send({"type": "capture", "on": wanted})
        except Exception:  # noqa: BLE001 - sem canal de volta a página só não grava as trilhas
            pass

    def _audio_chunk(self, msg: Dict[str, Any], status, now: float) -> None:
        """Um pedaço de áudio de uma trilha, na ordem em que a página gravou. Só com gravação ativa."""
        if not self.track_audio or status not in ("recording", "paused"):
            return
        track, started, seq, b64 = msg.get("track"), msg.get("started"), msg.get("seq"), msg.get("b64")
        if not (isinstance(track, int) and not isinstance(track, bool) and 0 <= track < MAX_AUDIO_TRACKS
                and _number(started) and abs(started / 1000.0 - now) < 86400
                and isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 1 << 24
                and isinstance(b64, str) and 0 < len(b64) <= MAX_AUDIO_CHUNK_BYTES * 4 // 3 + 4):
            return
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError):
            self.note_error("audio")
            return
        key = (track, int(started))
        entry = self._audio.get(key)
        if entry is None:
            # O registro no arquivo da sessão cria a sessão (se preciso) e diz onde está o áudio.
            self._write({"type": "audio", "t": now, "track": track, "started": round(started / 1000.0, 3), "first_seq": seq})
            folder = self.path.parent / (self.path.stem + ".audio")
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / f"track{track}-{int(started)}.webm"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            # Começa sempre do pedaço 0 (é ele que traz o cabeçalho do arquivo), mesmo que outro chegue antes.
            entry = self._audio[key] = {"file": os.fdopen(fd, "ab"), "next": 0, "pending": {}}
        if seq < entry["next"] or seq in entry["pending"]:
            return  # repetido
        entry["pending"][seq] = data
        while entry["next"] in entry["pending"]:
            piece = entry["pending"].pop(entry["next"])
            if self._audio_bytes + len(piece) > MAX_AUDIO_SESSION_BYTES:
                entry["pending"].clear()
                return
            entry["file"].write(piece)
            self._audio_bytes += len(piece)
            entry["next"] += 1
        entry["file"].flush()
        if len(entry["pending"]) > MAX_AUDIO_PENDING:
            # Um pedaço se perdeu no caminho: o arquivo continua do próximo que chegou.
            self.note_error("audio_gap")
            entry["next"] = min(entry["pending"])

    def close(self) -> None:
        for entry in self._audio.values():
            try:
                entry["file"].close()
            except OSError:
                pass
        self._audio = {}
        if self.file is not None:
            try:
                self.file.close()
            finally:
                self.file = None


class _Oversized(Exception):
    pass


def _read_message(stream) -> Optional[Any]:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (size,) = struct.unpack("=I", header)
    if size > MAX_MESSAGE_BYTES:
        # Consumir o corpo mantém o protocolo alinhado para a próxima mensagem.
        left = size
        while left:
            chunk = stream.read(min(left, 1 << 16))
            if not chunk:
                return None
            left -= len(chunk)
        raise _Oversized()
    body = stream.read(size)
    if len(body) < size:
        return None
    return json.loads(body.decode("utf-8"))


def run_host(stdin=None, session: Optional[Session] = None) -> int:
    """Protocolo native messaging: 4 bytes de tamanho + JSON, até o navegador fechar."""
    stdin = stdin or sys.stdin.buffer
    active = enabled()
    if active:
        prune()

    def send(message: Dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        sys.stdout.buffer.write(struct.pack("=I", len(body)) + body)
        sys.stdout.buffer.flush()

    session = session or Session(track_audio=active and track_audio_enabled(), send=send)
    try:
        while True:
            try:
                msg = _read_message(stdin)
            except (_Oversized, ValueError):
                session.note_error("message")
                continue
            if msg is None:
                return 0
            # Desligado: o host fica de pé e descarta, senão o navegador
            # relançaria um processo a cada mensagem.
            if active:
                session.handle(msg)
    finally:
        session.close()


# ---------------------------------------------------------- recorte e timeline

def _epoch(value) -> Optional[float]:
    """`started_at` é isoformat local sem fuso (datetime.now()); com fuso também serve."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def meet_code(meeting: Optional[Dict[str, Any]]) -> Optional[str]:
    for key in ("conference_url", "hangoutLink", "location", "url", "description"):
        value = (meeting or {}).get(key)
        if isinstance(value, str):
            match = re.search(r"meet\.google\.com/([a-z]{3,}-[a-z]{3,}-[a-z]{3,})", value)
            if match:
                return match.group(1)
    return None


def _load_session(path: Path) -> List[Dict[str, Any]]:
    records = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # linha cortada por queda do navegador
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def snapshot(state: Dict[str, Any], stopped_at: float, root: Optional[Path] = None,
             why: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """O trecho das sessões do Meet que cobre esta gravação, para ir ao Bronze.

    Fonte→dispositivo guarda o histórico com o instante de cada mudança: se o
    Meet reatribuir uma fonte no meio da chamada, as falas anteriores seguem
    com quem era dono dela naquela hora. Nada posterior ao fim da janela entra.
    Sem sessão, com sessão ambígua (duas chamadas ao mesmo tempo sem o código
    do evento para desempatar) ou sem fala, devolve None.
    """
    start = _epoch(state.get("started_at"))
    if start is None:
        return None
    lo, hi = start - SNAPSHOT_MARGIN, stopped_at + SNAPSHOT_MARGIN
    root = root or sessions_dir()
    candidates = {}
    for path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
        records = _load_session(path)
        header = next((r for r in records if r.get("type") == "session"), None)
        stamps = [f[0] for r in records if r.get("type") == "levels"
                  for f in r.get("frames") or [] if isinstance(f, list) and f and _number(f[0])]
        if not header or not stamps or max(stamps) < lo or min(stamps) > hi:
            continue
        candidates.setdefault(header.get("code"), []).append(records)
    if not candidates:
        if why is not None:
            why.append("no_session")  # a extensão não mandou nada durante a gravação
        return None
    # Duas chamadas ao mesmo tempo: o gravador pega o som das duas, e o código
    # do evento não isola o áudio. Sem atribuição.
    if len(candidates) != 1:
        if why is not None:
            why.append("two_calls")
        return None
    code = next(iter(candidates))
    pauses = _pause_windows(start, state.get("pauses"), end=stopped_at)
    devices, sources, frames, late, errors, gaps = {}, {}, [], set(), 0, []
    measured = False
    for records in candidates[code]:
        for record in records:
            kind, t = record.get("type"), record.get("t")
            if kind in ("device", "source") and _number(t) and t > hi:
                if kind == "source" and isinstance(record.get("ssrc"), int):
                    late.add(str(record["ssrc"]))
                continue
            if kind == "device" and isinstance(record.get("name"), str) and _number(t):
                devices.setdefault(record.get("device"), []).append([t, record["name"]])
            elif kind == "source" and isinstance(record.get("ssrc"), int) and _number(t):
                sources.setdefault(str(record["ssrc"]), []).append([t, record.get("device")])
            elif kind == "levels":
                measured = measured or record.get("presence") is not True
                # Voz durante a pausa não entra no Bronze.
                frames.extend(f for f in record.get("frames") or []
                              if isinstance(f, list) and len(f) == 3 and all(_number(x) for x in f)
                              and lo <= f[0] <= hi and not any(a <= f[0] <= b for a, b in pauses))
            elif kind == "error" and _number(t) and t <= hi:
                errors += 1
            elif kind == "gap":
                gaps.extend(g for g in record.get("gaps") or []
                            if isinstance(g, list) and len(g) == 2 and all(_number(x) for x in g)
                            and g[1] >= lo and g[0] <= hi)
            elif kind == "session" and record.get("errors_before"):
                errors += 1
    for history in list(sources.values()) + list(devices.values()):
        history.sort(key=lambda item: item[0])
    frames.sort()
    if not frames:
        return None
    # Fonte que só ganhou dono depois da gravação era um participante: concorre sem nome.
    return {"version": 2, "decoder": DECODER_VERSION, "source": evidence_source(code), "meeting_code": code,
            "window": [lo, hi], "recording": [start, stopped_at], "devices": devices, "sources": sources,
            "late_sources": sorted(late - set(sources)), "errors": errors, "gaps": sorted(gaps),
            "presence_only": platform(code) == "teams" and not measured, "frames": frames}


def _owner(history, ts: float):
    """Dono da fonte no instante do pacote; antes do primeiro vínculo, ninguém."""
    owner = None
    for t, device in history:
        if t > ts:
            break
        owner = device
    return owner


def _pause_windows(start: float, pauses, end: Optional[float] = None) -> List[Tuple[float, float]]:
    """Janelas de pausa em relógio de parede. Sem retomada, a pausa vai até `end` (o stop)."""
    windows = []
    for p in pauses or []:
        at, until = p.get("paused_at"), p.get("resumed_at")
        if until is None and end is not None:
            until = end
        if _number(at) and _number(until) and until >= at:
            windows.append((at, until))
    return windows


def _name_at(devices: Dict[str, Any], device: Optional[str], ts: float) -> Optional[str]:
    """Nome do dispositivo no instante; renomear não reescreve o passado."""
    history = devices.get(device) if device else None
    if isinstance(history, str):
        return history
    name = None
    for t, value in history or []:
        if t > ts:
            break
        name = value
    return name


def speaking_intervals(snap: Dict[str, Any], exclude: Iterable[Tuple[float, float]] = (),
                       min_level: float = SPEAKING_LEVEL) -> List[Tuple[float, float, Optional[str]]]:
    """Intervalos (início, fim, nome) em relógio de parede, a partir dos níveis.

    Fonte de participante sem nome conhecido (dispositivo sem nome, ou dono só
    anunciado depois) vira nome None: não identifica ninguém, mas concorre com
    quem tem nome. Fonte que nunca pertence a um dispositivo, como a mistura
    que o Meet manda junto (CSRC 42 na chamada de teste), não é pessoa e sai.
    """
    per_name: Dict[Optional[str], List[float]] = {}
    devices, sources = snap.get("devices") or {}, snap.get("sources") or {}
    late = set(snap.get("late_sources") or [])
    exclude = list(exclude)
    active = [(ts, str(ssrc), level) for ts, ssrc, level in snap.get("frames") or []
              if level >= min_level and not any(a <= ts <= b for a, b in exclude)]
    owned = sorted((ts, level) for ts, ssrc, level in active if sources.get(ssrc) and _owner(sources[ssrc], ts))
    owned_ts = [ts for ts, _ in owned]

    def looks_like_mix(ts, level):
        # A mistura do Meet nunca aparece sozinha nem mais alta que quem fala
        # (medido em 3 sessões reais: diferença máxima 0,0). Uma voz desconhecida
        # mais alta que todos, ou sozinha, é gente e concorre.
        i = bisect.bisect_left(owned_ts, ts - FRAME_SPAN)
        j = bisect.bisect_right(owned_ts, ts + FRAME_SPAN)
        loudest = max((owned[k][1] for k in range(i, j)), default=None)
        return loudest is not None and level <= loudest + MIX_MARGIN

    # Mistura = fonte sem dono que nunca fala sozinha nem mais alta que quem
    # tem dono E acompanha quase toda a fala com dono. Qualquer outra fonte sem
    # dono é gente em todos os seus pacotes, inclusive por cima de alguém.
    owned_bins = {int(ts * 10) for ts in owned_ts if ts >= 0}
    unowned: Dict[str, set] = {}
    people = set()
    for ts, ssrc, level in active:
        if sources.get(ssrc) or ssrc in late:
            continue
        unowned.setdefault(ssrc, set()).add(int(ts * 10))
        if not looks_like_mix(ts, level):
            people.add(ssrc)
    for ssrc, bins in unowned.items():
        covered = sum(1 for k in owned_bins if k in bins or k - 1 in bins or k + 1 in bins)
        if (len(owned_bins) < MIX_MIN_EVIDENCE or covered < MIX_MIN_COVERAGE * len(owned_bins)
                or platform(snap.get("meeting_code")) == "teams"):
            # O Teams não tem fonte de mistura e não manda nível: toda fonte sem dono é gente.
            people.add(ssrc)
    for ts, ssrc, level in active:
        history = sources.get(ssrc)
        if not history and ssrc not in late and ssrc not in people:
            continue
        device = _owner(history, ts) if history else None
        per_name.setdefault(_name_at(devices, device, ts), []).append(ts)
    intervals = []
    for name, stamps in per_name.items():
        stamps.sort()
        begin = end = stamps[0]
        for ts in stamps[1:]:
            if ts - end > MERGE_GAP:
                intervals.append((begin - FRAME_SPAN, end + FRAME_SPAN, name))
                begin = ts
            end = ts
        intervals.append((begin - FRAME_SPAN, end + FRAME_SPAN, name))
    intervals.sort(key=lambda item: (item[0], item[1]))
    return intervals


def audio_segments(start: float, pauses=None) -> List[Tuple[float, float, float]]:
    """Trechos contínuos do arquivo: (deslocamento inicial, final, soma das pausas antes).

    Pausa (SIGSTOP no ffmpeg) não entra no arquivo; o relógio de parede anda.
    """
    segments, begin, paused = [], 0.0, 0.0
    for at, until in sorted(_pause_windows(start, pauses)):
        offset = at - start - paused
        if offset < begin:
            offset = begin
        segments.append((begin, offset, paused))
        begin, paused = offset, paused + (until - at)
    segments.append((begin, math.inf, paused))
    return segments


def _segment_index(segments, offset: float) -> int:
    for index, (a, b, _) in enumerate(segments):
        if a <= offset < b:
            return index
    return len(segments) - 1


def wall_clock(offset: float, start: float, pauses: Optional[List[Dict[str, Any]]] = None) -> float:
    """Deslocamento no áudio → relógio de parede."""
    segments = audio_segments(start, pauses)
    return start + offset + segments[_segment_index(segments, offset)][2]


def channel_envelope(audio_path: Path, channel: int = 1, step: float = LAG_STEP) -> Optional[List[float]]:
    """RMS linear do canal a cada `step` s, pelo ffmpeg. None se não der para medir."""
    rate = 8000
    samples = int(rate * step)
    graph = (f"pan=mono|c0=c{channel},aresample={rate},asetnsamples=n={samples}:p=0,"
             "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    try:
        proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio_path), "-af", graph, "-f", "null", "-"],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    values = []
    for line in proc.stdout.splitlines():
        if "RMS_level=" in line:
            raw = line.split("=", 1)[1].strip()
            try:
                db = float(raw)
            except ValueError:
                db = -math.inf
            values.append(0.0 if not math.isfinite(db) else 10 ** (db / 20))
    return values or None


def _pearson(a: List[float], b: List[float]) -> float:
    n = len(a)
    if n < 20:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def _level_bins(snap: Dict[str, Any], step: float = LAG_STEP) -> Dict[int, float]:
    level_at: Dict[int, float] = {}
    for ts, _, level in snap.get("frames") or []:
        key = int(math.floor(ts / step))
        level_at[key] = level_at.get(key, 0.0) + level
    return level_at


def estimate_lag(envelope: List[float], snap: Dict[str, Any], start: float,
                 pauses=None, step: float = LAG_STEP) -> Tuple[float, float]:
    """(atraso em s, correlação) de um trecho. Positivo: o som chega à gravação depois do sinal do Meet."""
    return estimate_lags(envelope, snap, start, pauses, step)[0]


def estimate_lags(envelope: List[float], snap: Dict[str, Any], start: float,
                  pauses=None, step: float = LAG_STEP) -> List[Tuple[float, float]]:
    """Um (atraso, correlação) por trecho entre pausas.

    Ao voltar do SIGSTOP o PipeWire pode entregar o áudio acumulado e deslocar
    o resto do arquivo; cada trecho acha o próprio atraso.
    """
    level_at = _level_bins(snap, step)
    out = []
    steps = int(round(LAG_WINDOW / step))
    for a, b, paused in audio_segments(start, pauses):
        lo, hi = int(math.ceil(a / step)), min(len(envelope), int(b / step) if math.isfinite(b) else len(envelope))
        piece = envelope[lo:hi]
        walls = [start + i * step + paused for i in range(lo, hi)]
        scores = []
        for k in range(-steps, steps + 1):
            lag = k * step
            meet = [level_at.get(int(math.floor((w - lag) / step)), 0.0) for w in walls]
            scores.append((round(lag, 3), _pearson(piece, meet)))
        out.append(_best_lag(scores, step))
    return out


def mix_sources(snap: Dict[str, Any]) -> set:
    """Fontes que são a mistura do Meet, não gente.

    O Meet manda, junto das vozes, uma fonte que repete o nível de quem fala
    (CSRC 42 em 23 chamadas reais). Ela nunca tem dono, nunca aparece sozinha
    nem mais alta que as outras e acompanha quase toda a fala. Sem dono mas
    falando sozinha ou por cima de alguém é participante. Duas fontes sem dono
    que se espelham (chamada em que o mapa não chegou): a de menor número é a
    mistura. O Teams não tem mistura.
    """
    if platform(snap.get("meeting_code")) == "teams":
        return set()
    sources = snap.get("sources") or {}
    late = set(snap.get("late_sources") or [])
    level: Dict[str, Dict[int, float]] = {}
    for ts, ssrc, value in snap.get("frames") or []:
        if value >= SPEAKING_LEVEL:
            bins = level.setdefault(str(ssrc), {})
            key = int(ts * 10)
            bins[key] = max(bins.get(key, 0.0), value)
    candidates = sorted((s for s in level if not sources.get(s) and s not in late),
                        key=lambda s: (int(s) if s.isdigit() else 1 << 40, s))
    mix: set = {MEET_MIX_SOURCE} if MEET_MIX_SOURCE in candidates else set()
    for ssrc in candidates:
        if ssrc in mix:
            continue
        others: Dict[int, float] = {}
        for other, bins in level.items():
            if other == ssrc or other in mix:
                continue
            for key, value in bins.items():
                others[key] = max(others.get(key, 0.0), value)
        mine = level[ssrc]
        if len(others) < MIX_MIN_EVIDENCE:
            continue  # pouca fala alheia para concluir: é participante
        near = lambda bins, key: max(bins.get(key - 1, 0.0), bins.get(key, 0.0), bins.get(key + 1, 0.0))  # noqa: E731
        covered = sum(1 for key in others if near(mine, key) > 0)
        own_way = sum(1 for key, value in mine.items() if value > near(others, key) + MIX_MARGIN)
        if covered >= MIX_MIN_COVERAGE * len(others) and own_way <= MIX_OWN_WAY * len(mine):
            mix.add(ssrc)
    return mix


def voices(snap: Dict[str, Any], exclude: Iterable[Tuple[float, float]] = ()) -> List[Tuple[float, float, Tuple[str, str]]]:
    """(instante, nível, quem) de cada pacote de voz, com a mistura do Meet de fora.

    "Quem" é ("name", nome) quando o Meet disse o nome do dispositivo dono da
    fonte; ("device", dispositivo) quando a fonte tem dono sem nome; e
    ("source", fonte) quando nem dono tem. Os dois últimos são participantes
    de verdade, só sem nome: viram "Participante N" na transcrição.

    O Meet só anuncia nome e dono quando alguém entra ou muda, então o vínculo
    costuma chegar depois de a pessoa já ter falado. Fonte que só teve um dono
    na chamada inteira é dele desde o primeiro pacote, e dispositivo que só
    teve um nome se chama assim o tempo todo. Com mais de um dono ou nome, vale
    o de cada instante (antes do primeiro, o primeiro).
    """
    devices, sources = snap.get("devices") or {}, snap.get("sources") or {}
    mix = mix_sources(snap)
    exclude = list(exclude)
    cache: Dict[str, Any] = {}

    def owner(ssrc: str, ts: float) -> Optional[str]:
        history = sources.get(ssrc)
        if not history:
            return None
        if ssrc not in cache:
            owners = {device for _, device in history}
            cache[ssrc] = next(iter(owners)) if len(owners) == 1 else False
        if cache[ssrc] is not False:
            return cache[ssrc]
        return _owner(history, ts) if ts >= history[0][0] else history[0][1]

    def name(device: Optional[str], ts: float) -> Optional[str]:
        history = devices.get(device) if device else None
        if not history:
            return None
        if isinstance(history, str):
            return history
        return _name_at(devices, device, ts) or history[0][1]

    out = []
    for ts, ssrc, value in snap.get("frames") or []:
        ssrc = str(ssrc)
        if value < COMPETING_LEVEL or ssrc in mix or any(a <= ts <= b for a, b in exclude):
            continue
        device = owner(ssrc, ts)
        who = name(device, ts)
        key = ("name", who) if who else ("device", device) if device else ("source", ssrc)
        out.append((ts, value, key))
    out.sort(key=lambda item: item[0])
    return out


def _best_lag(scores: List[Tuple[float, float]], step: float = LAG_STEP) -> Tuple[float, float]:
    """Atraso de um conjunto (atraso, correlação) em ordem de atraso.

    O pico pode ser um platô (o atraso deriva um pouco ao longo da gravação):
    vale o meio dele, se não for largo demais. Outro pico quase igual, separado
    do platô, é alinhamento ambíguo (sinal periódico ou pobre) e zera a
    correlação: o nome poderia ser o do vizinho.
    """
    top = max(range(len(scores)), key=lambda i: (scores[i][1], -abs(scores[i][0])))
    floor = scores[top][1] - LAG_AMBIGUITY
    lo = hi = top
    while lo > 0 and scores[lo - 1][1] >= floor:
        lo -= 1
    while hi < len(scores) - 1 and scores[hi + 1][1] >= floor:
        hi += 1
    apart = int(round(LAG_DISTINCT / step))
    rival = any(r >= floor for i, (_, r) in enumerate(scores) if i < lo - apart or i > hi + apart)
    if rival or scores[hi][0] - scores[lo][0] > LAG_PLATEAU_MAX:
        return (scores[top][0], 0.0)
    lag = scores[top][0] if scores[hi][0] - scores[lo][0] <= LAG_DISTINCT else scores[(lo + hi) // 2][0]
    return (round(lag, 3), scores[top][1])


def _lag_scores(piece: List[float], walls: List[float], level_at: Dict[int, float], center: float,
                reach: float, step: float) -> List[Tuple[float, float]]:
    steps = int(round(reach / step))
    scores = []
    for k in range(-steps, steps + 1):
        lag = center + k * step
        meet = [level_at.get(int(math.floor((w - lag) / step)), 0.0) for w in walls]
        scores.append((round(lag, 3), _pearson(piece, meet)))
    return scores


def lag_track(envelope: List[float], snap: Dict[str, Any], start: float, pauses=None,
              step: float = LAG_STEP) -> List[List[Tuple[float, float, float, float]]]:
    """Por trecho entre pausas, janelas (início, fim, atraso, correlação) em deslocamento de áudio.

    O atraso entre o som gravado e o sinal do Meet não é constante numa
    gravação longa. Cada janela de dois minutos acha o seu, perto do atraso do
    trecho; janela sem correlação herda o da vizinha mais próxima que tenha, e
    trecho em que as janelas não concordam entre si fica sem atraso (vazio).
    """
    level_at = _level_bins(snap, step)
    tracks = []
    span, hop = int(round(LAG_TRACK_WINDOW / step)), int(round(LAG_TRACK_HOP / step))
    for (a, b, paused), (seg_lag, seg_r) in zip(audio_segments(start, pauses), estimate_lags(envelope, snap, start, pauses, step)):
        lo, hi = int(math.ceil(a / step)), min(len(envelope), int(b / step) if math.isfinite(b) else len(envelope))
        anchored = seg_r >= MIN_CORRELATION
        windows = []
        for first in range(lo, max(lo + 1, hi - span // 2), hop):
            last = min(hi, first + span)
            piece = envelope[first:last]
            walls = [start + i * step + paused for i in range(first, last)]
            if anchored:
                lag, r = _best_lag(_lag_scores(piece, walls, level_at, seg_lag, LAG_TRACK_REACH, step), step)
            else:
                lag, r = _best_lag(_lag_scores(piece, walls, level_at, 0.0, LAG_WINDOW, step), step)
            windows.append([first * step, last * step, lag, r])
        good = [w for w in windows if w[3] >= MIN_CORRELATION]
        if good:
            ordered = sorted(w[2] for w in good)
            median = ordered[len(ordered) // 2]
            good = [w for w in good if abs(w[2] - median) <= LAG_TRACK_AGREE]
        if not good or (not anchored and len(good) < max(2, len(windows) // 2)):
            tracks.append([(a, b, seg_lag, seg_r)] if anchored else [])
            continue
        # Janela recusada (pico rival longe do atraso das outras) tenta de novo só perto da mediana.
        for w in windows:
            if w in good:
                continue
            piece = envelope[int(round(w[0] / step)):int(round(w[1] / step))]
            walls = [start + i * step + paused for i in range(int(round(w[0] / step)), int(round(w[1] / step)))]
            lag, r = _best_lag(_lag_scores(piece, walls, level_at, median, LAG_TRACK_AGREE, step), step)
            if r >= MIN_CORRELATION:
                w[2], w[3] = lag, r
                good.append(w)
        centers = [((w[0] + w[1]) / 2, w) for w in good]
        out = []
        for w in windows:
            if w in good:
                out.append(tuple(w))
                continue
            middle = (w[0] + w[1]) / 2
            distance, nearest = min((abs(c - middle), g) for c, g in centers)
            if distance <= LAG_TRACK_INHERIT:
                out.append((w[0], w[1], nearest[2], nearest[3]))
        tracks.append(out)
    return tracks


def _lag_at(track: List[Tuple[float, float, float, float]], offset: float) -> Optional[Tuple[float, float]]:
    """Atraso da janela cujo meio está mais perto do deslocamento, entre as que o cobrem."""
    best = None
    for a, b, lag, r in track:
        if a <= offset < b or (not math.isfinite(b) and offset >= a):
            distance = abs((a + (b if math.isfinite(b) else a)) / 2 - offset)
            if best is None or distance < best[0]:
                best = (distance, lag, r)
    return (best[1], best[2]) if best else None


def attribute(utterances: List[Dict[str, Any]], snap: Dict[str, Any], start: float,
              pauses=None, lags: Optional[List[Tuple[float, float]]] = None,
              envelope: Optional[List[float]] = None, step: float = LAG_STEP,
              track: Optional[List[List[Tuple[float, float, float, float]]]] = None) -> Dict[int, Dict[str, Any]]:
    """Índice da fala → {key, coverage}: a voz que dominou o intervalo. Só o canal do sistema.

    Cada fala remota vai para quem concentrou a energia de voz no intervalo
    dela (WINNER_SHARE), tenha nome ou não: participante sem nome continua
    sendo uma pessoa só, e a transcrição o chama de "Participante N". Ficam
    sem dono as falas que atravessam uma pausa, as de trecho sem alinhamento
    com o Meet, as que caem numa travada da página, as divididas entre duas
    vozes e as que o canal gravado não acompanhou (Meet mudo, outro som).
    """
    segments = audio_segments(start, pauses)
    spoken = voices(snap, exclude=_pause_windows(start, pauses))
    stamps = [ts for ts, _, _ in spoken]
    level_at = _level_bins(snap, step) if envelope is not None else {}
    result = {}
    for index, utterance in enumerate(utterances):
        if utterance.get("origin") != REMOTE_ORIGIN:
            continue
        try:
            u_start, u_end = float(utterance["start"]), float(utterance["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(u_start) and math.isfinite(u_end)) or u_end <= u_start:
            continue
        segment = _segment_index(segments, u_start)
        if _segment_index(segments, max(u_start, u_end - 1e-6)) != segment:
            continue
        if track is not None:
            found = _lag_at(track[segment], (u_start + u_end) / 2) if segment < len(track) else None
            lag, correlation = found if found else (0.0, 0.0)
        else:
            lag, correlation = (lags[segment] if lags and segment < len(lags) else (0.0, 0.0))
        if correlation < MIN_CORRELATION:
            continue
        shift = start + segments[segment][2] - lag
        lo, hi = u_start + shift - EDGE_TOLERANCE, u_end + shift + EDGE_TOLERANCE
        if any(a < hi and b > lo for a, b in snap.get("gaps") or []):
            continue  # a página travou nesse trecho: a coleta pode ter perdido outra voz
        energy: Dict[Tuple[str, str], float] = {}
        active: Dict[Tuple[str, str], set] = {}
        for i in range(bisect.bisect_left(stamps, lo), bisect.bisect_right(stamps, hi)):
            ts, value, key = spoken[i]
            energy[key] = energy.get(key, 0.0) + value
            active.setdefault(key, set()).add(int(ts / step))
        if not energy:
            continue
        key = max(energy, key=lambda k: (energy[k], k))
        share = energy[key] / sum(energy.values())
        width = hi - lo
        # Cobertura medida dentro da fala, sem a folga das bordas: o começo da
        # fala seguinte de outra pessoa não pode dar dono a um trecho mudo.
        core_lo, core_hi = int((lo + EDGE_TOLERANCE) / step), int((hi - EDGE_TOLERANCE) / step)
        covered = min(1.0, sum(1 for k in active[key] if core_lo <= k <= core_hi) * step / (u_end - u_start))
        # Tempo em que outra pessoa (com ou sem nome) teve voz no intervalo: uma
        # frase curta de outro já é fala dele, mesmo baixa.
        others = sum(len(bins) for k, bins in active.items() if k != key) * step
        if (share < WINNER_SHARE or others > min(MAX_OTHER_SHARE * width, MAX_OTHER_SECONDS)
                or covered < (SOLO_MIN_COVERAGE if share >= SOLO_SHARE else MIN_VOICE_COVERAGE)):
            continue
        if envelope is not None:
            # A correlação da janela não vê um desvio curto (Meet mudo e outro
            # áudio tocando): a fala precisa bater com o Meet sozinha.
            seg_lo, seg_hi = segments[segment][0], segments[segment][1]
            first = max(int(math.ceil(max(u_start - LOCAL_MARGIN, seg_lo) / step)), 0)
            last = min(int((min(u_end + LOCAL_MARGIN, seg_hi)) / step), len(envelope))
            if last - first < LOCAL_MIN_BINS:
                continue
            local = [level_at.get(int(math.floor((i * step + shift) / step)), 0.0)
                     for i in range(first, last)]
            piece = envelope[first:last]
            presence = snap.get("presence_only") is True
            if presence:
                local = [1.0 if value > 0 else 0.0 for value in local]  # presença, não amplitude
            if len(set(local)) == 1 and presence:
                # Teams só diz "presente": sem variação no trecho, basta haver som gravado.
                if sum(piece) / len(piece) < PRESENCE_MIN_RMS:
                    continue
            elif _pearson(piece, local) < LOCAL_CORRELATION:
                continue
        result[index] = {"key": key, "coverage": round(share, 3)}
    return result


def label_voices(found: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Troca a chave de cada voz pelo nome exibido: o do Meet, ou "Participante N" na ordem em que falaram."""
    anonymous: Dict[Tuple[str, str], str] = {}
    out = {}
    for index in sorted(found):
        kind, value = found[index]["key"]
        if kind == "name":
            shown, named = value, True
        else:
            shown = anonymous.setdefault((kind, value), f"{ANONYMOUS} {len(anonymous) + 1}")
            named = False
        out[index] = {"name": shown, "coverage": found[index]["coverage"], "named": named}
    return out


def annotate(utterances: List[Dict[str, Any]], result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cópia das falas, na ordem da transcrição, com o nome atribuído e a evidência."""
    ordered = sorted(utterances, key=lambda s: (s.get("start", 0), s.get("end", 0), s.get("channel", 0)))
    names = (result or {}).get("names") or {}
    out = []
    for index, utterance in enumerate(ordered):
        item = dict(utterance)
        hit = names.get(str(index))
        if hit:
            item.update(attributed_name=hit["name"], attribution_coverage=hit["coverage"],
                        attribution_source=(OWNER_SOURCE if hit.get("owner") else (result or {}).get("source", EVIDENCE_SOURCE)))
            if hit.get("named") is False:
                item["attribution_named"] = False  # participante real, sem nome: "Participante N"
        out.append(item)
    return out


def _render(utterances: List[Dict[str, Any]], names: Dict[int, Dict[str, Any]], tag: str = "Meet") -> str:
    lines = []
    for index, s in enumerate(utterances):
        hit = names.get(index)
        speaker = (s.get("speaker") if not hit else f"{hit['name']} (microfone)" if hit.get("owner")
                   else f"{hit['name']} ({tag})")
        lines.append(f"[{float(s['start']):.2f}s–{float(s['end']):.2f}s] {speaker}: {s.get('text', '')}")
    return "\n".join(lines)


def _speakers_dir(bronze: Path) -> Path:
    return bronze / ".speakers"


def attribution_for_job(bronze: Path, job: Dict[str, Any], envelope_fn=None,
                        owner_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resultado cacheado por gravação: nomes por fala, texto reescrito e evidência.

    O checkpoint da transcrição não muda: a atribuição é uma camada à parte em
    `.speakers/<sha do áudio>`, refeita só quando o recorte, as falas ou o
    algoritmo mudam. Sem medição do canal, nada é atribuído nem cacheado.
    """
    sha = job.get("sha256")
    utterances = job.get("utterances") or []
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    snap_path = _speakers_dir(bronze) / f"{sha}.json"
    if not snap_path.exists() or not job.get("channel_provenance") or not utterances:
        return None
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if snap.get("version") != 2 or snap.get("errors"):
        # Mensagem do Meet perdida ou ilegível: o mapa pode estar incompleto.
        return None
    ordered = sorted(utterances, key=lambda s: (s.get("start", 0), s.get("end", 0), s.get("channel", 0)))
    state = job.get("state") or {}
    pauses = state.get("pauses") or []
    owner_name = owner_name.strip()[:200] if isinstance(owner_name, str) and owner_name.strip() else None
    key = hashlib.sha256(json.dumps([ALGORITHM_VERSION, snap, ordered, pauses, job.get("recorded_at"), owner_name],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = _speakers_dir(bronze) / f"{sha}.result.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("key") == key:
            return cached if cached.get("attributed") else None
    except (OSError, ValueError):
        pass
    start = _epoch(job.get("recorded_at") or state.get("started_at"))
    envelope_fn = envelope_fn or channel_envelope
    envelope = envelope_fn(Path(job["audio_path"])) if job.get("audio_path") else None
    if start is None or not envelope:
        return None
    lags = estimate_lags(envelope, snap, start, pauses)
    track = lag_track(envelope, snap, start, pauses)
    names = label_voices(attribute(ordered, snap, start, pauses, envelope=envelope, track=track))
    remote = sum(1 for s in ordered if s.get("origin") == REMOTE_ORIGIN)
    attributed = len(names)
    spoken: Dict[str, int] = {}
    unnamed = set()
    for item in names.values():
        spoken[item["name"]] = spoken.get(item["name"], 0) + 1
        if not item["named"]:
            unnamed.add(item["name"])
    # Presença observada = teve voz dentro da gravação (sem a margem do recorte);
    # quem só estava no mapa fica de fora.
    rec_lo, rec_hi = snap.get("recording") or [start, math.inf]
    heard = {who for ts, value, (kind, who) in voices(snap, exclude=_pause_windows(start, pauses, end=rec_hi))
             if kind == "name" and value >= SPEAKING_LEVEL and rec_lo <= ts <= rec_hi}
    roster = sorted(heard | (set(spoken) - unnamed)) + sorted(unnamed, key=lambda n: (len(n), n))
    if names and owner_name:
        # Numa chamada gravada nesta máquina, o microfone é de quem a configurou como dona
        # (`meet.owner_name`). Só entra quando a chamada teve voz identificada pelo Meet.
        for index, s in enumerate(ordered):
            if s.get("origin") == LOCAL_ORIGIN:
                names[index] = {"name": owner_name, "coverage": 1.0, "named": True, "owner": True}
    result = {
        "key": key, "version": ALGORITHM_VERSION, "source": evidence_source(snap.get("meeting_code")),
        "decoder": snap.get("decoder"),
        "meeting_code": snap.get("meeting_code"),
        "segments": [{"lag_seconds": lag, "correlation": round(r, 3)} for lag, r in lags],
        "lag_windows": sum(len(t) for t in track),
        "remote_utterances": remote, "attributed": attributed,
        "named": sum(1 for i, v in names.items() if v["named"] and not v.get("owner")),
        "participants": [{"name": n, "utterances": spoken.get(n, 0), "named": n not in unnamed} for n in roster],
        "owner": owner_name if names and owner_name else None,
        "names": {str(i): v for i, v in names.items()},
        "text": _render(ordered, names, label(snap.get("meeting_code"))) if names else None,
    }
    try:
        from castanha.durability import write_json
        write_json(cache, result)
    except OSError:
        pass
    return result if names else None


def save_snapshot(bronze: Path, audio_sha256: str, state: Dict[str, Any], stopped_at: float,
                  why: Optional[List[str]] = None) -> Optional[Path]:
    """Copia para o Bronze o trecho do Meet desta gravação. Falha nunca derruba o stop."""
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", audio_sha256 or ""):
            return None
        snap = snapshot(state, stopped_at, why=why)
        if not snap:
            return None
        from castanha.durability import write_json
        if len(json.dumps(snap)) > MAX_SNAPSHOT_BYTES:
            # Grande demais para a exclusão/restauração copiarem: sem nomes nesta gravação.
            print("[Castanha] Linha do tempo do Meet grande demais; gravação fica sem nomes", file=sys.stderr)
            return None
        path = _speakers_dir(bronze) / f"{audio_sha256}.json"
        write_json(path, snap)
        return path
    except Exception as exc:  # noqa: BLE001 - evidência opcional, gravação é o que importa
        print(f"[Castanha] Linha do tempo do Meet não salva: {type(exc).__name__}", file=sys.stderr)
        return None


# ------------------------------------------------------------- instalação

def _xdg_config() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value) if value else Path.home() / ".config"


def _flags_file() -> Path:
    return _xdg_config() / "chromium-flags.conf"


def _native_manifest() -> Path:
    return _xdg_config() / "chromium" / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def _extension_dir(project_root: Path) -> Path:
    return project_root / "browser" / "meet-speakers"


def _split_extensions(line: str) -> List[str]:
    return [p for p in line.split("=", 1)[1].split(",") if p] if "=" in line else []


def install(project_root: Path, flags: Optional[Path] = None, manifest: Optional[Path] = None) -> Dict[str, Any]:
    """Liga a extensão no Chromium (vale no próximo início dele) e registra o host nativo."""
    flags = flags or _flags_file()
    manifest = manifest or _native_manifest()
    extension = str(_extension_dir(project_root))
    host = project_root / "bin" / "castanha-meet-host"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "name": HOST_NAME, "description": "Castanha: quem fala no Google Meet",
        "path": str(host), "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }, indent=2) + "\n", encoding="utf-8")
    lines = flags.read_text(encoding="utf-8").splitlines() if flags.exists() else []
    backup = flags.with_name(flags.name + ".pre-castanha-meet")
    if flags.exists() and not backup.exists():
        backup.write_text(flags.read_text(encoding="utf-8"), encoding="utf-8")
    out, placed = [], False
    for line in lines:
        if line.strip().startswith("--load-extension="):
            paths = [p for p in _split_extensions(line.strip()) if not p.endswith("/browser/meet-speakers")]
            line = "--load-extension=" + ",".join(paths + [extension])
            placed = True
        out.append(line)
    if not placed:
        out.append(f"--load-extension={extension}")
    flags.write_text("\n".join(out) + "\n", encoding="utf-8")
    return status(project_root, flags, manifest)


def set_enabled(value: bool) -> None:
    """Só a chave `meet.speakers_enabled` muda; o resto do arquivo fica como está."""
    from castanha.config import get_config_file
    from castanha.secure_io import read_private_json, write_private_json
    path = get_config_file()
    data = read_private_json(path) if path.exists() else {}
    data = data if isinstance(data, dict) else {}
    section = data.get("meet") if isinstance(data.get("meet"), dict) else {}
    section["speakers_enabled"] = bool(value)
    data["meet"] = section
    write_private_json(path, data)


def uninstall(flags: Optional[Path] = None, manifest: Optional[Path] = None) -> None:
    flags = flags or _flags_file()
    manifest = manifest or _native_manifest()
    if flags.exists():
        out = []
        for line in flags.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("--load-extension="):
                paths = [p for p in _split_extensions(line.strip()) if not p.endswith("/browser/meet-speakers")]
                if not paths:
                    continue
                line = "--load-extension=" + ",".join(paths)
            out.append(line)
        flags.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    try:
        manifest.unlink()
    except FileNotFoundError:
        pass


def _running_chromium_has(extension: str) -> Optional[bool]:
    found = None
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            args = [a for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if len(args) == 1:
            # O Chromium reescreve o próprio argv numa string só, separada por espaços.
            args = args[0].split(b" ")
        if not args or not args[0].endswith(b"/chromium") or any(a.startswith(b"--type=") for a in args):
            continue
        found = any(a.startswith(b"--load-extension=") and extension.encode() in a for a in args)
        if found:
            return True
    return found


def status(project_root: Path, flags: Optional[Path] = None, manifest: Optional[Path] = None) -> Dict[str, Any]:
    flags = flags or _flags_file()
    manifest = manifest or _native_manifest()
    extension = str(_extension_dir(project_root))
    in_flags = flags.exists() and any(
        extension in _split_extensions(line.strip())
        for line in flags.read_text(encoding="utf-8").splitlines() if line.strip().startswith("--load-extension="))
    try:
        registered = json.loads(manifest.read_text(encoding="utf-8")).get("path") == str(project_root / "bin" / "castanha-meet-host")
    except (OSError, ValueError):
        registered = False
    from castanha.config import load_config
    per_channel = (load_config().get("transcription") or {}).get("por_canal") is True
    return {"enabled": enabled(), "flags": in_flags, "native_host": registered, "per_channel": per_channel,
            "chromium_loaded": _running_chromium_has(extension), "extension": extension}


def main(argv: Optional[List[str]] = None) -> int:
    return run_host()


if __name__ == "__main__":
    sys.exit(main())
