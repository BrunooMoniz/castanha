"""Biblioteca local somente leitura; nenhum provedor, rede ou caminho vindo do texto."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from castanha.storage import AUDIO_EXTENSIONS, MeetingStorage, _title_from_slug

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class LibraryError(ValueError):
    pass


def _component(value):
    if (not isinstance(value, str) or not value or value.startswith('.') or len(value) > 255
            or '/' in value or '\\' in value or '\x00' in value):
        raise LibraryError('Identificador de reunião inválido.')
    return value


def _open(root, *parts):
    """Percorre por descritores, sem seguir symlinks de componentes internos."""
    fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            _component(part)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(root, *parts, json_data=False):
    try:
        fd = _open(root, *parts)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
            raise LibraryError('Arquivo local inválido ou maior que o limite de leitura.')
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise LibraryError('Arquivo maior que o limite de leitura.')
        value = json.loads(data) if json_data else data.decode('utf-8')
        if json_data and not isinstance(value, dict):
            raise LibraryError('Estrutura do arquivo local inválida.')
        return value
    finally:
        if fd is not None:
            os.close(fd)


def _valid_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _number(value):
    return float(value) if _valid_number(value) else 0


def _text(value):
    return value if isinstance(value, str) else ''


def _notes(silver):
    value = re.sub(r'\A---\r?\n.*?\r?\n---\r?\n', '', silver, count=1, flags=re.S)
    return re.split(r'(?:\A|\n)## 📝 Transcrição[^\n]*', value, maxsplit=1)[0].strip()


class MeetingLibrary:
    def __init__(self, storage=None):
        self.storage = storage or MeetingStorage()

    def _load(self, root, *parts, warnings, label, json_data=False):
        try:
            return _read(root, *parts, json_data=json_data)
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(f'{label}: não foi possível ler o arquivo local.')
            return None

    def _audio(self, slug, metadata, warnings):
        try:
            directory = _open(self.storage.bronze_dir, slug)
        except FileNotFoundError:
            return []
        except OSError:
            warnings.append('Áudio: pasta local indisponível.')
            return []
        recordings = []
        records = metadata.get('recordings') if isinstance(metadata.get('recordings'), list) else []
        by_name = {r.get('filename'): r for r in records if isinstance(r, dict) and isinstance(r.get('filename'), str)}
        try:
            if not stat.S_ISDIR(os.fstat(directory).st_mode):
                warnings.append('Áudio: pasta local inválida.')
                return []
            for name in sorted(os.listdir(directory)):
                if name.startswith('.') or Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except OSError:
                    warnings.append('Áudio: um arquivo ficou indisponível durante a leitura.')
                    continue
                if not stat.S_ISREG(info.st_mode):
                    warnings.append('Áudio: link ou arquivo especial recusado.')
                    continue
                record = by_name.get(name, {})
                recordings.append({'id': name, 'name': name,
                    'url': (self.storage.bronze_dir.resolve() / slug / name).as_uri(),
                    'size_bytes': info.st_size, 'duration_seconds': _number(record.get('duration_seconds')),
                    'job_id': _text(record.get('job_id')), 'sha256': _text(record.get('sha256'))})
        finally:
            os.close(directory)
        return recordings

    def _entry(self, slug):
        warnings = []
        meta = self._load(self.storage.bronze_dir, slug, 'metadata.json', warnings=warnings,
                          label='Metadados', json_data=True) or {}
        # Só stat, sem carregar resumo/transcrição no inventário.
        present = {}
        for key, root, parts in (
            ('has_summary', self.storage.silver_dir, (slug + '.md',)),
            ('has_transcript', self.storage.bronze_dir, (slug, 'transcript_raw.txt'))):
            try:
                fd = _open(root, *parts)
                try:
                    info = os.fstat(fd)
                    present[key] = stat.S_ISREG(info.st_mode) and info.st_size > 0
                finally:
                    os.close(fd)
            except FileNotFoundError:
                present[key] = False
            except OSError:
                present[key] = False
                warnings.append('Um arquivo da reunião não pôde ser acessado.')
        audio = self._audio(slug, meta, warnings)
        delivery = meta.get('zinom') if isinstance(meta.get('zinom'), dict) else {}
        pending = (bool(warnings) or meta.get('audio_status') in ('sem_audio', 'mic_mudo')
                   or meta.get('processing_status') in ('pending', 'processing', 'error', 'failed')
                   or meta.get('summary_status') in ('pending', 'error', 'failed') or meta.get('transcription_pending') is True
                   or delivery.get('status') not in ('ok', 'success', 'synced', 'disabled')
                   or delivery.get('facts_status') in ('pending', 'pending_lineage', 'error', 'failed')
                   or not present['has_summary'])
        manual = meta.get('source') == 'manual'
        if manual:
            pending = bool(warnings) or not present['has_summary'] or meta.get('summary_status') in ('pending', 'error', 'failed')
        entry = {'source': _text(meta.get('source')), 'slug': slug, 'title': _text(meta.get('title')) or _title_from_slug(slug),
                 'when': _text(meta.get('recorded_at')), 'duration_seconds': _number(meta.get('duration_seconds')),
                 'status': 'pending' if pending else 'complete',
                 'status_label': ('Resumo local pronto' if manual and not pending else 'Anotações locais' if manual else 'Gravação sem áudio detectado' if meta.get('audio_status') == 'sem_audio'
                     else 'Microfone sem áudio na gravação' if meta.get('audio_status') == 'mic_mudo'
                     else 'Notas prontas · entrega não confirmada'
                     if present['has_summary'] and delivery.get('status') not in ('ok', 'success', 'synced', 'disabled')
                     else 'Pendente' if pending else 'Concluída'),
                 'recordings_count': len(audio), 'warnings': warnings, **present}
        return entry, meta, audio

    def list(self):
        slugs = set()
        skipped = 0
        try:
            for root, extension in ((self.storage.bronze_dir, None), (self.storage.silver_dir, '.md'), (self.storage.gold_dir, '.json')):
                with os.scandir(root) as entries:
                    for entry in entries:
                        if entry.name.startswith('.'):
                            continue
                        try:
                            if extension is None and entry.is_dir(follow_symlinks=False):
                                slugs.add(_component(entry.name))
                            elif extension and entry.is_file(follow_symlinks=False) and entry.name.endswith(extension):
                                slugs.add(_component(Path(entry.name).stem))
                        except LibraryError:
                            skipped += 1
        except (OSError, LibraryError) as exc:
            raise LibraryError('Não foi possível carregar o histórico. Os arquivos permanecem preservados.') from exc
        meetings = [self._entry(slug)[0] for slug in slugs]
        meetings.sort(key=lambda item: (item['when'] or item['slug'], item['slug']), reverse=True)
        return {'status': 'ok', 'meetings': meetings,
                'warnings': ([f'{skipped} entrada(s) com identificador inválido foram preservadas e não exibidas.'] if skipped else [])}

    def detail(self, slug):
        slug = _component(slug)
        entry, meta, recordings = self._entry(slug)
        warnings = entry['warnings']
        silver = self._load(self.storage.silver_dir, slug + '.md', warnings=warnings, label='Resumo')
        gold = self._load(self.storage.gold_dir, slug + '.json', warnings=warnings, label='Decisões', json_data=True)
        transcript = self._load(self.storage.bronze_dir, slug, 'transcript_raw.txt', warnings=warnings, label='Transcrição')
        timeline = self._load(self.storage.bronze_dir, slug, 'transcript_segments.json', warnings=warnings,
                              label='Tempos da transcrição', json_data=True) or {}
        if silver is None and gold is None and transcript is None and not recordings and not meta:
            raise LibraryError('Reunião não encontrada ou indisponível para leitura.')
        decisions, actions = [], []
        for value in (gold or {}).get('decisions', []) if isinstance((gold or {}).get('decisions', []), list) else []:
            line = value if isinstance(value, str) else value.get('decision') if isinstance(value, dict) else ''
            if isinstance(line, str) and line.strip():
                decisions.append(line)
        for value in (gold or {}).get('action_items', []) if isinstance((gold or {}).get('action_items', []), list) else []:
            if isinstance(value, str):
                actions.append(value)
            elif isinstance(value, dict) and isinstance(value.get('task'), str):
                line = value['task']
                if isinstance(value.get('assignee'), str) and value['assignee']:
                    line += ' · Responsável: ' + value['assignee']
                if isinstance(value.get('deadline'), str) and value['deadline']:
                    line += ' · Prazo: ' + value['deadline']
                actions.append(line)
        segments = self._segments(slug, timeline, recordings, warnings)
        # Hash/job são usados internamente para vincular tempos; não são UX.
        audio = [{k: v for k, v in recording.items() if k not in ('sha256', 'job_id')} for recording in recordings]
        return {'status': 'ok', 'meeting': {**entry, 'summary': _notes(silver or ''),
                'decisions': decisions, 'action_items': actions, 'transcript': transcript or '',
                'segments': segments, 'recordings': audio, 'warnings': warnings}}

    def _segments(self, slug, timeline, recordings, warnings):
        segments = []
        sources = timeline.get('recordings', [])
        if not isinstance(sources, list):
            warnings.append('Tempos da transcrição: formato indisponível.')
            return segments
        for source in sources:
            if not isinstance(source, dict):
                continue
            index = -1
            matches = [(i, record) for i, record in enumerate(recordings)
                       if record['job_id'] and record['job_id'] == source.get('job_id')
                       and re.fullmatch('[0-9a-f]{64}', record['sha256'])
                       and record['sha256'] == source.get('source_sha256')]
            if len(matches) == 1 and source.get('time_reference') == 'recording_start':
                candidate, record = matches[0]
                try:
                    fd = _open(self.storage.bronze_dir, slug, record['id'])
                    with os.fdopen(fd, 'rb') as handle:
                        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                            raise OSError('Arquivo de áudio inválido')
                        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    if digest == source.get('source_sha256'):
                        index = candidate
                except OSError:
                    warnings.append('Áudio: não foi possível conferir a origem dos tempos.')
            utterances = source.get('utterances', [])
            for utterance in utterances if isinstance(utterances, list) else []:
                if not isinstance(utterance, dict) or not isinstance(utterance.get('text'), str):
                    continue
                start, end = utterance.get('start'), utterance.get('end')
                valid_time = _valid_number(start) and _valid_number(end) and end >= start
                channel = utterance.get('channel')
                if not isinstance(channel, int) or isinstance(channel, bool):
                    channel = None
                proven = source.get('channel_provenance') is True
                label = ('Microfone local' if channel == 0 else 'Áudio remoto' if channel == 1 else 'Canal não identificado') if proven else 'Origem não identificada'
                segments.append({'text': utterance['text'], 'start': start if valid_time else None,
                    'end': end if valid_time else None, 'channel': label, 'audio_index': index,
                    'can_seek': valid_time and index >= 0})
        return segments
