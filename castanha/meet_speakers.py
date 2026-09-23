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
ALGORITHM_VERSION = "meet-speakers-v1"
EVIDENCE_SOURCE = "google_meet_webrtc"

MEET_CODE_RE = re.compile(r"^[a-z]{3,}-[a-z]{3,}-[a-z]{3,}$")
DEVICE_RE = re.compile(r"^spaces/[\w-]+/devices/\d+$")
MAX_MESSAGE_BYTES = 1 << 20
MAX_INFLATED_BYTES = 4 << 20
RETENTION_DAYS = 7
# Nível linear (0..1, convertido de dBov pelo navegador). Fala medida no teste
# de 23/09/2026 ficou entre 0,14 e 0,28; fonte calada, 0.
SPEAKING_LEVEL = 0.05
FRAME_SPAN = 0.12
MERGE_GAP = 0.8
SNAPSHOT_MARGIN = 5.0
# Atribuição: a fala precisa estar coberta pelo participante e ele precisa
# dominar o intervalo; empate ou cobertura rala mantém o rótulo do canal.
EDGE_TOLERANCE = 0.3
MIN_COVERAGE = 0.3
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
    return state if state.get("status") in ("recording", "paused") else None


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


class Session:
    """Uma aba do Meet. Guarda o mapa sempre; escreve só com gravação ativa.

    Cada vínculo guarda o instante em que foi aprendido: o arquivo aberto no
    meio da chamada registra o mapa com esses instantes, não com a hora da
    abertura. Níveis só entram com a gravação rodando (pausa não grava voz).
    """

    def __init__(self, recording_active=_recording_active, clock=time.time, root: Optional[Path] = None,
                 check_interval: float = 0.5):
        self.check_interval = check_interval
        self.recording_active = recording_active
        self.clock = clock
        self.root = root
        self.code: Optional[str] = None
        self.devices: Dict[str, Tuple[str, float]] = {}
        self.sources: Dict[int, Tuple[str, float]] = {}
        self.file = None
        self.path: Optional[Path] = None
        self.errors = 0
        self._checked_at = -1e9
        self._status: Optional[str] = None

    def _recording_status(self) -> Optional[str]:
        now = self.clock()
        if now - self._checked_at >= self.check_interval:
            state = self.recording_active()
            self._status = (state or {}).get("status") if state else None
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
                self.file.write(json.dumps({"type": "device", "t": learned, "device": device, "name": name},
                                           ensure_ascii=False) + "\n")
            for ssrc, (device, learned) in self.sources.items():
                self.file.write(json.dumps({"type": "source", "t": learned, "device": device, "ssrc": ssrc}) + "\n")
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()

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
        if status is None:
            # Gravação acabou: o arquivo fecha e a próxima abre outro, com o mapa daquele momento.
            self.close()
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
            for event in events:
                if event["type"] == "device":
                    changed = (self.devices.get(event["device"]) or (None,))[0] != event["name"]
                    if changed:
                        self.devices[event["device"]] = (event["name"], learned)
                else:
                    changed = (self.sources.get(event["ssrc"]) or (None,))[0] != event["device"]
                    if changed:
                        self.sources[event["ssrc"]] = (event["device"], learned)
                if changed and status is not None and self.file is not None:
                    self._write({**event, "t": learned})
        elif kind == "levels":
            frames = msg.get("frames")
            if status != "recording" or not isinstance(frames, list) or not frames:
                return
            clean = []
            for frame in frames[:5000]:
                if (isinstance(frame, list) and len(frame) == 3
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in frame)
                        and all(math.isfinite(x) for x in frame)):
                    ts, ssrc, level = frame
                    if 0 < ssrc < 2 ** 32 and 0 <= level <= 1 and abs(ts / 1000.0 - now) < 3600:
                        clean.append([round(ts / 1000.0, 3), int(ssrc), round(float(level), 3)])
            if clean:
                self._write({"type": "levels", "t": now, "frames": clean})
        elif kind == "replay_incomplete":
            # A página não guardou o histórico inteiro: o mapa reenviado pode estar velho.
            self.note_error("replay")
        elif kind == "bye":
            self.close()

    def close(self) -> None:
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
    session = session or Session()
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


