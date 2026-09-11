"""Exclusão de conteúdo com quarentena, invalidação e limpeza remota durável."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from castanha.capture_gate import capture_start
from castanha.annotations import (_directory, _read, _write, _json, _write_json, _metadata, AnnotationError)

POINTER = '.recording-exclusion.json'
ARCHIVE = '.recording-exclusions'
ROOT_DOCUMENTS = ('metadata.json', 'transcript_raw.txt', 'transcript_segments.json', '.annotations-regeneration.json')


class ExclusionError(AnnotationError):
    pass


class ExclusionConflict(ExclusionError):
    pass


@contextmanager
def _child(fd, name, create=False):
    if name in ('', '.', '..') or '/' in name or '\\' in name or '\x00' in name:
        raise ExclusionError('Componente de caminho inválido')
    if create:
        try: os.mkdir(name, 0o700, dir_fd=fd)
        except FileExistsError: pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try: yield child
    finally: os.close(child)


@contextmanager
def _archive(fd, identity, create=False):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise ExclusionError('Identidade da exclusão inválida')
    with _child(fd, ARCHIVE, create) as parent, _child(parent, identity, create) as target:
        yield target


def _audio_hash(fd, name):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(source, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode): raise ExclusionError('Áudio deve ser arquivo regular')
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024): digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ExclusionError('Áudio mudou durante a leitura')
        return digest.hexdigest()


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _load(fd):
    pointer = _json(fd, POINTER)
    if pointer is None: return None
    with _archive(fd, pointer.get('id')) as archive:
        op = _json(archive, 'operation.json')
    if not op or op.get('id') != pointer.get('id') or op.get('version') != 1:
        raise ExclusionError('Registro de exclusão inválido; arquivos preservados')
    return op


def _save(fd, op):
    with _archive(fd, op['id']) as archive:
        _write_json(archive, 'operation.json', op)


def _capture_guard(slug):
    from castanha.state import StateManager
    state = StateManager().read()
    if (state.get('capture_slug') == slug or state.get('target_meeting_slug') == slug) and state.get('status') in ('recording', 'paused', 'processing'):
        raise ExclusionError('Aguarde a gravação ou o processamento ativo desta reunião terminar')


def _walk(fd, prefix=()):
    result = []
    for name in sorted(os.listdir(fd)):
        if name == '.processing.lock': continue
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with _child(fd, name) as nested: result += _walk(nested, prefix + (name,))
        elif stat.S_ISREG(info.st_mode):
            result.append((prefix + (name,), _read(fd, name)))
        else:
            raise ExclusionError('Arquivo de processamento não regular; exclusão interrompida')
    return result


@contextmanager
def _parent(fd, parts, create=False):
    if len(parts) == 1:
        yield fd
    else:
        with _child(fd, parts[0], create) as child, _parent(child, parts[1:], create) as parent:
            yield parent


def _snapshots(fd, storage, slug, archive):
    files = []
    def keep(root, parts, text):
        name = _sha(root + '/' + '/'.join(parts)) + '.snapshot'
        if text is not None: _write(archive, name, text)
        files.append({'root': root, 'parts': list(parts), 'backup': name,
                      'sha256': _sha(text) if text is not None else None})
    for name in ROOT_DOCUMENTS: keep('bronze', (name,), _read(fd, name))
    for name in ('.jobs', '.brain-ingest'):
        try:
            with _child(fd, name) as child:
                for parts, text in _walk(child): keep('bronze', (name,) + parts, text)
        except FileNotFoundError: pass
    for root, directory, name in (('silver', storage.silver_dir, slug + '.md'), ('gold', storage.gold_dir, slug + '.json')):
        out = os.open(directory.resolve(), os.O_RDONLY | os.O_DIRECTORY)
        try: keep(root, (name,), _read(out, name))
        finally: os.close(out)
    return files


def _backup(archive, item):
    if item['sha256'] is None: return None
    text = _read(archive, item['backup'])
    if text is None or _sha(text) != item['sha256']:
        raise ExclusionError('Snapshot divergente; exclusão interrompida')
    return text


def _targets(files, archive, removed_job, metadata):
    from castanha.bronze_ingest import revision_fingerprint
    removed_source = 'castanha:' + _sha(removed_job) if removed_job else None
    targets, retained, destination = {}, [], None
    for item in files:
        parts = item['parts']
        if item['root'] != 'bronze' or len(parts) != 2 or parts[0] != '.brain-ingest': continue
        if parts[1] == 'destination.json':
            destination = json.loads(_backup(archive, item)); retained.append(item); continue
        if not re.fullmatch(r'[a-f0-9]{64}\.json', parts[1]): continue
        saved = json.loads(_backup(archive, item))
        request = saved.get('request') or {}
        envelope = request.get('envelope') or {}
        if parts[1] != revision_fingerprint(request) + '.json' or envelope.get('source_type') != 'castanha':
            raise ExclusionError('Recibo de origem divergente; exclusão interrompida')
        source = envelope.get('source_id')
        remove = envelope.get('fidelidade') == 'sintese' or source == removed_source
        if remove:
            if saved.get('attempted') is True or saved.get('status') in ('ok', 'tombstoned', 'superseded') or saved.get('remote_identity'):
                if not isinstance(source, str) or not re.fullmatch('castanha:[a-f0-9]{64}', source):
                    raise ExclusionError('Fonte remota inválida')
                targets[source] = {'source_type': 'castanha', 'source_id': source,
                    'workspace': envelope.get('workspace'), 'reason': 'Gravação excluída pelo usuário; invalidar conteúdo anterior da reunião',
                    'confirm': True}
        else:
            if envelope.get('fidelidade') != 'projecao':
                raise ExclusionError('Fidelidade de origem desconhecida')
            retained.append(item)
    if targets:
        if not destination or any(t['workspace'] != destination.get('workspace') for t in targets.values()):
            raise ExclusionError('Escopo remoto não comprovado; arquivos preservados')
    previous = metadata.get('zinom') or {}
    unscoped = bool(previous.get('remember_id')) or (not targets and bool((previous.get('source') or {}).get('revisions')))
    return list(targets.values()), retained, destination, unscoped


def _projection(metadata, op):
    return {key: metadata.get(key) for key in ('content_status', 'recording_revision', 'excluded_recording',
        'exclusion_id', 'remaining_count', 'can_reprocess', 'cleanup_status', 'cleanup_reason', 'processing_status',
        'can_restore', 'restore_reason', 'remote_cleanup_required')} | {'slug': op['slug'], 'status': 'ok'}


@capture_start
def exclude_recording(slug, filename, storage=None, expected_revision=None):
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    if not isinstance(filename, str) or Path(filename).name != filename or '\\' in filename or filename in ('', '.', '..'):
        raise ExclusionError('Nome da gravação inválido')
    _capture_guard(slug)
    with _directory(storage, slug, lock=True) as fd:
        _capture_guard(slug)
        metadata = _metadata(fd, slug)
        revision = metadata.get('recording_revision', 0)
        if type(revision) is not int or revision < 0: raise ExclusionError('Revisão da reunião inválida')
        if expected_revision is not None and expected_revision != revision:
            raise ExclusionConflict('A reunião mudou; recarregue antes de excluir')
        old = _load(fd)
        if old and old.get('phase') not in ('done', 'restored'):
            raise ExclusionError('Conclua ou restaure a exclusão anterior antes de excluir outra gravação')
        if '.legacy-recovery' in os.listdir(fd):
            raise ExclusionError('Origem legada congelada exige recuperação específica; nada alterado')
        records = storage.list_meeting_recordings(slug)
        selected = [r for r in records if r['filename'] == filename]
        if len(selected) != 1: raise ExclusionError('Gravação não encontrada nesta reunião')
        info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode): raise ExclusionError('Áudio deve ser arquivo regular')
        jobs = []
        try:
            with _child(fd, '.jobs') as child:
                for name in os.listdir(child):
                    if name.endswith('.json'):
                        job = _json(child, name)
                        if Path(job.get('audio_path', '')).parent != storage.bronze_dir / slug:
                            raise ExclusionError('Job fora da reunião; exclusão interrompida')
                        jobs.append((name, job))
        except FileNotFoundError: pass
        matching = [(name, job) for name, job in jobs if Path(job['audio_path']).name == filename]
        if jobs and len(matching) != 1: raise ExclusionError('Origem nativa ausente ou ambígua')
        removed_job = matching[0][1]['id'] if matching else None
        identity = uuid.uuid4().hex
        with _archive(fd, identity, create=True) as archive:
            files = _snapshots(fd, storage, slug, archive)
            targets, retained, destination, unscoped = _targets(files, archive, removed_job, metadata)
            remaining = [r for r in records if r['filename'] != filename]
            digest = _audio_hash(fd, filename)
            updated = {**metadata, 'recording_revision': revision + 1, 'excluded_recording': filename,
                'exclusion_id': identity, 'remaining_count': len(remaining), 'can_reprocess': bool(remaining),
                'content_status': 'invalidated' if remaining else 'empty',
                'processing_status': 'invalidated' if remaining else 'empty', 'summary_status': 'invalidated' if remaining else 'unavailable',
                'transcription_status': 'pending' if remaining else 'unavailable', 'transcription_pending': False,
                'summary_error': '', 'transcription_error': None, 'transcription_pending_reason': None,
                'recordings': [r for r in metadata.get('recordings', []) if r.get('filename') != filename],
                'recordings_count': len(remaining), 'bronze_audio_file': remaining[0]['path'] if remaining else None,
                'duration_seconds': sum(r.get('duration_seconds', 0) for r in remaining),
                'audio_status': metadata.get('audio_status', 'desconhecido') if remaining else 'audio_apagado',
                'cleanup_status': 'pending' if targets or unscoped else 'not_needed',
                'cleanup_reason': 'Retirada do conteúdo anterior do Zinom pendente' if targets or unscoped else '',
                'remote_cleanup_required': bool(targets or unscoped), 'can_restore': True, 'restore_reason': '',
                'removed_job_ids': sorted(set(metadata.get('removed_job_ids', []) + ([removed_job] if removed_job else []))),
                'zinom': {'status': 'pending_cleanup' if targets or unscoped else 'invalidated' if remaining else 'not_needed',
                          'reason': 'Conteúdo anterior invalidado pela exclusão da gravação'}}
            updated.pop('memory_recording_ids', None)
            if not jobs:
                for record in updated['recordings']: record['transcribed'] = False
            op = {'version': 1, 'id': identity, 'slug': slug, 'filename': filename, 'audio_sha256': digest,
                'phase': 'applying', 'files': files, 'retained_receipts': retained, 'targets': targets,
                'destination': destination, 'unscoped_remote': unscoped, 'attempted': False, 'acked': [],
                'removed_job': removed_job, 'removed_job_file': matching[0][0] if matching else None,
                'remaining': [r['filename'] for r in remaining], 'reprocess_requested': False, 'new_metadata': updated}
            _write_json(archive, 'operation.json', op)
        _write_json(fd, POINTER, {'id': identity})
        _apply(fd, storage, op)
        return _projection(_metadata(fd, slug), op)


def _apply(fd, storage, op):
    with _archive(fd, op['id']) as archive:
        before_meta = next(item['sha256'] for item in op['files'] if item['parts'] == ['metadata.json'])
        if _sha(_read(fd, 'metadata.json')) not in (before_meta, _sha(json.dumps(op['new_metadata'], ensure_ascii=False, indent=2))):
            raise ExclusionConflict('A reunião mudou durante a exclusão; retome com revisão')
        _write_json(fd, 'metadata.json', op['new_metadata'])
        try:
            info = os.stat(op['filename'], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if _audio_hash(archive, 'audio') != op['audio_sha256']:
                raise ExclusionError('Áudio de recuperação ausente')
        else:
            if not stat.S_ISREG(info.st_mode) or _audio_hash(fd, op['filename']) != op['audio_sha256']:
                raise ExclusionError('Áudio mudou antes da quarentena')
            os.rename(op['filename'], 'audio', src_dir_fd=fd, dst_dir_fd=archive)
            os.fsync(archive); os.fsync(fd)
        for item in op['files']:
            root, parts = item['root'], item['parts']
            delete = (root in ('silver', 'gold') or parts[0] in ('transcript_raw.txt', 'transcript_segments.json', '.annotations-regeneration.json')
                      or parts[0] == '.brain-ingest' or parts == ['.jobs', op['removed_job_file']])
            if not delete or item['sha256'] is None: continue
            out = fd if root == 'bronze' else os.open(getattr(storage, root + '_dir').resolve(), os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _parent(out, parts) as parent:
                    current = _read(parent, parts[-1])
                    if current is not None:
                        if _sha(current) != item['sha256']: raise ExclusionError('Documento mudou durante a exclusão')
                        os.unlink(parts[-1], dir_fd=parent); os.fsync(parent)
            finally:
                if out != fd: os.close(out)
        for item in op['retained_receipts']:
            with _parent(fd, item['parts'], create=True) as parent:
                _write(parent, item['parts'][-1], _backup(archive, item))
        # Conteúdo legado agregado não tem associação segura por arquivo.
        if op['removed_job'] is None:
            try:
                with _child(fd, '.jobs') as child: os.unlink('base_transcript.txt', dir_fd=child)
            except FileNotFoundError: pass
    op['phase'] = 'invalidated' if op['new_metadata']['remote_cleanup_required'] else 'done'
    op['after_metadata_sha256'] = _sha(_read(fd, 'metadata.json'))
    _save(fd, op)


def pending_exclusion(bronze):
    path = bronze / POINTER
    return path.exists() or path.is_symlink()


def applicable(slug, storage):
    with _directory(storage, slug) as fd:
        op = _load(fd)
        metadata = _metadata(fd, slug)
        return bool(op and (op['phase'] in ('applying', 'restoring') or
                    (op['phase'] != 'restored' and
                     metadata.get('recording_revision') == op['new_metadata']['recording_revision'] and
                     (metadata.get('content_status') in ('invalidated', 'empty', 'rebuilding') or
                      metadata.get('cleanup_status') == 'pending'))))


def capture_blocked(slug, storage):
    if not pending_exclusion(storage.bronze_dir / slug): return False
    with _directory(storage, slug) as fd:
        op = _load(fd)
        metadata = _metadata(fd, slug)
        return bool(op and (op['phase'] in ('applying', 'restoring') or
                            metadata.get('cleanup_status') == 'pending'))


def engine_result(result, storage):
    metadata = storage.get_meeting(result['slug']) or {}
    content = metadata.get('content_status')
    return {'status': 'empty' if content == 'empty' and result['status'] == 'ok' else
                     'success' if content == 'current' and result['status'] == 'ok' else 'partial',
            'message': result.get('message', ''),
            'result': {**metadata, **result, 'zinom': metadata.get('zinom') or {}}}


def needs_resume(slug, storage):
    with _directory(storage, slug) as fd:
        op = _load(fd)
        if not op or op['phase'] == 'restored': return False
        meta = _metadata(fd, slug)
        return (op['phase'] == 'applying' or meta.get('cleanup_status') == 'pending'
                or (op.get('reprocess_requested') and meta.get('content_status') != 'current'))


def _remote_cleanup(fd, op, metadata, adapter):
    if metadata.get('cleanup_status') != 'pending': return True
    if op.get('unscoped_remote'):
        metadata['cleanup_reason'] = 'Recibo remoto sem escopo verificável; retirada pendente de revisão'
        _write_json(fd, 'metadata.json', metadata)
        if not op.get('attempted'):
            op['after_metadata_sha256'] = _sha(_read(fd, 'metadata.json')); _save(fd, op)
        return False
    from castanha.bronze_ingest import frozen_destination
    from castanha.zinom_adapter import ZinomMcpClient, tool_json
    if (not adapter.enabled or not adapter.token or
            op['destination'] != frozen_destination(endpoint=adapter.endpoint, token=adapter.token,
                workspace=adapter.workspace, account_id=adapter.account_id)):
        metadata['cleanup_reason'] = 'Destino ou credencial mudou; retome com a configuração original'
        _write_json(fd, 'metadata.json', metadata)
        if not op.get('attempted'):
            op['after_metadata_sha256'] = _sha(_read(fd, 'metadata.json')); _save(fd, op)
        return False
    for target in op['targets']:
        if target['source_id'] in op['acked']: continue
        # Uma resposta perdida não permite prometer undo. Repetir é sempre
        # sobre a MESMA fonte e workspace congelados, sem comparar contadores.
        op['attempted'] = True
        _save(fd, op)
        metadata.update(can_restore=False, restore_reason='Retirada remota iniciada; cópia do áudio preservada na quarentena')
        _write_json(fd, 'metadata.json', metadata)
        category = 'transport'
        try:
            client = ZinomMcpClient(adapter.endpoint, adapter.token)
            client.connect()
            reply = tool_json(client.call_tool('brain_forget_source', target))
            category = 'invalid_ack'
            if reply.get('ok') is False:
                code = reply.get('error')
                category = code if code in ('workspace_forbidden', 'unauthorized', 'forbidden',
                    'feature_disabled', 'bronze_disabled', 'bronze_forget_disabled', 'account_mismatch', 'rate_limited', 'confirmation_required',
                    'source_not_found', 'invalid_request') else 'remote_rejected'
            counters = ('revisions', 'contentHashes', 'chunks', 'facts', 'profileFacts', 'rechecks')
            if (reply.get('ok') is not True or reply.get('tombstoned') is not True or
                    any(type(reply.get(k)) is not int or reply[k] < 0 for k in counters)):
                raise ExclusionError('Zinom não confirmou a retirada da origem')
        except Exception as exc:
            if isinstance(exc, TimeoutError): category = 'timeout'
            elif getattr(exc, 'code', None) in (401, 403): category = 'authorization'
            elif getattr(exc, 'code', None) == 429: category = 'rate_limited'
            op['last_error_code'] = category; _save(fd, op)
            metadata['cleanup_reason'] = 'Retirada ainda não confirmada (' + category + '); tentativa será retomada'
            _write_json(fd, 'metadata.json', metadata)
            return False
        op['acked'].append(target['source_id'])
        op.setdefault('acknowledgements', {})[target['source_id']] = reply
        _save(fd, op)
    metadata.update(cleanup_status='complete', cleanup_reason='', zinom={
        'status': 'invalidated' if op['remaining'] else 'not_needed',
        'reason': 'Conteúdo anterior retirado; aguardando novo processamento' if op['remaining'] else 'Gravação excluída e conteúdo anterior retirado'})
    _write_json(fd, 'metadata.json', metadata)
    return True


def resume_exclusion(slug, storage=None, *, engine=None, reprocess=False, adapter=None):
    from castanha.storage import MeetingStorage
    from castanha.zinom_adapter import ZinomAdapter
    storage = storage or MeetingStorage()
    _capture_guard(slug)
    with _directory(storage, slug, lock=True) as fd:
        _capture_guard(slug)
        op = _load(fd)
        if not op: raise ExclusionError('Exclusão não encontrada')
        if op['phase'] == 'restored': raise ExclusionError('Exclusão já restaurada')
        if op['phase'] == 'restoring': raise ExclusionError('Restauração interrompida; retome a restauração antes de sincronizar')
        if op['phase'] == 'applying': _apply(fd, storage, op)
        metadata = _metadata(fd, slug)
        if reprocess and op['remaining']:
            op['reprocess_requested'] = True
            _save(fd, op)
            metadata.update(can_restore=False, restore_reason='Novo processamento iniciado; cópia anterior preservada',
                            content_status='rebuilding', processing_status='pending')
            _write_json(fd, 'metadata.json', metadata)
        if not _remote_cleanup(fd, op, metadata, adapter or ZinomAdapter()):
            return {**_projection(_metadata(fd, slug), op), 'status': 'pending',
                    'message': 'Retirada do conteúdo anterior do Zinom pendente'}
        metadata = _metadata(fd, slug)
        if not op['remaining'] or not op.get('reprocess_requested'):
            op['phase'] = 'done'
            _save(fd, op)
            return {**_projection(metadata, op), 'message': ('Reunião sem gravações; anotações manuais preservadas'
                    if not op['remaining'] else 'Gravação excluída; reprocesse os áudios restantes')}
        if engine is None:
            from castanha.engine import CastanhaEngine
            engine = CastanhaEngine(); engine.storage = storage
        jobs = list((storage.bronze_dir / slug / '.jobs').glob('*.json'))
        result = engine._process_pending_locked(slug) if jobs else engine._reprocess_legacy_locked(slug)
        metadata = _metadata(fd, slug)
        complete = metadata.get('content_status') == 'current'
        delivery = (result.get('result') or {}).get('zinom') or {}
        delivered = delivery.get('status') in ('ok', 'disabled', 'local_only', 'not_needed')
        metadata.update(content_status='current' if complete else 'rebuilding', can_restore=False,
                        restore_reason='Novo processamento iniciado; cópia anterior preservada')
        _write_json(fd, 'metadata.json', metadata)
        if complete:
            op['phase'] = 'done'
            _save(fd, op)
        return {**_projection(metadata, op), 'status': 'ok' if complete and delivered else 'pending',
                'message': ('Conteúdo atualizado com os áudios restantes' if delivered else
                            'Conteúdo local atualizado; entrega ao Zinom pendente') if complete else
                           'Processamento dos áudios restantes pendente',
                'result': result.get('result', {})}


@capture_start
def restore_recording(slug, identity, storage=None):
    from castanha.storage import MeetingStorage
    storage = storage or MeetingStorage()
    _capture_guard(slug)
    with _directory(storage, slug, lock=True) as fd:
        _capture_guard(slug)
        op = _load(fd)
        if not op or op['id'] != identity: raise ExclusionConflict('A exclusão atual mudou; restauração recusada')
        if op.get('attempted') or op.get('reprocess_requested'):
            raise ExclusionError('Retirada remota ou novo processamento iniciado; áudio permanece recuperável na quarentena')
        if op['phase'] == 'restored': raise ExclusionError('Gravação já restaurada')
        if op['phase'] == 'applying': _apply(fd, storage, op)
        # Antes de restaurar qualquer byte, validar todos os snapshots e que
        # nenhum derivado novo seria sobrescrito. O Markdown autoral não entra.
        with _archive(fd, op['id']) as archive:
            originals = [(item, _backup(archive, item)) for item in op['files']]
            restored_meta = json.loads(next(text for item, text in originals if item['parts'] == ['metadata.json']))
            restored_meta['recording_revision'] = op['new_metadata']['recording_revision'] + 1
            if restored_meta.get('exclusion_id'):
                restored_meta.update(can_restore=False, restore_reason='Uma operação posterior alterou a reunião')
            accepted = {op.get('after_metadata_sha256')}
            if op['phase'] == 'restoring':
                accepted.add(_sha(json.dumps(restored_meta, ensure_ascii=False, indent=2)))
            if _sha(_read(fd, 'metadata.json')) not in accepted:
                raise ExclusionConflict('A reunião mudou após a exclusão; restauração automática recusada')
            for item, text in originals:
                root, parts = item['root'], item['parts']
                if parts == ['metadata.json']: continue
                out = fd if root == 'bronze' else os.open(getattr(storage, root + '_dir').resolve(), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    try:
                        with _parent(out, parts) as parent: current = _read(parent, parts[-1])
                    except FileNotFoundError: current = None
                    if current is not None and current != text:
                        raise ExclusionConflict('Arquivo mudou após a exclusão; restauração recusada')
                finally:
                    if out != fd: os.close(out)
            already_moved = op['filename'] in os.listdir(fd)
            if already_moved:
                if op['phase'] != 'restoring' or _audio_hash(fd, op['filename']) != op['audio_sha256']:
                    raise ExclusionConflict('Já existe áudio com esse nome')
            elif _audio_hash(archive, 'audio') != op['audio_sha256']:
                raise ExclusionError('Áudio da quarentena diverge do original')
            op['phase'] = 'restoring'; _save(fd, op)
            for item, text in originals:
                if text is None or item['parts'] == ['metadata.json']: continue
                root, parts = item['root'], item['parts']
                out = fd if root == 'bronze' else os.open(getattr(storage, root + '_dir').resolve(), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with _parent(out, parts, create=True) as parent: _write(parent, parts[-1], text)
                finally:
                    if out != fd: os.close(out)
            if not already_moved:
                os.rename('audio', op['filename'], src_dir_fd=archive, dst_dir_fd=fd)
            os.fsync(fd); os.fsync(archive)
        _write_json(fd, 'metadata.json', restored_meta)
        op['phase'] = 'restored'; _save(fd, op)
        return {'status': 'ok', 'slug': slug, 'message': 'Gravação e conteúdo anterior restaurados; anotações preservadas',
                'recording_revision': restored_meta['recording_revision'], 'can_restore': False}
