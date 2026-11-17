"""Log format detection and failure-excerpt extraction (format-agnostic)."""
import re

from core import config

ERROR_RE = re.compile(
    r"error|exception|fail|fatal|traceback|panic|assert|timed out|timeout|refused|denied|"
    r"not found|segfault|oom|killed|cannot|unable|abort",
    re.IGNORECASE,
)

_FORMAT_RULES = [
    ("pytest", re.compile(r"=+ (FAILURES|short test summary info|test session starts) =+|\w\.py::\w")),
    ("python", re.compile(r"Traceback \(most recent call last\)")),
    ("java", re.compile(r"\bat\s+[\w.$]+\([\w$]+\.(java|kt|scala|groovy):\d+\)|Exception in thread")),
    ("junit", re.compile(r"<testsuite|<testcase")),
    ("go", re.compile(r"^--- FAIL:|^panic: |goroutine \d+ \[", re.MULTILINE)),
    ("shell", re.compile(r"\.(sh|bash): line \d+|^\+ \w", re.MULTILINE)),
]


def detect_format(text: str) -> str:
    sample = text[:200_000]
    for name, rule in _FORMAT_RULES:
        if rule.search(sample):
            return name
    return "generic"


def clip(text: str, limit: int | None = None) -> str:
    limit = limit or config.MAX_EXCERPT_CHARS
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]


def build_excerpt(text: str, fmt: str) -> str:
    """Return the part of the log most likely to explain the failure."""
    lines = text.splitlines()
    if not lines:
        return ""

    if fmt == "pytest":
        start = next((i for i, l in enumerate(lines) if re.match(r"=+ FAILURES =+", l)), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(lines))
                        if re.match(r"=+ short test summary info =+", lines[i])), len(lines))
            return clip("\n".join(lines[start:end] + lines[end:end + 40]))

    hits = [i for i, l in enumerate(lines) if ERROR_RE.search(l)]
    keep: set[int] = set()
    for i in hits[:6] + hits[-4:]:
        keep.update(range(max(0, i - 6), min(len(lines), i + 7)))
    keep.update(range(max(0, len(lines) - 25), len(lines)))

    out: list[str] = []
    previous = -2
    for i in sorted(keep):
        if previous != -2 and i != previous + 1:
            out.append("...")
        out.append(lines[i])
        previous = i
    return clip("\n".join(out))
