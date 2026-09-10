"""Read-only fresh Codex app-server queries. Never writes hook trust."""
import asyncio
import json
import os
import shutil


def query(method, root, cwd):
    async def perform():
        executable = shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex CLI unavailable on PATH')
        child = await asyncio.create_subprocess_exec(executable,'app-server','--stdio',
            env={**os.environ,'CODEX_HOME':str(root)}, cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,limit=4*1024*1024)
        async def request(identifier, name, params):
            child.stdin.write((json.dumps({'id':identifier,'method':name,'params':params})+'\n').encode())
            await child.stdin.drain()
            while True:
                line = await asyncio.wait_for(child.stdout.readline(),10)
                if not line:
                    raise RuntimeError('Codex closed before discovery completed')
                result = json.loads(line)
                if result.get('id') == identifier:
                    if 'error' in result:
                        raise RuntimeError(str(result['error']))
                    return result['result']
        try:
            await request(1,'initialize',{'clientInfo':{'name':'blctx-doctor','version':'1'}})
            child.stdin.write(b'{"method":"initialized"}\n')
            return await request(2,method,{'cwds':[str(cwd)]})
        finally:
            if child.returncode is None:
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(),3)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()
    return asyncio.run(perform())
