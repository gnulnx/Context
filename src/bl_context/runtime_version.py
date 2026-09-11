"""Fingerprint executable package code, including editable development changes."""
import hashlib
from pathlib import Path


def runtime_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def hook_handler_version():
    """Version capture code without importing provider/installer dependencies."""
    digest = hashlib.sha256()
    for name in ('capture.py', 'hook_handler.py'):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()
