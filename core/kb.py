"""Knowledge base: a short problem statement + fix per project, matched without any LLM call.

Matching uses a normalized error signature (timestamps, ids, addresses and numbers removed) and
Jaccard token similarity. Only entries with status "verified" and a stored patch are replayed
automatically; every other match is passed to the LLM as a hint.
"""
import json
import re
import sqlite3
from contextlib import closing

from core import config
from core.errors import AppError
from core.extract import ERROR_RE

STATUSES = ("verified", "suggested", "open_issue")
_RANK = {"verified": 3, "suggested": 2, "open_issue": 1}

_SCHEMA = """CREATE TABLE IF NOT EXISTS kb (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, signature TEXT NOT NULL,
    norm TEXT NOT NULL, problem TEXT NOT NULL, category TEXT DEFAULT '', fix_text TEXT DEFAULT '',
    patch_file TEXT DEFAULT '', patch_blocks TEXT DEFAULT '', status TEXT NOT NULL,
    link TEXT DEFAULT '', hits INTEGER DEFAULT 0, created TEXT, updated TEXT)"""


def _conn() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / "knowledge.db", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _public(row: sqlite3.Row) -> dict:
    data = dict(row)
    data.pop("norm", None)
    data["patch_blocks"] = json.loads(data["patch_blocks"]) if data.get("patch_blocks") else []
    return data


def signature_of(log: str) -> str:
    """Short, stable description of the error taken from a failure log."""
    lines = [l.strip() for l in (log or "").splitlines() if l.strip()]
    e_lines = [l[1:].strip() for l in lines if re.match(r"^E\s", l)]
    if e_lines:
        chosen = e_lines[:3]
    else:
        hits = [l for l in lines if ERROR_RE.search(l)]
        chosen = list(dict.fromkeys(hits[:2] + hits[-1:])) if hits else lines[-3:]
    return " | ".join(chosen)[:400]


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?z?", " ", t)
    t = re.sub(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b", " ", t)
    t = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", " ", t)
    t = re.sub(r"\b0x[0-9a-f]+\b", " ", t)
    t = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", " ", t)
    t = re.sub(r"\b[0-9a-f]{12,}\b", " ", t)
    t = re.sub(r"\d+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm: str) -> set[str]:
    return set(re.findall(r"[a-z_]{2,}", norm))


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _tokens(na), _tokens(nb)
    if len(ta) < 2 or len(tb) < 2:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 3)


def search(project: str, signature: str, limit: int = 3, min_score: float = 0.4) -> list[dict]:
    with closing(_conn()) as conn:
        rows = conn.execute("SELECT * FROM kb WHERE project = ?", (project,)).fetchall()
    scored = []
    for row in rows:
        score = similarity(signature, row["signature"])
        if score >= min_score:
            entry = _public(row)
            entry["score"] = score
            scored.append(entry)
    scored.sort(key=lambda e: (e["score"], _RANK[e["status"]]), reverse=True)
    return scored[:limit]


def reusable(hints: list[dict]) -> dict | None:
    """First hint that can be replayed without the LLM (verified, has a patch, similar enough)."""
    for hint in hints:
        if (hint["status"] == "verified" and hint["patch_blocks"] and hint["patch_file"]
                and hint["score"] >= config.KB_MATCH_THRESHOLD):
            return hint
    return None


def record(project: str, signature: str, problem: str, category: str = "", fix_text: str = "",
           status: str = "suggested", patch_file: str = "", patch_blocks: list | None = None,
           link: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown KB status: {status}")
    if not (signature or "").strip() or not (problem or "").strip():
        raise AppError("A knowledge-base entry needs a problem statement and an error signature.")
    from core import store
    now = store.now()
    blocks = json.dumps(patch_blocks or [])
    with closing(_conn()) as conn:
        existing = conn.execute("SELECT * FROM kb WHERE project = ?", (project,)).fetchall()
        match = next((r for r in existing if similarity(signature, r["signature"]) >= 0.95), None)
        if match:
            if _RANK[status] >= _RANK[match["status"]]:
                conn.execute(
                    "UPDATE kb SET problem=?, category=?, fix_text=?, patch_file=?, patch_blocks=?, "
                    "status=?, link=?, updated=? WHERE id=?",
                    (problem[:200], category, fix_text, patch_file, blocks, status, link or match["link"], now, match["id"]))
            entry_id = match["id"]
        else:
            cur = conn.execute(
                "INSERT INTO kb (project, signature, norm, problem, category, fix_text, patch_file, "
                "patch_blocks, status, link, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (project, signature, normalize(signature), problem[:200], category, fix_text,
                 patch_file, blocks, status, link, now, now))
            entry_id = cur.lastrowid
        conn.commit()
        return _public(conn.execute("SELECT * FROM kb WHERE id = ?", (entry_id,)).fetchone())


def touch(entry_id: int) -> None:
    with closing(_conn()) as conn:
        conn.execute("UPDATE kb SET hits = hits + 1 WHERE id = ?", (entry_id,))
        conn.commit()


def list_entries(project: str = "") -> list[dict]:
    with closing(_conn()) as conn:
        if project:
            rows = conn.execute("SELECT * FROM kb WHERE project = ? ORDER BY updated DESC", (project,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM kb ORDER BY updated DESC").fetchall()
    return [_public(r) for r in rows]


def delete(entry_id: int) -> None:
    with closing(_conn()) as conn:
        cur = conn.execute("DELETE FROM kb WHERE id = ?", (entry_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise AppError(f"Knowledge-base entry {entry_id} not found.")
