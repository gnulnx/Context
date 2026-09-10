"""Install and verify the Context historical-recall Codex skill."""
import hashlib
import os
from pathlib import Path
from . import storage

SKILL_NAME = "base-layer-context"
SKILL_VERSION = "0.1.0"

def source():
    """Read the editable repository skill source before every install/verify."""
    path = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"
    if not path.is_file():
        raise RuntimeError(f"Skill source is missing: {path}")
    return path.read_text()

def root():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()

def target():
    return root() / "skills" / SKILL_NAME / "SKILL.md"

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def install():
    skill_text = source(); storage.verify(); paths = storage.locations(); skill_file = target()
    with storage.locked(paths):
        manifest = storage.read_manifest(paths); owned = manifest.get("codex_skill")
        if owned and owned.get("root") != str(root()):
            raise RuntimeError("Skill belongs to another CODEX_HOME; uninstall that installation first")
        if skill_file.exists():
            if not owned or skill_file.read_text() != skill_text:
                raise RuntimeError(f"Codex skill path already exists or was modified; preserving it: {skill_file}")
        else:
            skill_file.parent.mkdir(parents=True, exist_ok=True); skill_file.write_text(skill_text); os.chmod(skill_file, 0o600)
        manifest["codex_skill"] = {"root": str(root()), "path": str(skill_file), "name": SKILL_NAME, "version": SKILL_VERSION, "sha256": digest(skill_text)}
        storage.atomic_manifest(paths, manifest)
    verify()

def verify():
    skill_text = source(); storage.verify(); manifest = storage.read_manifest(storage.locations()); owned = manifest.get("codex_skill")
    if not owned: raise RuntimeError("Context Codex skill is not installed; run blctx install codex --step codex_skills")
    path = Path(owned.get("path", ""))
    if owned.get("root") != str(root()) or path != target() or not path.is_file(): raise RuntimeError("Context Codex skill ownership path is invalid or unavailable")
    if owned.get("name") != SKILL_NAME or owned.get("version") != SKILL_VERSION or path.read_text() != skill_text or owned.get("sha256") != digest(skill_text): raise RuntimeError("Context Codex skill content or version was modified")
    return f"Codex skill {SKILL_NAME} v{SKILL_VERSION} content and ownership verified."

def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists(): return
    with storage.locked(paths):
        manifest = storage.read_manifest(paths); owned = manifest.get("codex_skill")
        if not owned: return
        path = Path(owned["path"])
        if path.exists():
            if path.read_text() != source(): raise RuntimeError("Owned Codex skill was modified; refusing to remove it")
            path.unlink()
            try: path.parent.rmdir()
            except OSError: pass
        manifest.pop("codex_skill"); storage.atomic_manifest(paths, manifest)
