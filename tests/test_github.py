import httpx
import pytest

from core import config, github
from core.errors import GitHubError


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.Client(base_url="https://api.test", transport=transport)

    monkeypatch.setattr(github, "_client", factory)


def test_configured(monkeypatch):
    assert github.configured("o/r") is True            # dry-run is on in the fixture
    assert github.configured("") is False
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    assert github.configured("o/r") is False
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    assert github.configured("o/r") is True


def test_client_requires_token():
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        github._client()


def test_client_sets_auth_header(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    with github._client() as client:
        assert client.headers["Authorization"] == "Bearer tok"


def test_dry_run_issue_and_pr_do_not_touch_network(monkeypatch):
    monkeypatch.setattr(github, "_client", lambda: pytest.fail("network used"))
    issue = github.create_issue("o/r", "t", "b", ["bug"])
    pr = github.create_pull_request("o/r", "t", "b", "a.py", "x", "m")
    assert issue["dry_run"] and pr["dry_run"] and pr["branch"].startswith("log-analyser/fix-")


@pytest.mark.parametrize("repo", ["", "nope", "a/b/c"])
def test_repo_is_required_and_validated(repo):
    with pytest.raises(GitHubError, match="repository"):
        github.create_issue(repo, "t", "b")
    with pytest.raises(GitHubError, match="repository"):
        github.create_pull_request(repo, "t", "b", "a.py", "x", "m")


def test_search_issues_unavailable_returns_empty(monkeypatch):
    assert github.search_issues("o/r", "sig") == []           # no token
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    assert github.search_issues("o/r", "") == []
    assert github.search_issues("bad", "sig") == []


def test_search_issues_maps_results_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"items": [
        {"number": 4, "title": "dup", "state": "open", "html_url": "https://x/4"}]}))
    assert github.search_issues("o/r", "sig") == [
        {"number": 4, "title": "dup", "state": "open", "url": "https://x/4"}]
    _mock_client(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    assert github.search_issues("o/r", "sig") == []


def test_create_issue_live(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, req.read()
        return httpx.Response(201, json={"html_url": "https://x/9", "number": 9})

    _mock_client(monkeypatch, handler)
    out = github.create_issue("o/r", "title", "body", ["bug"])
    assert out == {"dry_run": False, "url": "https://x/9", "number": 9}
    assert seen["path"] == "/repos/o/r/issues"


def test_create_pull_request_live_sequence(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        path = req.url.path
        if req.method == "GET" and path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "GET" and "/contents/" in path:
            return httpx.Response(200, json={"sha": "filesha"})
        if req.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://x/pr/3", "number": 3})
        return httpx.Response(201, json={})

    _mock_client(monkeypatch, handler)
    out = github.create_pull_request("o/r", "t", "b", "shop/pricing.py", "new", "msg")
    assert out["number"] == 3 and out["dry_run"] is False
    assert ("PUT", "/repos/o/r/contents/shop/pricing.py") in calls and calls[-1][1].endswith("/pulls")


def test_create_pull_request_new_file(monkeypatch):
    """When the file doesn't exist in the repo (404) the PUT is sent without a sha."""
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    put_bodies = []

    def handler(req):
        path = req.url.path
        if req.method == "GET" and path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "GET" and "/contents/" in path:
            return httpx.Response(404, json={"message": "Not Found"})  # file absent
        if req.method == "PUT" and "/contents/" in path:
            import json
            put_bodies.append(json.loads(req.read()))
            return httpx.Response(201, json={})
        if req.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://x/pr/5", "number": 5})
        return httpx.Response(201, json={})

    _mock_client(monkeypatch, handler)
    out = github.create_pull_request("o/r", "t", "b", "new/file.java", "content", "msg")
    assert out["number"] == 5
    assert len(put_bodies) == 1
    assert "sha" not in put_bodies[0], "sha must be omitted when file is new"


def test_create_pull_request_path_normalisation(monkeypatch):
    """Leading slash and backslashes in file_path are normalised before the API call."""
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    seen_paths = []

    def handler(req):
        if "/contents/" in req.url.path:
            seen_paths.append(req.url.path)
        if req.method == "GET" and req.url.path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.method == "GET" and req.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "GET" and "/contents/" in req.url.path:
            return httpx.Response(200, json={"sha": "s"})
        if req.method == "POST" and req.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://x/pr/6", "number": 6})
        return httpx.Response(201, json={})

    _mock_client(monkeypatch, handler)
    github.create_pull_request("o/r", "t", "b", "\\src\\Foo.java", "x", "m")
    # All paths in the API calls must use forward slashes and no leading slash
    for p in seen_paths:
        assert "/contents/src/Foo.java" in p, f"unexpected path: {p}"


def test_api_error_is_reported(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", False)
    _mock_client(monkeypatch, lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(GitHubError, match="403"):
        github.create_issue("o/r", "t", "b")
