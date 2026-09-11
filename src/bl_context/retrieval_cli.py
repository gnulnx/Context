"""CLI clients for the daemon's indexing and retrieval protocol."""
import json
import os
import socket
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from . import storage
from .embedding import MODEL


def call(request):
    try:
        storage.verify()
        endpoint = storage.locations()['state'] / 'daemon.sock'
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('Not a private Context socket')
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(35)
            channel.connect(str(endpoint))
            channel.sendall(json.dumps(request).encode() + b'\n')
            response = bytearray()
            while not response.endswith(b'\n'):
                block = channel.recv(65536)
                if not block:
                    raise RuntimeError('Daemon disconnected')
                response.extend(block)
                if len(response) > 16 * 1024 * 1024:
                    raise RuntimeError('Response exceeds 16 MiB')
        result = json.loads(response)
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(f'{exc}. Check blctx doctor --step background_service.') from exc


def emit(result):
    click.echo(json.dumps(result, ensure_ascii=True, indent=2))


@click.command('index')
@click.argument('session', required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--root', type=click.Path(exists=True, file_okay=False, path_type=Path), help='Index active and archived JSONL files in this Codex root.')
@click.option('--wait', is_flag=True, help='Wait up to ten minutes for this durable job.')
def index_command(session, root, wait):
    """Queue transcript indexing; defaults to the local Codex root. Emits JSON."""
    if session and root:
        raise click.UsageError('Choose a session file or --root, not both')
    if session:
        sources = [str(session.absolute())]
    else:
        root = (root or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')).expanduser().absolute()
        if not root.is_dir():
            raise click.ClickException(f'Codex root does not exist: {root}')
        sources = sorted(str(p) for kind in ('sessions', 'archived_sessions') for p in (root / kind).rglob('*.jsonl'))
    if not sources:
        emit({'state': 'empty', 'sources': 0})
        return
    result = call({'operation': 'index', 'sources': sources})
    if wait:
        click.echo(
            f"Queued {result['job_id']}; indexing {len(sources)} source(s). "
            f"The configured model is {MODEL}.",
            err=True,
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            result = call({'operation': 'index_status', 'job_id': result.get('job_id') or result['id']})
            if result['state'] not in ('queued', 'running'):
                break
            time.sleep(.5)
    emit(result)
    if result['state'] in ('partial', 'failed'):
        raise click.exceptions.Exit(1)


@click.command('index-status')
@click.argument('job_id', required=False)
def index_status(job_id):
    """Show import coverage and freshness, or the state of one durable job."""
    emit(call({'operation': 'index_status', 'job_id': job_id}))


def filters(function):
    for option in [click.option('--project', help='Exact project/cwd path.'),
                   click.option('--since', help='Inclusive ISO timestamp/date; UTC if no offset.'),
                   click.option('--until', help='Exclusive ISO timestamp/date; UTC if no offset.'),
                   click.option('--limit', type=click.IntRange(1, 50), default=10),
                   click.option('--offset', type=click.IntRange(0, 100000), default=0)]:
        function = option(function)
    return function


@click.command('recent')
@filters
@click.option('--days', type=click.IntRange(1, 36500), default=3, help='Lookback in days when --since is absent.')
def recent(days, **options):
    """Retrieve recent searchable turns with source references. Emits JSON."""
    options['since'] = options['since'] or (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    emit(call({'operation': 'recent_context', **options}))


@click.command('search')
@click.argument('query')
@filters
def search(query, **options):
    """Search global local history, with optional project/time filters. Emits JSON."""
    emit(call({'operation': 'search_context', 'query': query, **options}))


@click.command('context')
@click.option('--char-offset', type=click.IntRange(min=0), default=0, help='Continue a long message; use with --limit 1 and its message --offset.')
@click.argument('context_id')
@click.option('--limit', type=click.IntRange(1, 50), default=20)
@click.option('--offset', type=click.IntRange(0, 100000), default=0)
def context(context_id, limit, offset, char_offset):
    """Expand a result's context_id into stored messages, including commentary."""
    emit(call({'operation': 'get_context', 'context_id': context_id, 'limit': limit, 'offset': offset, 'char_offset': char_offset}))
