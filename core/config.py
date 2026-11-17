"""Central configuration. Reads `.env` once (real environment variables win)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Load *path* into os.environ.

    Real environment variables already set in the shell take precedence;
    .env values only fill in keys that are not yet present.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Only set if not already provided by the real environment.
        if key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def _get(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ── LLM backend: bob_shell (Bob CLI subprocess, default) or http (OpenAI-compatible endpoint) ──
LLM_BACKEND = _get("LLM_BACKEND", default="bob_shell")
BOBSHELL_API_KEY = _get("BOBSHELL_API_KEY")
BOB_SHELL_MODEL = _get("BOB_SHELL_MODEL", default="premium-ide")   # bob 1.x only
BOB_SHELL_MODE = _get("BOB_SHELL_MODE")                            # default "ask": no file access

# ── HTTP backend (LLM_BACKEND=http) ──────────────────────────────────
# LLM_* names win; BOB_* names are accepted so an existing .env can be reused.
LLM_API_KEY = _get("LLM_API_KEY", "BOB_API_KEY")
LLM_API_BASE = _get(
    "LLM_API_BASE", "BOB_API_BASE",
    default="https://api.us-east.bob.ibm.com/inference/v1/chat/completions",
)
LLM_MODEL = _get("LLM_MODEL", "BOB_MODEL", default="premium-ide")
LLM_AUTH_STYLE = _get("LLM_AUTH_STYLE", default="auto")  # auto | x-api-key | bearer
LLM_MAX_TOKENS = int(_get("LLM_MAX_TOKENS", default="2048"))
LLM_TIMEOUT_S = int(_get("LLM_TIMEOUT_S", default="300"))

# ── Projects: every project's repo lives under PROJECTS_ROOT ─────────
PROJECTS_ROOT = Path(_get("PROJECTS_ROOT", default="/workspace/projects"))

# ── GitHub (issues / pull requests) ──────────────────────────────────
GITHUB_TOKEN = _get("GITHUB_TOKEN")
GITHUB_REPO = _get("GITHUB_REPO")                    # default owner/name for projects without their own
GITHUB_API = _get("GITHUB_API_URL", default="https://api.github.com")
GITHUB_DRY_RUN = _bool("GITHUB_DRY_RUN", False)

# ── Behaviour ────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = float(_get("CONFIDENCE_THRESHOLD", default="0.6"))
MAX_EXCERPT_CHARS = int(_get("MAX_EXCERPT_CHARS", default="12000"))
MAX_SOURCE_CHARS = int(_get("MAX_SOURCE_CHARS", default="14000"))
MAX_INFO_CHARS = int(_get("MAX_INFO_CHARS", default="20000"))
MAX_PATCH_SRC_CHARS = int(_get("MAX_PATCH_SRC_CHARS", default="150000"))
MAX_CASES = int(_get("MAX_CASES", default="200"))
MAX_RUNS = int(_get("MAX_RUNS", default="30"))
MAX_LOG_CHARS = int(_get("MAX_LOG_CHARS", default="20000000"))
# Minimum similarity (0-1) for a knowledge-base entry to be reused without calling the LLM.
KB_MATCH_THRESHOLD = float(_get("KB_MATCH_THRESHOLD", default="0.8"))
# Max provide_info rounds before warning the user (soft cap; overridable with force_continue).
MAX_INFO_ROUNDS = int(_get("MAX_INFO_ROUNDS", default="3"))
# Absolute max provide_info rounds (hard cap); auto-drafts an issue and cannot be overridden.
MAX_INFO_ROUNDS_HARD = int(_get("MAX_INFO_ROUNDS_HARD", default="5"))

DATA_DIR = Path(_get("DATA_DIR", default=str(BASE_DIR / "data")))
SAMPLES_DIR = BASE_DIR / "samples"
UPLOADS_DIR = DATA_DIR / "uploads"
MAX_UPLOADS = int(_get("MAX_UPLOADS", default="20"))
