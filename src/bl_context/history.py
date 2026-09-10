"""Installer orchestration for declared, daemon-owned historical imports."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

from . import storage

callback = ContextVar('history_progress', default=None)


def emit(stage, completed=0, total=0, chunks_completed=0, chunks_total=0):
    if callback.get():
        callback.get()(stage, completed, total, chunks_completed, chunks_total)


@contextmanager
def progress(console=None):
    if console is None:
        yield
        return
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeRemainingColumn
    display = Progress(SpinnerColumn(), TextColumn('{task.description}'), BarColumn(),
                       MofNCompleteColumn(), TimeRemainingColumn(), console=console)
    task = chunk_task = None
    last = None
    started = False
    last_completed = 0
    def update(stage, completed, total, chunks_completed, chunks_total):
        nonlocal task, chunk_task, last, started, last_completed
        if not console.is_terminal:
            state = (stage, completed, total)
            if state != last:
                console.print(f' {stage}: {completed}/{total} files', markup=False)
                last = state
            return
        if task is None:
            task = display.add_task(stage, total=total or None)
            chunk_task = display.add_task('Current session chunks', total=None, visible=False)
            display.start()
            started = True
        if not started:
            display.start()
            started = True
        if completed < last_completed:
            display.reset(task, total=total or None, completed=completed)
            display.reset(chunk_task, total=chunks_total or None, completed=chunks_completed)
        last_completed = completed
        display.update(task, description=stage, completed=completed, total=total or None)
        display.update(chunk_task, completed=chunks_completed, total=chunks_total or None, visible=bool(chunks_total), refresh=True)
        if stage in ('Discovery complete', 'Discovery unchanged', 'Import complete', 'Import partial'):
            display.stop()
            started = False
    token = callback.set(update)
    try:
        yield
    finally:
        callback.reset(token)
        if started:
            display.stop()


def install():
    from .discovery import inventory
    from .retrieval_cli import call
    summary, _ = inventory()
    result = call({'operation': 'index_inventory', 'inventory_id': summary['id']})
    job = result['job_id']
    try:
        while True:
            result = call({'operation':'index_status','job_id':job})
            report = result['result']
            emit(report.get('stage', 'Indexing history'), report.get('files_completed',0), report.get('files_total',summary['files']),
                 report.get('chunks_completed',0), report.get('chunks_total',0))
            if result['state'] not in ('queued','running'):
                return  # Independent verification decides whether this is green.
            time.sleep(.3)
    except KeyboardInterrupt:
        raise RuntimeError(f'Import {job} continues in the background. Inspect blctx index-status {job}; rerun install to verify or resume.') from None


def verify():
    from .discovery import inventory, freshness
    from .retrieval_cli import call
    summary, rows = inventory()
    fresh = freshness(summary, rows)
    registration = storage.read_manifest(storage.locations()).get('history_index')
    if not registration or registration['inventory_id'] != summary['id']:
        raise RuntimeError('No active import for the discovered snapshot. Run blctx install codex --step history_index.')
    state = call({'operation':'index_status', 'job_id':registration['job_id']})
    if state['state'] != 'complete':
        report = state['result']
        raise RuntimeError(f"Import {state['state']}: {report.get('files_completed',0)}/{len(rows)} files processed; "
                           f"{len(report.get('failed',[]))} failures, "
                           f"{sum(r.get('unclassified',0) for r in report.get('files',[]))} unclassified records. "
                           f"Inspect blctx index-status {registration['job_id']} and rerun install.")
    verified = call({'operation':'verify_history','inventory_id':summary['id']})
    return (f"Declared snapshot indexed: {verified['files']} files, {verified['messages']} messages, {verified['chunks']} chunks; "
            f"source-linked retrieval verified. Outside snapshot: {fresh['new_files']} new files, {fresh['appended_files']} appended files.")
