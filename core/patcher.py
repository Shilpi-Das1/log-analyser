"""Patch generation for any text file in a project repository.

The model only emits OLD/NEW blocks; the unified diff is built locally with difflib, so line
numbers and context are never taken from the model. Every OLD block must match the real file
exactly, otherwise generation fails instead of producing a bad patch. The accepted blocks are
returned so the knowledge base can replay a verified fix on current code without an LLM call.
"""
import difflib
import re
from pathlib import Path

from core import config, llm
from core.errors import PatchError

_BLOCK_RE = re.compile(r"<<<OLD\r?\n(.*?)\r?\n\|\|\|\r?\n(.*?)\r?\n?>>>NEW", re.DOTALL)
_SYSTEM = ("You are a careful engineer producing minimal code changes. "
           "Output only change blocks in the exact requested format.")


def resolve_target(root: Path, rel: str) -> Path:
    base = root.resolve()
    path = (base / rel).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise PatchError("Target file is outside the project repository.") from exc
    if not path.is_file():
        raise PatchError(f"Target file not found in the project repository: {rel}")
    return path


def _apply(original: str, blocks: list[tuple[str, str]]) -> str:
    updated = original
    for old, new in blocks:
        if not old.strip():
            raise PatchError("A change block has an empty OLD part.")
        if old == new:
            continue
        count = updated.count(old)
        if count == 0:
            raise PatchError("A change block does not match the file exactly; refusing to guess.")
        if count > 1:
            raise PatchError("A change block matches multiple places in the file.")
        updated = updated.replace(old, new, 1)
    return updated


def make_diff(rel: str, original: str, updated: str) -> str:
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="\n"))


def _build(root: Path, rel: str, blocks: list[tuple[str, str]]) -> dict:
    original = resolve_target(root, rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if len(original) > config.MAX_PATCH_SRC_CHARS:
        raise PatchError("Target file is too large for patch generation.")
    updated = _apply(original, blocks)
    diff = make_diff(rel, original, updated)
    if not diff:
        raise PatchError("The change produces no difference.")
    return {"file": rel, "diff": diff, "new_content": updated,
            "blocks": [[o, n] for o, n in blocks if o != n]}


def replay(root: Path, rel: str, blocks: list[list[str]]) -> dict:
    """Re-apply stored blocks (knowledge-base fix) to the current file, no LLM involved."""
    return {**_build(root, rel, [(o, n) for o, n in blocks]), "tokens": 0, "from_kb": True}


def generate(root: Path, rel: str, classification: dict, user_notes: str = "") -> dict:
    original = resolve_target(root, rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if len(original) > config.MAX_PATCH_SRC_CHARS:
        raise PatchError("Target file is too large for patch generation.")
    prompt = (
        f"File: {rel}\n\n##SOURCE##\n{original}\n##ENDSOURCE##\n\n"
        f"Root cause: {classification.get('root_cause', '')}\n"
        f"Fix to apply: {classification.get('fix_suggestion', '')}\n"
        + (f"Extra guidance: {user_notes}\n" if user_notes else "")
        + "\nOutput ONLY change blocks, one per change, in exactly this format:\n"
          "<<<OLD\n<lines copied byte-for-byte from ##SOURCE##, 3-6 lines, unique>\n|||\n"
          "<replacement lines>\n>>>NEW\n"
          "No prose, no markdown fences. OLD and NEW must differ. Keep the change minimal."
    )
    result = llm.chat([{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
                      max_tokens=1500, temperature=0.1)
    blocks = [(o, n) for o, n in _BLOCK_RE.findall(result["content"]) if o.strip() != n.strip()]
    if not blocks:
        raise PatchError("The model did not return a usable change. Add guidance and retry.")
    built = _build(root, rel, blocks)
    built["tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return built
