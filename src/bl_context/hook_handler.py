"""Bounded command hook. Never imports embeddings or performs model work."""
import argparse
import json
import sys

from .capture import receive
from .codex_hooks import handler_version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation-id',required=True)
    parser.add_argument('--generation',required=True)
    parser.add_argument('--handler-version',required=True)
    args = parser.parse_args()
    try:
        if args.handler_version != handler_version():
            raise ValueError('Hook handler changed; reinstall and review its updated definition in Codex /hooks')
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError('Hook input exceeds 64 KiB')
        result = receive(json.loads(payload),args.installation_id,args.generation)
    except Exception as exc:
        # Advisory failure: never block the engineer or initiate a continuation.
        print(f'Context capture deferred: {exc}',file=sys.stderr)
        result = {}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
