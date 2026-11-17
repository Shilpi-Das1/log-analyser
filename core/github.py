"""Minimal GitHub REST client: search issues, create issue, create PR (branch + commit).

Every call takes the target `repo` (owner/name) so each project can publish to its own repository.
"""
import base64
import re
import time

import httpx

from core import config
from core.errors import GitHubError

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def configured(repo: str = "") -> bool:
    return bool(repo and (config.GITHUB_TOKEN or config.GITHUB_DRY_RUN))


def _client() -> httpx.Client:
    if not config.GITHUB_TOKEN:
        raise GitHubError("GITHUB_TOKEN is not set.")
    return httpx.Client(
        base_url=config.GITHUB_API, timeout=30,
        headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})


def _check(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub API {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _require_repo(repo: str) -> str:
    if not _REPO_RE.match(repo or ""):
        raise GitHubError("No GitHub repository configured for this project (expected owner/name).")
    return repo


def search_issues(repo: str, signature: str, limit: int = 5) -> list[dict]:
    """Existing issues matching the error signature (duplicate check). [] when unavailable."""
    if not signature or not config.GITHUB_TOKEN or not _REPO_RE.match(repo or ""):
        return []
    query = f'repo:{repo} is:issue "{signature[:120]}"'
    try:
        with _client() as client:
            data = _check(client.get("/search/issues", params={"q": query, "per_page": limit}))
    except (GitHubError, httpx.HTTPError):
        return []
    return [{"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
            for i in data.get("items", [])]


def create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> dict:
    repo = _require_repo(repo)
    if config.GITHUB_DRY_RUN:
        return {"dry_run": True, "url": f"https://github.com/{repo}/issues/0", "number": 0}
    with _client() as client:
        data = _check(client.post(f"/repos/{repo}/issues",
                                  json={"title": title, "body": body, "labels": labels or []}))
    return {"dry_run": False, "url": data["html_url"], "number": data["number"]}


def create_pull_request(repo: str, title: str, body: str, file_path: str, new_content: str,
                        commit_message: str) -> dict:
    repo = _require_repo(repo)
    branch = f"log-analyser/fix-{int(time.time())}"
    if config.GITHUB_DRY_RUN:
        return {"dry_run": True, "url": f"https://github.com/{repo}/pull/0", "number": 0, "branch": branch}
    # Normalise path: use forward slashes, then strip any leading slash/backslash
    # (GitHub Contents API requires a clean relative path, e.g. "src/Foo.java")
    file_path = file_path.replace("\\", "/").strip("/")
    with _client() as client:
        base = _check(client.get(f"/repos/{repo}"))["default_branch"]
        ref = _check(client.get(f"/repos/{repo}/git/ref/heads/{base}"))
        _check(client.post(f"/repos/{repo}/git/refs",
                           json={"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]}))
        # Fetch existing file sha — if the file doesn't exist yet (404) create it without sha
        existing = client.get(f"/repos/{repo}/contents/{file_path}", params={"ref": branch})
        put_body: dict = {
            "message": commit_message, "branch": branch,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii")}
        if existing.status_code == 200:
            put_body["sha"] = existing.json()["sha"]
        elif existing.status_code != 404:
            # Any status other than 200 (found) or 404 (new file) is a real error
            raise GitHubError(f"GitHub API {existing.status_code}: {existing.text[:300]}")
        _check(client.put(f"/repos/{repo}/contents/{file_path}", json=put_body))
        pr = _check(client.post(f"/repos/{repo}/pulls",
                                json={"title": title, "body": body, "head": branch, "base": base}))
    return {"dry_run": False, "url": pr["html_url"], "number": pr["number"], "branch": branch}
