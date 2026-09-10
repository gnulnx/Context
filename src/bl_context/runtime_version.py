"""Fingerprint executable package code, including editable development changes."""
import hashlib
from pathlib import Path


def runtime_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
