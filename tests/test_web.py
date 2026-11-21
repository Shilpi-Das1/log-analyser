import io

import pytest

import app as app_module
from tests.conftest import PYTEST_FAIL, PYTEST_PASS, cls, fake_llm

OLD = "    return s * (1 - d) * (1 - d)"
PATCH_REPLY = f"<<<OLD\n{OLD}\n|||\n    return s * (1 - d)\n>>>NEW"
TEST_ID = "tests/test_a.py::test_boom"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_index_health_status_samples(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").get_json() == {"status": "ok"}
    assert client.get("/favicon.ico").mimetype == "image/svg+xml"
    status = client.get("/api/status").get_json()
    assert status["github_dry_run"] is True and "pytest" in status["log_formats"]
    names = {s["name"] for s in client.get("/api/samples").get_json()}
    assert "pytest_demo_shop.log" in names


def test_projects_crud_and_available(client):
    assert "shop" in client.get("/api/projects/available").get_json()
    r = client.post("/api/projects", json={"name": "shop", "path": "shop", "log_format": "pytest"})
    assert r.status_code == 201 and r.get_json()["available"] is True
    assert [p["name"] for p in client.get("/api/projects").get_json()] == ["shop"]
    assert client.post("/api/projects", json={"name": "bad name"}).status_code == 400
    assert client.delete("/api/projects/shop").get_json() == {"removed": "shop"}
    assert client.delete("/api/projects/shop").status_code == 400


def test_refresh_project_endpoint(client):
    client.post("/api/projects", json={"name": "shop", "path": "shop"})
    r = client.post("/api/projects/shop/refresh")
    assert r.status_code == 400 and "not a git clone" in r.get_json()["error"]
    assert client.post("/api/projects/ghost/refresh").status_code == 400


def test_available_projects_when_root_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr("core.config.PROJECTS_ROOT", tmp_path / "none")
    assert client.get("/api/projects/available").get_json() == []


def test_full_flow_over_http(client, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    client.post("/api/projects", json={"name": "shop", "path": "shop", "github_repo": "o/shop"})

    run = client.post("/api/runs", json={"project": "shop", "log": PYTEST_FAIL, "name": "ci"}).get_json()
    assert run["counts"]["failed"] == 1
    assert client.get(f"/api/runs/{run['id']}").get_json()["id"] == run["id"]
    assert len(client.get("/api/runs?project=shop").get_json()) == 1

    case = client.post(f"/api/runs/{run['id']}/analyse", json={"test_id": TEST_ID}).get_json()
    cid = case["id"]
    assert client.get(f"/api/case/{cid}").get_json()["id"] == cid

    patch = client.post(f"/api/case/{cid}/patch", json={"notes": "x"}).get_json()
    assert patch["patch"]["diff"]

    r = client.post(f"/api/case/{cid}/rerun", data={"file": (io.BytesIO(PYTEST_PASS.encode()), "rerun.log")},
                    content_type="multipart/form-data")
    assert r.get_json()["rerun"]["status"] == "passed"

    assert client.post(f"/api/case/{cid}/draft/pr").get_json()["pr_draft"]
    edited = client.put(f"/api/case/{cid}/draft/pr", json={"title": "T", "body": "B"}).get_json()
    assert edited["pr_draft"]["title"] == "T"

    refused = client.post(f"/api/case/{cid}/publish/pr", json={"approved": "yes"})
    assert refused.status_code == 400 and "approval" in refused.get_json()["error"]
    ok = client.post(f"/api/case/{cid}/publish/pr", json={"approved": True}).get_json()
    assert ok["published"]["dry_run"] is True

    assert client.get("/api/kb").get_json()[0]["status"] == "verified"
    entry_id = client.get("/api/kb?project=shop").get_json()[0]["id"]
    assert client.delete(f"/api/kb/{entry_id}?project=shop").get_json() == {"deleted": entry_id}
    assert client.delete(f"/api/kb/{entry_id}?project=shop").status_code == 400


def test_info_kb_and_issue_endpoints(client, monkeypatch):
    fake_llm(monkeypatch, [cls(), cls(summary="again")])
    client.post("/api/projects", json={"name": "shop", "path": "shop", "github_repo": "o/shop"})
    run = client.post("/api/runs", json={"project": "shop", "log": PYTEST_FAIL}).get_json()
    cid = client.post(f"/api/runs/{run['id']}/analyse", json={"test_id": TEST_ID}).get_json()["id"]
    info = client.post(f"/api/case/{cid}/info", json={"answer": "more", "question": "q"}).get_json()
    assert info["classification"]["summary"] == "again"
    assert client.post(f"/api/case/{cid}/kb").get_json()["entry"]["status"] == "suggested"
    assert client.post(f"/api/case/{cid}/draft/issue").get_json()["issue_draft"]["labels"]
    assert client.post(f"/api/case/{cid}/publish/issue", json={"approved": True}).get_json()["published"]["dry_run"]


def test_force_draft_issue_over_http(client, monkeypatch):
    fake_llm(monkeypatch, [cls(category="env_infra")])
    client.post("/api/projects", json={"name": "shop", "path": "shop", "github_repo": "o/shop"})
    run = client.post("/api/runs", json={"project": "shop", "log": PYTEST_FAIL}).get_json()
    cid = client.post(f"/api/runs/{run['id']}/analyse", json={"test_id": TEST_ID}).get_json()["id"]
    assert client.post(f"/api/case/{cid}/draft/issue").status_code == 400
    assert client.post(f"/api/case/{cid}/draft/issue", json={"force": "yes"}).status_code == 400
    assert client.post(f"/api/case/{cid}/draft/issue", json={"force": True}).get_json()["issue_draft"]["title"]

def test_upload_and_error_paths(client):
    client.post("/api/projects", json={"name": "shop", "path": "shop"})
    up = client.post("/api/runs", data={"project": "shop", "file": (io.BytesIO(PYTEST_FAIL.encode()), "a.log")},
                     content_type="multipart/form-data")
    assert up.get_json()["name"] == "a.log"
    assert client.post("/api/runs", json={"project": "shop", "log": ""}).status_code == 400
    assert client.post("/api/runs", json={"project": "ghost", "log": "x"}).status_code == 400
    assert client.get("/api/case/zzzz").status_code == 400
    assert client.post("/api/case/0123456789/draft/bogus").status_code == 400
    assert client.post("/api/case/0123456789/publish/bogus", json={"approved": True}).status_code == 400
    assert client.post("/api/runs/0123456789/analyse", json={}).status_code == 400


def test_ci_post_is_marked_stamped_and_not_saved_to_uploads(client):
    client.post("/api/projects", json={"name": "shop", "path": "shop"})
    ci = client.post("/api/runs", content_type="multipart/form-data", data={
        "project": "shop", "source": "github_actions", "run_number": "7", "branch": "main",
        "run_url": "https://github.com/o/shop/actions/runs/1",
        "file": (io.BytesIO(PYTEST_FAIL.encode()), "pytest-output.log")}).get_json()
    assert ci["source"] == "github_actions" and ci["meta"]["run_number"] == "7"
    assert ci["name"].startswith("pytest-output_20") and ci["name"].endswith(".log")
    # a CI post is not a person's upload, so it must not appear in the uploads list
    assert client.get("/api/uploads").get_json() == []

    mine = client.post("/api/runs", content_type="multipart/form-data", data={
        "project": "shop", "file": (io.BytesIO(PYTEST_FAIL.encode()), "mine.log")}).get_json()
    assert mine["source"] == "upload" and mine["name"] == "mine.log"
    assert [u["name"] for u in client.get("/api/uploads").get_json()] == ["mine.log"]

    runs = {r["id"]: r for r in client.get("/api/runs").get_json()}
    assert runs[ci["id"]]["source"] == "github_actions" and runs[mine["id"]]["source"] == "upload"
    assert client.post("/api/runs", json={"project": "shop", "log": PYTEST_FAIL, "source": "x"}).status_code == 400


def test_upload_limit_returns_json(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 10)
    r = client.post("/api/runs", data={"project": "shop", "file": (io.BytesIO(b"x" * 100), "a.log")},
                    content_type="multipart/form-data")
    assert r.status_code == 413 and "too large" in r.get_json()["error"]
