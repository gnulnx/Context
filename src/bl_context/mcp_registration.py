"""Owned Codex registration using the supported CLI; preserve unrelated config."""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    import tomllib
except ImportError:
    import tomli as tomllib
from . import storage

NAME = 'base-layer-context'
TOOLS = {
    'recent_context',
    'search_context',
    'get_context',
    'context_status',
    'open_session',
    'log_update',
}


def root():
    return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().absolute()


def config(directory):
    path = directory/'config.toml'
    storage.safe_path(path)
    return tomllib.loads(path.read_text()) if path.exists() else {}


def run(executable, directory, *args):
    result = subprocess.run([executable, 'mcp', *args], env={**os.environ, 'CODEX_HOME':str(directory)}, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Codex MCP command failed')
    return result.stdout


def expected(paths, manifest):
    return {'command':manifest['interpreter'], 'args':['-m','bl_context.mcp_server','--installation-id',manifest['installation_id']],
            'env':{f'XDG_{key.upper()}_HOME':str(path.parent) for key,path in paths.items()}}


def unrelated(document):
    document = {**document}
    servers = dict(document.get('mcp_servers', {}))
    servers.pop(NAME, None)
    if servers:
        document['mcp_servers'] = servers
    else:
        document.pop('mcp_servers', None)
    return document


def install():
    storage.verify()
    paths = storage.locations()
    directory = root()
    executable = shutil.which('codex')
    if not executable:
        raise RuntimeError('Codex CLI not found on PATH; install Codex before registering MCP')
    executable = str(Path(executable).absolute())
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        before = config(directory)
        saved = before.get('mcp_servers', {}).get(NAME)
        owned = manifest.get('mcp_registration')
        target = expected(paths, manifest)
        if owned and owned['root'] != str(directory):
            raise RuntimeError('Registration belongs to another CODEX_HOME; uninstall that registration first')
        if saved is not None and (not owned or saved != owned['config']):
            raise RuntimeError('MCP name collision or modified registration; preserving existing entry')
        if saved is not None and saved != target:
            run(executable, directory, 'remove', NAME)
        manifest['mcp_registration'] = {'root':str(directory), 'codex':executable, 'config':target}
        storage.atomic_manifest(paths, manifest)
        if saved != target:
            args = ['add', NAME]
            for key, value in target['env'].items():
                args.extend(['--env', f'{key}={value}'])
            run(executable, directory, *args, '--', target['command'], *target['args'])
        if unrelated(before) != unrelated(config(directory)):
            raise RuntimeError('Codex changed unrelated configuration; inspect config.toml')
    verify()


def verify():
    storage.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    owned = manifest.get('mcp_registration')
    if not owned:
        raise RuntimeError('Context MCP is not registered; run blctx install codex --step codex_mcp')
    if str(root()) != owned['root']:
        raise RuntimeError('Current CODEX_HOME differs from the owned registration root')
    if config(Path(owned['root'])).get('mcp_servers', {}).get(NAME) != expected(paths, manifest):
        raise RuntimeError('Saved MCP registration does not match this installation')
    result = json.loads(run(owned['codex'], Path(owned['root']), 'get', NAME, '--json'))
    transport = result.get('transport', {})
    target = expected(paths, manifest)
    if not result.get('enabled') or transport.get('type') != 'stdio' or any(transport.get(k) != v for k,v in target.items()):
        raise RuntimeError('Codex effective registration is disabled or has the wrong executable/environment')
    return 'Owned Codex MCP registration and absolute executable verified.'


async def probe_tool(configuration, name, arguments, error_log):
    parameters = StdioServerParameters(
        command=configuration['command'],
        args=configuration['args'],
        env={**os.environ, **configuration['env']},
    )
    async with stdio_client(parameters, errlog=error_log) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            if names != TOOLS:
                raise RuntimeError(
                    f'MCP tool contract mismatch: expected {sorted(TOOLS)}, got {sorted(names)}'
                )
            response = await client.call_tool(name, arguments)
            if response.isError or not isinstance(response.structuredContent, dict):
                detail = 'MCP tool returned an error'
                if response.content:
                    detail = getattr(response.content[0], 'text', detail)
                raise RuntimeError(detail)
            return response.structuredContent


def call_tool(name, arguments=None):
    storage.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    configuration = expected(paths, manifest)
    with open(os.devnull, 'w', encoding='utf-8') as error_log:
        try:
            return asyncio.run(
                asyncio.wait_for(
                    probe_tool(configuration, name, arguments or {}, error_log), 20
                )
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError('MCP stdio health check timed out after 20 seconds') from exc


def health():
    verify()
    status = call_tool('context_status')
    if (
        not isinstance(status.get('sources'), list)
        or type(status.get('messages')) is not int
        or type(status.get('chunks')) is not int
    ):
        raise RuntimeError('MCP context_status returned an invalid payload')
    return (
        f'MCP stdio initialized, {len(TOOLS)} tools verified, and daemon round-trip '
        f'completed ({status["messages"]} messages).'
    )


def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists():
        return
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('mcp_registration')
        if not owned:
            return
        directory = Path(owned['root'])
        before = config(directory)
        saved = before.get('mcp_servers', {}).get(NAME)
        if saved is not None:
            if saved != owned['config']:
                raise RuntimeError('Owned MCP registration was modified; refusing to remove it')
            run(owned['codex'], directory, 'remove', NAME)
        after = config(directory)
        if NAME in after.get('mcp_servers', {}) or unrelated(before) != unrelated(after):
            raise RuntimeError('MCP removal verification failed')
        manifest.pop('mcp_registration')
        storage.atomic_manifest(paths, manifest)