def snapshot(state: Dict[str, Any], stopped_at: float, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
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
        return None
    wanted = meet_code(state.get("current_meeting"))
    if wanted in candidates:
        code = wanted
    elif len(candidates) == 1:
        code = next(iter(candidates))
    else:
        return None
    pauses = _pause_windows(start, state.get("pauses"), end=stopped_at)
    devices, sources, frames, late, errors = {}, {}, [], set(), 0
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
                # Voz durante a pausa não entra no Bronze.
                frames.extend(f for f in record.get("frames") or []
                              if isinstance(f, list) and len(f) == 3 and all(_number(x) for x in f)
                              and lo <= f[0] <= hi and not any(a <= f[0] <= b for a, b in pauses))
            elif kind == "error" and _number(t) and t <= hi:
                errors += 1
            elif kind == "session" and record.get("errors_before"):
                errors += 1
    for history in list(sources.values()) + list(devices.values()):
        history.sort(key=lambda item: item[0])
    frames.sort()
    if not frames:
        return None
    # Fonte que só ganhou dono depois da gravação era um participante: concorre sem nome.
    return {"version": 2, "decoder": DECODER_VERSION, "source": EVIDENCE_SOURCE, "meeting_code": code,
            "window": [lo, hi], "recording": [start, stopped_at], "devices": devices, "sources": sources,
            "late_sources": sorted(late - set(sources)), "errors": errors, "frames": frames}


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


def speaking_intervals(snap: Dict[str, Any], exclude: Iterable[Tuple[float, float]] = ()) -> List[Tuple[float, float, Optional[str]]]:
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
    active = [(ts, str(ssrc)) for ts, ssrc, level in snap.get("frames") or []
              if level >= SPEAKING_LEVEL and not any(a <= ts <= b for a, b in exclude)]
    owned = sorted(ts for ts, ssrc in active if sources.get(ssrc) and _owner(sources[ssrc], ts))

    def someone_owned_near(ts):
        i = bisect.bisect_left(owned, ts - FRAME_SPAN)
        return i < len(owned) and owned[i] <= ts + FRAME_SPAN

    for ts, ssrc in active:
        history = sources.get(ssrc)
        if not history and ssrc not in late and someone_owned_near(ts):
            # Fonte que nunca teve dono, junto de quem tem: a mistura do Meet.
            # Sozinha, é alguém que o host não mapeou (reinício, mensagem perdida).
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
    level_at: Dict[int, float] = {}
    for ts, _, level in snap.get("frames") or []:
        key = int(math.floor(ts / step))
        level_at[key] = level_at.get(key, 0.0) + level
    out = []
    steps = int(round(LAG_WINDOW / step))
    for a, b, paused in audio_segments(start, pauses):
        lo, hi = int(math.ceil(a / step)), min(len(envelope), int(b / step) if math.isfinite(b) else len(envelope))
        piece = envelope[lo:hi]
        walls = [start + i * step + paused for i in range(lo, hi)]
        best = (0.0, -1.0)
        for k in range(-steps, steps + 1):
            lag = k * step
            meet = [level_at.get(int(math.floor((w - lag) / step)), 0.0) for w in walls]
            r = _pearson(piece, meet)
            if r > best[1] + 1e-9 or (abs(r - best[1]) <= 1e-9 and abs(lag) < abs(best[0])):
                best = (round(lag, 3), r)
        out.append(best)
    return out


def attribute(utterances: List[Dict[str, Any]], snap: Dict[str, Any], start: float,
              pauses=None, lags: Optional[List[Tuple[float, float]]] = None) -> Dict[int, Dict[str, Any]]:
    """Índice da fala → {nome, cobertura}. Só o canal do sistema; o microfone nunca.

    Fala que atravessa uma pausa, trecho com correlação fraca, fonte sem nome
    dominante ou outra voz relevante no mesmo intervalo: fica sem nome.
    """
    segments = audio_segments(start, pauses)
    pause_walls = _pause_windows(start, pauses)
    intervals = speaking_intervals(snap, exclude=pause_walls)
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
        lag, correlation = (lags[segment] if lags and segment < len(lags) else (0.0, 0.0))
        if correlation < MIN_CORRELATION:
            continue
        shift = start + segments[segment][2] - lag
        lo, hi = u_start + shift - EDGE_TOLERANCE, u_end + shift + EDGE_TOLERANCE
        overlap: Dict[Optional[str], float] = {}
        for a, b, name in intervals:
            if b <= lo or a >= hi:
                continue
            overlap[name] = overlap.get(name, 0.0) + min(b, hi) - max(a, lo)
        named = sorted(((n, v) for n, v in overlap.items() if n is not None), key=lambda kv: -kv[1])
        if not named:
            continue
        name, covered = named[0]
        others = sum(v for n, v in overlap.items() if n != name)
        width = hi - lo
        coverage = covered / width
        if coverage < MIN_COVERAGE or others > MAX_OTHER_SHARE * width or (others and covered < DOMINANCE * others):
            continue
        result[index] = {"name": name, "coverage": round(coverage, 3)}
    return result


def annotate(utterances: List[Dict[str, Any]], result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cópia das falas, na ordem da transcrição, com o nome atribuído e a evidência."""
    ordered = sorted(utterances, key=lambda s: (s.get("start", 0), s.get("end", 0), s.get("channel", 0)))
    names = (result or {}).get("names") or {}
    out = []
    for index, utterance in enumerate(ordered):
        item = dict(utterance)
        hit = names.get(str(index))
        if hit:
            item.update(attributed_name=hit["name"], attribution_source=EVIDENCE_SOURCE,
                        attribution_coverage=hit["coverage"])
        out.append(item)
    return out


def _render(utterances: List[Dict[str, Any]], names: Dict[int, Dict[str, Any]]) -> str:
    lines = []
    for index, s in enumerate(utterances):
        speaker = f"{names[index]['name']} (Meet)" if index in names else s.get("speaker")
        lines.append(f"[{float(s['start']):.2f}s–{float(s['end']):.2f}s] {speaker}: {s.get('text', '')}")
    return "\n".join(lines)


def _speakers_dir(bronze: Path) -> Path:
    return bronze / ".speakers"


def attribution_for_job(bronze: Path, job: Dict[str, Any], envelope_fn=None) -> Optional[Dict[str, Any]]:
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
    key = hashlib.sha256(json.dumps([ALGORITHM_VERSION, snap, ordered, pauses, job.get("recorded_at")],
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
    names = attribute(ordered, snap, start, pauses, lags)
    remote = sum(1 for s in ordered if s.get("origin") == REMOTE_ORIGIN)
    spoken: Dict[str, int] = {}
    for item in names.values():
        spoken[item["name"]] = spoken.get(item["name"], 0) + 1
    # Presença observada = teve voz dentro da gravação (sem a margem do recorte);
    # quem só estava no mapa fica de fora.
    rec_lo, rec_hi = snap.get("recording") or [start, math.inf]
    roster = sorted({name for a, b, name in speaking_intervals(snap, exclude=_pause_windows(start, pauses, end=rec_hi))
                     if name and b > rec_lo and a < rec_hi})
    result = {
        "key": key, "version": ALGORITHM_VERSION, "source": EVIDENCE_SOURCE, "decoder": snap.get("decoder"),
        "meeting_code": snap.get("meeting_code"),
        "segments": [{"lag_seconds": lag, "correlation": round(r, 3)} for lag, r in lags],
        "remote_utterances": remote, "attributed": len(names),
        "participants": [{"name": n, "utterances": spoken.get(n, 0)} for n in roster],
        "names": {str(i): v for i, v in names.items()},
        "text": _render(ordered, names) if names else None,
    }
    try:
        from castanha.durability import write_json
        write_json(cache, result)
    except OSError:
        pass
    return result if names else None


def save_snapshot(bronze: Path, audio_sha256: str, state: Dict[str, Any], stopped_at: float) -> Optional[Path]:
    """Copia para o Bronze o trecho do Meet desta gravação. Falha nunca derruba o stop."""
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", audio_sha256 or ""):
            return None
        snap = snapshot(state, stopped_at)
        if not snap:
            return None
        from castanha.durability import write_json
        path = _speakers_dir(bronze) / f"{audio_sha256}.json"
        write_json(path, snap)
        return path
    except Exception as exc:  # noqa: BLE001 - evidência opcional, gravação é o que importa
        print(f"[Castanha] Linha do tempo do Meet não salva: {type(exc).__name__}", file=sys.stderr)
        return None


# ------------------------------------------------------------- instalação

def _flags_file() -> Path:
    return Path.home() / ".config" / "chromium-flags.conf"


def _native_manifest() -> Path:
    return Path.home() / ".config" / "chromium" / "NativeMessagingHosts" / f"{HOST_NAME}.json"


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
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
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
