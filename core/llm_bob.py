"""Bob Shell backend: runs the `bob` CLI headless and returns the same shape as the HTTP client.

Supports bob 2.x (`bob run --format json ...`) and bob 1.x (`bob --output-format json ...`).

Safety notes
- The CLI runs in an empty workspace directory (DATA_DIR/bob-workspace) in `ask` mode by default,
  so it cannot read or edit project files. Override the mode with BOB_SHELL_MODE.
- The prompt is sent on stdin, never as an argument: Windows limits a command line to ~8 KB (and
  cmd.exe would interpret `&`, `%`, `^` in it), while a patch prompt can carry a whole source file.
Token counts are estimates (1 token ~ 4 chars); the CLI does not report real usage.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import config
from core.errors import LLMError

_version: tuple[int, int] | None = None


def find_bob() -> str:
    """Executable name to launch, or '' when bob is not on PATH."""
    for name in (("bob.cmd", "bob") if sys.platform == "win32" else ("bob",)):
        if shutil.which(name):
            return name
    return ""


def _version_of(exe: str) -> tuple[int, int]:
    global _version
    if _version is None:
        try:
            result = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
        except (OSError, subprocess.SubprocessError):
            return (2, 0)
        match = re.match(r"\s*(\d+)\.(\d+)", result.stdout)
        _version = (int(match.group(1)), int(match.group(2))) if match else (2, 0)
    return _version


def _env() -> dict:
    env = os.environ.copy()
    if config.BOBSHELL_API_KEY:
        env["BOBSHELL_API_KEY"] = config.BOBSHELL_API_KEY
    env.setdefault("NODE_OPTIONS", "--use-openssl-ca")      # trust the system CA bundle (corporate proxies)
    return env


def _workspace() -> Path:
    path = config.DATA_DIR / "bob-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def collapse(messages: list[dict]) -> str:
    """Flatten chat messages into one prompt: system text, prior turns as context, last turn as the question."""
    system = [m["content"] for m in messages if m["role"] == "system"]
    turns = [m for m in messages if m["role"] != "system"]
    parts = list(system[-1:])
    if len(turns) == 1:
        parts.append(turns[0]["content"])
    elif turns:
        prior = "\n\n".join(f"[{'USER' if m['role'] == 'user' else 'ASSISTANT'}]\n{m['content']}"
                            for m in turns[:-1])
        parts += [f"=== CONTEXT ===\n{prior}", f"=== QUESTION ===\n{turns[-1]['content']}"]
    return "\n\n".join(parts)


def _command(exe: str, major: int) -> list[str]:
    mode = config.BOB_SHELL_MODE or "ask"
    if major >= 2:
        cmd = [exe, "run", "--accept-license", "--format", "json", "--mode", mode,
               "--disable-mcp", "--disable-subagents", "--workspace", str(_workspace())]
    else:
        cmd = [exe, "--output-format", "json", "--yolo", "--hide-intermediary-output",
               "--chat-mode", mode, "-m", config.BOB_SHELL_MODEL, "-p", "-"]
    return cmd


_CONTENT_KEYS = ("last_message", "content", "result", "text", "output", "message")


def extract_content(raw: str) -> str:
    """Assistant text from the CLI's stdout (progress lines and JSON lines may be mixed)."""
    raw = raw.strip()
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()]):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            for key in _CONTENT_KEYS:
                if isinstance(obj.get(key), str) and obj[key].strip():
                    return obj[key].strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and item.get("role") == "assistant" and item.get("content"):
                return str(item["content"]).strip()
    return raw


def clean(content: str) -> str:
    """Strip CLI noise: ---output--- markers, <thinking> blocks and [using tool ...] lines."""
    if "---output---" in content:
        first = content.index("---output---") + len("---output---")
        last = content.rindex("---output---")
        content = content[first:last if last > first else None].strip()
    thinking = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
    outside = thinking.sub("", content).strip()
    if "<<<OLD" not in outside and "<<<OLD" in content:
        # the model put the change blocks inside its thinking block; keep them
        inside = "\n".join(re.sub(r"</?thinking>", "", t, flags=re.IGNORECASE).strip()
                           for t in thinking.findall(content))
        outside = inside if "<<<OLD" in inside else outside
    return re.sub(r"^\[using tool[^\]]*\]\s*$", "", outside, flags=re.MULTILINE).strip()


def chat(messages: list[dict], max_tokens: int | None = None, temperature: float = 0.2) -> dict:
    """Same contract as the HTTP client. `max_tokens` and `temperature` are not supported by the CLI."""
    exe = find_bob()
    if not exe:
        raise LLMError("The 'bob' CLI was not found on PATH. Install Bob Shell, or set LLM_BACKEND=http.")
    if not config.BOBSHELL_API_KEY:
        raise LLMError("BOBSHELL_API_KEY is not set (copy .env.example to .env and add the key).")

    prompt = collapse(messages)
    major, _ = _version_of(exe)
    started = time.perf_counter()
    try:
        result = subprocess.run(_command(exe, major), input=prompt, capture_output=True, text=True,
                                encoding="utf-8", timeout=config.LLM_TIMEOUT_S, env=_env(),
                                cwd=_workspace())
    except subprocess.TimeoutExpired as exc:
        raise LLMError("Bob Shell timed out.") from exc
    except OSError as exc:
        raise LLMError(f"Could not launch '{exe}': {exc}") from exc

    if result.returncode != 0:
        combined = (result.stderr + "\n" + result.stdout).strip()
        if "IDEClient" in combined or "ide companion" in combined.lower():
            raise LLMError("This bob version needs the Bob IDE companion extension running. "
                           "Use bob 2.x, or set LLM_BACKEND=http.")
        raise LLMError(f"bob exited with code {result.returncode}: {combined[:600]}")

    content = clean(extract_content(result.stdout))
    if not content:
        raise LLMError("Bob Shell returned an empty reply.")
    return {"content": content, "prompt_tokens": max(1, len(prompt) // 4),
            "completion_tokens": max(1, len(content) // 4),
            "elapsed_s": round(time.perf_counter() - started, 2)}
