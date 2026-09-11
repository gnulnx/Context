"""Read-only transcript exploration, deliberately separate from import."""
import json
import os
from pathlib import Path

import click

from .preview import preview


def read_records(path, start, limit, view):
    records = []
    scanned = 0
    more = False
    with path.open('rb') as stream:
        boundary = os.fstat(stream.fileno()).st_size
        while stream.tell() < boundary:
            offset = stream.tell()
            line = stream.readline(boundary - offset)
            scanned += 1
            if scanned < start:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                record = {'parse_error': 'Invalid JSON or incomplete record',
                          'raw': line.decode('utf-8', errors='replace')}
            payload = record.get('payload', {}) if isinstance(record, dict) else {}
            candidate = (isinstance(record, dict) and record.get('type') == 'response_item'
                         and isinstance(payload, dict) and payload.get('type') == 'message'
                         and payload.get('role') in ('user', 'assistant'))
            error = isinstance(record, dict) and 'parse_error' in record
            if view == 'messages' and not candidate and not error:
                continue
            if len(records) == limit:
                more = True
                break
            records.append({'line': scanned, 'byte_offset': offset, 'record': record})
    return {'path': str(path), 'view': view, 'snapshot_bytes': boundary,
            'records': records, 'has_more': more,
            'next_line': scanned if more else None}


@click.command()
@click.argument('session', required=False, type=click.Path(path_type=Path))
@click.option('--root', type=click.Path(path_type=Path), help='Codex root; defaults to CODEX_HOME or ~/.codex.')
@click.option('--limit', type=click.IntRange(1, 1000), default=20, show_default=True)
@click.option('--start-line', type=click.IntRange(min=1), default=1, show_default=True)
@click.option('--view', type=click.Choice(['messages', 'raw', 'preview']), default='preview', show_default=True)
@click.option('--details', is_flag=True, help='Expand preview to all turns and per-record decisions.')
@click.option('--json', 'json_output', is_flag=True, help='Emit structured JSON only.')
@click.option('--no-color', is_flag=True, help='Plain output (already the default).')
@click.pass_context
def explore(ctx, session, root, limit, start_line, view, details, json_output, no_color):
    """List local Codex transcripts, or inspect a SESSION file without importing.

    Default preview shows proposed searchable turns and summarized exclusions.
    Messages view shows unfiltered candidates; raw view shows every record type.
    Nothing is indexed or saved.
    """
    root = (root or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')).expanduser().absolute()
    try:
        if session is None:
            if not root.is_dir():
                raise click.ClickException(f'Codex root does not exist: {root}')
            files = []
            for directory in ('sessions', 'archived_sessions'):
                for path in (root / directory).rglob('*.jsonl'):
                    info = path.stat()
                    files.append({'path': str(path), 'archived': directory == 'archived_sessions',
                                  'bytes': info.st_size, 'modified': info.st_mtime})
            files.sort(key=lambda item: (item['modified'], item['path']), reverse=True)
            data = {'root': str(root), 'total_files': len(files), 'files': files[:limit],
                    'has_more': len(files) > limit}
        else:
            if view == 'preview':
                data = preview(session.expanduser().absolute(), start_line, limit, details=details)
            else:
                data = read_records(session.expanduser().absolute(), start_line, limit, view)
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output or ctx.find_root().obj.get('json_output', False):
        click.echo(json.dumps(data, ensure_ascii=True))
        return
    if session is None:
        click.echo(f"Codex root: {root}\n{data['total_files']} transcript files; showing {len(data['files'])} newest.")
        for item in data['files']:
            click.echo(f"{'archived' if item['archived'] else 'active'}  {item['bytes']} bytes  {item['path']}")
        click.echo('\nInspect a path: blctx explore /path/to/session.jsonl')
    elif view == 'preview':
        click.echo('INGESTION PREVIEW — proposed searchable text; nothing imported.')
        click.echo(f"{data['total_searchable_turns']} searchable turns in captured file; showing {len(data['turns'])} turns.")
        click.echo('File totals: ' + ', '.join(f'{key}={value}' for key, value in data['counts'].items()))
        for turn in data['turns']:
            click.echo(f"\n--- Turn {turn['turn_id']} | project {turn['project']} ---")
            for warning in turn['warnings']:
                click.echo('Review: ' + warning)
            for message in turn['messages']:
                click.echo(f"\n{message['role'].upper()} — source line {message['line']}")
                # Escape terminal controls while retaining readable Unicode and newlines.
                text = ''.join(c if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159) else repr(c)[1:-1] for c in message['text'])
                click.echo(text)
                if message['duplicate_sources']:
                    click.echo('Duplicate representations: lines ' + ', '.join(str(r['line']) for r in message['duplicate_sources']))
            click.echo('Turn totals: ' + ', '.join(f'{k}={v}' for k, v in turn['counts'].items()))
            if turn['review']:
                click.echo('Needs review: source lines ' + ', '.join(str(r['line']) for r in turn['review']))
            if details:
                click.echo(json.dumps(turn['records'], ensure_ascii=True, indent=2))
        click.echo('\nExcluded/context/metadata/review summary:')
        for reason, count in data['excluded_summary'].items():
            click.echo(f'  {count}: {reason}')
        if data['has_more']:
            click.echo(f"\nContinue with --start-line {data['next_line']}")
        click.echo('Use --details to inspect decisions, or --view raw --start-line N --limit 1 for a source record.')
    else:
        click.echo(f"Source: {data['path']}\nView: {view}. Exploration only; no import filtering applied.")
        for item in data['records']:
            click.echo(f"\n--- source line {item['line']}, byte {item['byte_offset']} ---")
            click.echo(json.dumps(item['record'], ensure_ascii=True, indent=2))
        if data['has_more']:
            click.echo(f"\nMore records: repeat with --start-line {data['next_line']}")
        else:
            click.echo('\nEnd of captured file.')
