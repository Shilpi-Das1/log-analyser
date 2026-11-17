"""Project registry: projects (ocs-ci, a Java service, ...) can be added or removed at runtime.

A project's repository must live under PROJECTS_ROOT, so the service can never read arbitrary
files from the host or container.
"""
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from core import config
from core.errors import AppError

_GIT_URL_RE = re.compile(
    r"^(https?://|git@|ssh://)"  # scheme or SCP-style ssh
    r"[A-Za-z0-9._:@/%-]+"       # host + path
    r"(\.git)?$",
    re.IGNORECASE,
)

LOG_FORMATS = ("auto", "pytest", "junit", "go", "maven", "generic")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_GH_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _file() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "projects.json"


def _read() -> dict:
    path = _file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(data: dict) -> None:
    path = _file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def resolve_root(rel_path: str) -> Path:
    """Resolve a project path relative to PROJECTS_ROOT; reject anything that escapes it."""
    base = config.PROJECTS_ROOT.resolve()
    target = (base / (rel_path or "")).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise AppError("Project path must be inside the projects root.") from exc
    return target


def root_of(project: dict) -> Path:
    return resolve_root(project["path"])


def _view(project: dict) -> dict:
    return {**project, "available": root_of(project).is_dir()}


def list_projects() -> list[dict]:
    return [_view(p) for p in sorted(_read().values(), key=lambda p: p["name"].lower())]


def get(name: str) -> dict:
    project = _read().get(name or "")
    if not project:
        raise AppError(f"Unknown project '{name}'. Add it first.")
    return project


def add(name: str, path: str = "", log_format: str = "auto", github_repo: str = "",
        domain_hint: str = "") -> dict:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise AppError("Project name must be 1-40 characters: letters, digits, '.', '_' or '-'.")
    if log_format not in LOG_FORMATS:
        raise AppError(f"log_format must be one of {', '.join(LOG_FORMATS)}.")
    github_repo = (github_repo or "").strip()
    if github_repo and not _GH_RE.match(github_repo):
        raise AppError("github_repo must look like owner/name.")
    rel = (path or name).strip()
    if not resolve_root(rel).is_dir():
        raise AppError(f"Directory '{rel}' not found under the projects root.")
    data = _read()
    data[name] = {"name": name, "path": rel, "log_format": log_format,
                  "github_repo": github_repo, "domain_hint": (domain_hint or "").strip()[:500]}
    _write(data)
    return _view(data[name])


def remove(name: str) -> None:
    data = _read()
    if name not in data:
        raise AppError(f"Unknown project '{name}'.")
    del data[name]
    _write(data)


def github_repo_of(project: dict) -> str:
    return project.get("github_repo") or config.GITHUB_REPO


def _inject_token(url: str, token: str) -> str:
    """Embed a token into an https:// URL as https://token@host/path."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AppError("A token can only be injected into an http/https URL.")
    netloc = f"{urllib.parse.quote(token, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def clone_project(git_url: str, name: str = "", token: str = "",
                  log_format: str = "auto", github_repo: str = "",
                  domain_hint: str = "") -> dict:
    """Clone *git_url* into PROJECTS_ROOT/<name> then register and return the project.

    Raises AppError if git is not available, the URL looks unsafe, the target
    already exists, or the clone fails.
    """
    git_url = (git_url or "").strip()
    if not git_url:
        raise AppError("git_url is required.")
    if not _GIT_URL_RE.match(git_url):
        raise AppError("git_url does not look like a valid Git URL (must start with https://, http://, git@, or ssh://).")

    # Derive a safe project name from the URL if not provided
    if not name:
        stem = git_url.rstrip("/").split("/")[-1]
        name = re.sub(r"\.git$", "", stem, flags=re.IGNORECASE)
        name = re.sub(r"[^A-Za-z0-9._-]", "-", name)[:40]

    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise AppError("Project name must be 1-40 characters: letters, digits, '.', '_' or '-'.")

    if not shutil.which("git"):
        raise AppError("'git' executable not found. Install git on the server to use this feature.")

    target = resolve_root(name)
    if target.exists():
        raise AppError(f"Directory '{name}' already exists under the projects root.")

    clone_url = _inject_token(git_url, token) if token and git_url.startswith(("http://", "https://")) else git_url

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(target)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Clean up partial clone
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise AppError("git clone timed out after 120 seconds.")
    except FileNotFoundError:
        raise AppError("'git' executable not found.")

    if result.returncode != 0:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        # Strip any token from error output before surfacing it
        stderr = result.stderr.replace(token, "***") if token else result.stderr
        raise AppError(f"git clone failed: {stderr.strip()[:400]}")

    return add(name, path=name, log_format=log_format, github_repo=github_repo, domain_hint=domain_hint)
