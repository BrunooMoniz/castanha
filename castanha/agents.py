"""Compartilhar as notas de uma reunião com um agente externo.

Os agentes vêm de `agents` na config: [{"id": "...", "name": "...", "command": ["/caminho/do/programa", ...]}].
Sem agente configurado, a biblioteca não mostra nada. O Castanha não copia arquivo nenhum: só diz ao programa
do agente qual nota (`<slug>.md` no Silver) entra ou sai. Protocolo, sempre JSON na saída e sem shell:

  <command> status                         {"audiences": [{"id", "label"}], "shared": {"<slug>.md": {"audience", "auto"}}}
  <command> share <slug>.md [<audience>]   {"status": "ok" | "pending" | "error", "message": "..."}
  <command> unshare <slug>.md              idem

`status` tem de ser local e rápido: a biblioteca consulta ao abrir. `auto` marca a nota que o agente recebe por
regra dele (a biblioteca mostra, mas não oferece "deixar de compartilhar").
"""
import json
import os
import subprocess

from castanha.config import load_config
from castanha.library import LibraryError, MeetingLibrary, _component, _open

STATUS_TIMEOUT = 5
SHARE_TIMEOUT = 120
MAX_OUTPUT = 1024 * 1024


def configured(config=None):
    """Agentes válidos da config; entrada malformada é ignorada, nunca executada."""
    agents, seen = [], set()
    for item in (config if config is not None else load_config()).get('agents') or []:
        if not isinstance(item, dict):
            continue
        ident, command = item.get('id'), item.get('command')
        if (not isinstance(ident, str) or not ident.strip() or ident in seen or not isinstance(command, list)
                or not command or not all(isinstance(part, str) and part for part in command)):
            continue
        seen.add(ident)
        name = item.get('name')
        agents.append({'id': ident, 'name': name.strip() if isinstance(name, str) and name.strip() else ident,
                       'command': command})
    return agents


def _call(agent, args, timeout):
    """Roda o programa do agente e devolve o JSON dele; qualquer falha vira LibraryError legível."""
    try:
        done = subprocess.run(agent['command'] + args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise LibraryError(f"{agent['name']} não respondeu a tempo.")
    except OSError:
        raise LibraryError(f"Não foi possível rodar o programa de {agent['name']}.")
    try:
        result = json.loads(done.stdout[:MAX_OUTPUT].decode('utf-8'))
    except (ValueError, UnicodeError):
        result = None
    if not isinstance(result, dict):
        raise LibraryError(f"{agent['name']} devolveu uma resposta que o Castanha não entende.")
    return result, done.returncode


def status(config=None):
    """O que cada agente recebe, por slug. A falha de um agente não esconde os outros."""
    out = []
    for agent in configured(config):
        entry = {'id': agent['id'], 'name': agent['name'], 'audiences': [], 'shared': {}, 'error': ''}
        try:
            result, code = _call(agent, ['status'], STATUS_TIMEOUT)
            if code != 0:
                raise LibraryError(f"{agent['name']} não informou o que recebe.")
            for audience in result.get('audiences') if isinstance(result.get('audiences'), list) else []:
                if isinstance(audience, dict) and isinstance(audience.get('id'), str) and audience['id']:
                    label = audience.get('label')
                    entry['audiences'].append({'id': audience['id'],
                                               'label': label if isinstance(label, str) and label else audience['id']})
            shared = result.get('shared') if isinstance(result.get('shared'), dict) else {}
            for name, info in shared.items():
                if isinstance(name, str) and name.endswith('.md') and isinstance(info, dict):
                    audience = info.get('audience')
                    entry['shared'][name[:-3]] = {'audience': audience if isinstance(audience, str) else '',
                                                  'auto': info.get('auto') is True}
        except LibraryError as exc:
            entry['error'] = str(exc)
        out.append(entry)
    return {'status': 'ok', 'agents': out}


def share(slug, agent_id, audience=None, remove=False, config=None, storage=None):
    slug = _component(slug)
    agent = next((a for a in configured(config) if a['id'] == agent_id), None)
    if agent is None:
        raise LibraryError('Agente não configurado.')
    if audience is not None:
        _component(audience)
    if not remove:
        # Só nota que existe: o agente recebe o resumo e a transcrição, nunca uma promessa de nota.
        try:
            os.close(_open((storage or MeetingLibrary().storage).silver_dir, slug + '.md'))
        except OSError:
            raise LibraryError('Esta reunião ainda não tem notas para compartilhar.')
    args = ['unshare', slug + '.md'] if remove else ['share', slug + '.md'] + ([audience] if audience else [])
    result, code = _call(agent, args, SHARE_TIMEOUT)
    state = result.get('status')
    message = result.get('message') if isinstance(result.get('message'), str) else ''
    if code != 0 or state not in ('ok', 'pending'):
        raise LibraryError(message or f"{agent['name']} recusou o pedido.")
    return {'status': state, 'agent': agent['id'], 'slug': slug, 'shared': not remove,
            'message': message or (f"{agent['name']} deixou de receber esta reunião." if remove
                                   else f"Reunião compartilhada com {agent['name']}.")}
