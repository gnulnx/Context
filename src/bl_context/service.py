"""Provider-independent service lifecycle, selected by the host operating system."""
import sys

from . import launchd_service, retrieval_cli, storage, systemd_service
from .service_runtime import identity

BACKENDS = {'linux': systemd_service, 'darwin': launchd_service}


def backend_for(platform=None):
    try:
        return BACKENDS[platform or sys.platform]
    except KeyError:
        raise RuntimeError('Background services support Linux and macOS.') from None


def install():
    return backend_for().install()


def verify():
    return backend_for().verify()


def uninstall():
    return backend_for().uninstall()


def diagnostics():
    return backend_for().DIAGNOSTICS


def health():
    verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    response = retrieval_cli.call({'operation': 'health'})
    expected = {
        'status': 'ok',
        'installation_id': manifest['installation_id'],
        'schema_version': storage.VERSION,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Daemon health response does not match this installation')
    if type(response.get('running_jobs')) is not int or response['running_jobs'] < 0:
        raise RuntimeError('Daemon health response has an invalid job count')
    if type(response.get('authored_updates_pending')) is not int or response['authored_updates_pending'] < 0:
        raise RuntimeError('Daemon health response has an invalid update count')
    if response.get('index_state') not in ('ready', 'syncing'):
        raise RuntimeError('Daemon health response has an invalid index state')
    if response.get('query_mode') not in ('hybrid', 'lexical_fallback'):
        raise RuntimeError('Daemon health response has an invalid query mode')
    pid = identity(paths)['pid']
    recall = (
        'Global recall ready.'
        if response['index_state'] == 'ready'
        else 'Global recall available while semantic vectors sync'
        + (
            f" ({response['authored_updates_pending']} updates pending)."
            if response['authored_updates_pending']
            else '.'
        )
    )
    return (
        f'Daemon protocol, private database, and installation identity verified '
        f'(PID {pid}). {recall}'
    )
