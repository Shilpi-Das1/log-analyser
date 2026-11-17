"""File-based JSON store for runs and cases (shared by the web app and the MCP server)."""
import json
import re
import uuid
from datetime import datetime, timezone

from core import config
from core.errors import AppError

_ID_RE = re.compile(r"^[0-9a-f]{10}$")
_LIMITS = {"cases": lambda: config.MAX_CASES, "runs": lambda: config.MAX_RUNS}


def _dir(kind: str):
    path = config.DATA_DIR / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(kind: str, obj: dict) -> None:
    path = _dir(kind) / f"{obj['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)
    _prune(kind)


def load(kind: str, obj_id: str) -> dict:
    if not _ID_RE.match(obj_id or ""):
        raise AppError(f"Invalid {kind[:-1]} id.")
    path = _dir(kind) / f"{obj_id}.json"
    if not path.exists():
        raise AppError(f"{kind[:-1].capitalize()} {obj_id} not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def list_all(kind: str) -> list[dict]:
    """All stored objects, newest first."""
    files = sorted(_dir(kind).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def _prune(kind: str) -> None:
    files = sorted(_dir(kind).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[_LIMITS[kind]():]:
        old.unlink(missing_ok=True)
