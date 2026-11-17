"""Language-agnostic source locator: stack-trace frames -> files in a project's repository."""
import re
from pathlib import Path

from core import config

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "target", "build",
              "dist", ".tox", ".idea", ".gradle", "vendor"}
_MAX_INDEX_FILES = 30_000
_SRC_EXT = {".py", ".java", ".kt", ".scala", ".groovy", ".go", ".sh", ".bash", ".js", ".ts",
            ".rb", ".c", ".cc", ".cpp", ".h", ".rs", ".php", ".cs", ".yaml", ".yml", ".tf"}

_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
_JAVA_FRAME = re.compile(
    r"at\s+((?:[\w$]+\.)*[\w$]+)\.[\w$<>]+\(([\w$]+\.(?:java|kt|scala|groovy)):(\d+)\)")
_GO_FRAME = re.compile(r"^\s*([\w./\\-]+\.go):(\d+)", re.MULTILINE)
_SHELL_FRAME = re.compile(r"([\w./\\-]+\.(?:sh|bash)): line (\d+)")
_GENERIC_FRAME = re.compile(r"([\w./\\-]+\.(?:py|js|ts|rb|c|cc|cpp|h|rs|php|cs)):(\d+)")
_NODE_ID = re.compile(r"([\w./\\-]+\.py)::([\w:\[\]\-.]+)")

_index_cache: dict[str, dict[str, list[Path]]] = {}


def clear_index() -> None:
    _index_cache.clear()


def _index(root: Path) -> dict[str, list[Path]]:
    key = str(root)
    if key in _index_cache:
        return _index_cache[key]
    index: dict[str, list[Path]] = {}
    count = 0
    stack = [root]
    while stack and count < _MAX_INDEX_FILES:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix.lower() in _SRC_EXT:
                index.setdefault(entry.name, []).append(entry)
                count += 1
    _index_cache[key] = index
    return index


def resolve(path_str: str, root: Path | None) -> Path | None:
    """Map a path found in a log to a file inside `root` (longest path-suffix match)."""
    if not root or not root.is_dir():
        return None
    normalized = path_str.replace("\\", "/")
    if any(t in normalized for t in ("site-packages", "dist-packages", "/usr/lib/", "/usr/local/lib/")):
        return None
    parts = [p for p in normalized.split("/") if p and p not in (".", "..")]
    if not parts:
        return None
    best, best_score = None, 0
    for cand in _index(root).get(parts[-1], []):
        cand_parts = cand.relative_to(root).as_posix().split("/")
        score = 0
        for a, b in zip(reversed(parts), reversed(cand_parts)):
            if a != b:
                break
            score += 1
        if score > best_score:
            best, best_score = cand, score
    return best


def find_frames(text: str, root: Path | None) -> list[dict]:
    """Return resolved frames [{file, line, language}] in order of appearance."""
    if not root or not root.is_dir():
        return []
    raw: list[tuple[str, int]] = []
    for m in _PY_FRAME.finditer(text):
        raw.append((m.group(1), int(m.group(2))))
    for m in _JAVA_FRAME.finditer(text):
        package = m.group(1).rsplit(".", 1)[0] if "." in m.group(1) else ""
        raw.append(((package.replace(".", "/") + "/" if package else "") + m.group(2), int(m.group(3))))
    for pattern in (_GO_FRAME, _SHELL_FRAME, _GENERIC_FRAME):
        for m in pattern.finditer(text):
            raw.append((m.group(1), int(m.group(2))))
    for m in _NODE_ID.finditer(text):
        found = resolve(m.group(1), root)
        line = _find_def_line(found, m.group(2).split("::")[-1]) if found else 0
        if line:
            raw.append((m.group(1), line))

    frames, seen = [], set()
    for path_str, line in raw:
        found = resolve(path_str, root)
        if not found or (found, line) in seen:
            continue
        seen.add((found, line))
        frames.append({"file": found.relative_to(root).as_posix(), "line": line,
                       "language": found.suffix.lstrip(".")})
    return frames


def _find_def_line(path: Path, method: str) -> int:
    pattern = re.compile(rf"\s*(async\s+)?def\s+{re.escape(re.sub(r'\[.*', '', method))}\b")
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.match(line):
                return i
    except OSError:
        pass
    return 0


def snippet(root: Path, rel: str, line: int, context: int = 25) -> dict | None:
    try:
        base = root.resolve()
        path = (base / rel).resolve()
        path.relative_to(base)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (ValueError, OSError):
        return None
    start, end = max(1, line - context), min(len(lines), line + context)
    body = "\n".join(f"{n:5d} | {lines[n - 1]}" for n in range(start, end + 1))
    return {"file": rel, "start": start, "end": end, "focus_line": line, "text": body}


def collect_sources(text: str, root: Path | None) -> tuple[list[dict], list[dict]]:
    """Return (frames, snippets): first 3 + last 3 resolved frames, capped by size."""
    frames = find_frames(text, root)
    chosen = frames if len(frames) <= 6 else frames[:3] + frames[-3:]
    snippets, used = [], 0
    for frame in chosen:
        snip = snippet(root, frame["file"], frame["line"])
        if not snip:
            continue
        if used + len(snip["text"]) > config.MAX_SOURCE_CHARS:
            break
        snippets.append(snip)
        used += len(snip["text"])
    return chosen, snippets
