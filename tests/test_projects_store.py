import pytest

from core import config, projects, store
from core.errors import AppError


def test_add_list_get_remove(isolated):
    p = projects.add("shop", "shop", "pytest", "o/r", "hint")
    assert p["available"] is True and p["log_format"] == "pytest"
    assert [x["name"] for x in projects.list_projects()] == ["shop"]
    assert projects.get("shop")["github_repo"] == "o/r"
    projects.remove("shop")
    with pytest.raises(AppError):
        projects.get("shop")
    with pytest.raises(AppError):
        projects.remove("shop")


def test_path_defaults_to_name_and_must_exist(isolated):
    assert projects.add("shop")["path"] == "shop"
    with pytest.raises(AppError):
        projects.add("ghost")


@pytest.mark.parametrize("name", ["", "bad name", "-x", "a" * 41, "x/y"])
def test_invalid_names(isolated, name):
    with pytest.raises(AppError):
        projects.add(name, "shop")


def test_validation_of_format_and_repo(isolated):
    with pytest.raises(AppError):
        projects.add("shop", "shop", "nope")
    with pytest.raises(AppError):
        projects.add("shop", "shop", "auto", "not a repo")


def test_path_cannot_escape_projects_root(isolated):
    with pytest.raises(AppError):
        projects.add("evil", "../../etc")
    with pytest.raises(AppError):
        projects.resolve_root("..")


def test_github_repo_falls_back_to_global(isolated, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_REPO", "global/repo")
    assert projects.github_repo_of({"github_repo": ""}) == "global/repo"
    assert projects.github_repo_of({"github_repo": "x/y"}) == "x/y"


def test_store_roundtrip_prune_and_ids(isolated, monkeypatch):
    import time
    monkeypatch.setattr(config, "MAX_RUNS", 2)
    ids = []
    for _ in range(3):
        obj = {"id": store.new_id()}
        store.save("runs", obj)
        ids.append(obj["id"])
        time.sleep(0.01)  # ensure distinct mtimes on Windows (1-second resolution)
    assert len(store.list_all("runs")) == 2
    with pytest.raises(AppError):
        store.load("runs", ids[0])
    assert store.load("runs", ids[2])["id"] == ids[2]
    with pytest.raises(AppError):
        store.load("runs", "../../x")
    with pytest.raises(AppError):
        store.load("cases", "0123456789")


# ── clone_project ──────────────────────────────────────────────────────────────

def test_clone_project_success(isolated, monkeypatch):
    """A successful git clone creates the dir, registers it, and returns the project."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")

    from core import config as _cfg

    def fake_run(cmd, **kw):
        # Simulate git clone: create the target directory so add() finds it
        import pathlib
        pathlib.Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)

    p = projects.clone_project("https://github.com/owner/myrepo.git", name="myrepo")
    assert p["name"] == "myrepo"
    assert p["available"] is True


def test_clone_project_name_derived_from_url(isolated, monkeypatch):
    """Name is auto-derived from the URL slug when not provided."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")

    def fake_run(cmd, **kw):
        import pathlib
        pathlib.Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)

    p = projects.clone_project("https://github.com/red-hat/ocs-ci.git")
    assert p["name"] == "ocs-ci"


def test_clone_project_bad_url(isolated):
    with pytest.raises(AppError, match="valid Git URL"):
        projects.clone_project("not-a-url")


def test_clone_project_no_git(isolated, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: None)
    with pytest.raises(AppError, match="git"):
        projects.clone_project("https://github.com/owner/repo.git")


def test_clone_project_already_exists(isolated, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")
    # 'shop' already exists in the isolated fixture
    with pytest.raises(AppError, match="already exists"):
        projects.clone_project("https://github.com/owner/shop.git", name="shop")


def test_clone_project_git_failure(isolated, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")

    def fake_run(cmd, **kw):
        class R:
            returncode = 128
            stderr = "fatal: repository not found"
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)
    with pytest.raises(AppError, match="git clone failed"):
        projects.clone_project("https://github.com/owner/nonexistent.git", name="nonexistent")


def test_clone_project_token_stripped_from_error(isolated, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")
    secret = "supersecrettoken"

    def fake_run(cmd, **kw):
        class R:
            returncode = 128
            stderr = f"fatal: auth failed with token {secret}"
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)
    exc = pytest.raises(AppError, lambda: projects.clone_project(
        "https://github.com/owner/repo.git", name="newrepo", token=secret))
    assert secret not in str(exc.value)


def test_inject_token_https(isolated):
    url = projects._inject_token("https://github.com/owner/repo.git", "mytoken")
    assert "mytoken@" in url
    assert url.startswith("https://")


def test_inject_token_rejects_non_https(isolated):
    with pytest.raises(AppError):
        projects._inject_token("git@github.com:owner/repo.git", "token")


# ── refresh (git pull) ─────────────────────────────────────────────────────────

def _git_clone_of_shop(isolated):
    projects.add("shop", "shop")
    (isolated / ".git").mkdir()


def test_refreshable_flag_only_for_git_clones(isolated):
    assert projects.add("shop", "shop")["refreshable"] is False
    (isolated / ".git").mkdir()
    assert projects.list_projects()[0]["refreshable"] is True


def test_refresh_runs_ff_only_pull(isolated, monkeypatch):
    _git_clone_of_shop(isolated)
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        class R:
            returncode = 0
            stdout = "Already up to date.\n"
            stderr = ""
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)
    out = projects.refresh("shop")
    assert out["output"] == "Already up to date." and out["name"] == "shop"
    assert seen["cmd"][-2:] == ["pull", "--ff-only"]


def test_refresh_rejects_unknown_and_non_git(isolated):
    with pytest.raises(AppError, match="Unknown project"):
        projects.refresh("ghost")
    projects.add("shop", "shop")
    with pytest.raises(AppError, match="not a git clone"):
        projects.refresh("shop")


def test_refresh_no_git(isolated, monkeypatch):
    _git_clone_of_shop(isolated)
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: None)
    with pytest.raises(AppError, match="git"):
        projects.refresh("shop")


def test_refresh_failure_hides_token_in_url(isolated, monkeypatch):
    _git_clone_of_shop(isolated)
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "fatal: unable to access 'https://sekrettoken@github.com/o/r.git/': 403"
        return R()

    monkeypatch.setattr(__import__("subprocess"), "run", fake_run)
    exc = pytest.raises(AppError, lambda: projects.refresh("shop"))
    assert "sekrettoken" not in str(exc.value) and "git pull failed" in str(exc.value)


def test_refresh_timeout_and_missing_binary(isolated, monkeypatch):
    import subprocess
    _git_clone_of_shop(isolated)
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/git")

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AppError, match="timed out"):
        projects.refresh("shop")

    def missing(cmd, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(AppError, match="not found"):
        projects.refresh("shop")

