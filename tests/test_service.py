"""End-to-end service flows with a fake LLM."""
import pytest

from core import kb, service
from core.errors import AppError, PatchError
from tests.conftest import PYTEST_FAIL, PYTEST_PASS, cls, fake_llm

OLD = "    return s * (1 - d) * (1 - d)"
PATCH_REPLY = f"<<<OLD\n{OLD}\n|||\n    return s * (1 - d)\n>>>NEW"
TEST_ID = "tests/test_a.py::test_boom"


def _run(project):
    return service.submit_log("shop", PYTEST_FAIL, "ci.log")


def _analysed(monkeypatch, project, **cls_kw):
    calls = fake_llm(monkeypatch, [cls(**cls_kw)])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    return run, case, calls


# ── submit_log / runs ────────────────────────────────────────────────

def test_submit_log_parses_without_llm(project, monkeypatch):
    calls = fake_llm(monkeypatch, [cls()])
    run = _run(project)
    assert run["counts"]["failed"] == 1 and run["counts"]["passed"] == 1 and run["counts"]["skipped"] == 1
    assert all("log" not in t for t in run["tests"])
    assert calls == []
    assert service.get_run(run["id"])["id"] == run["id"]
    assert [r["id"] for r in service.list_runs("shop")] == [run["id"]]
    assert service.list_runs("other") == []


def test_submit_log_validation(project, monkeypatch):
    with pytest.raises(AppError, match="empty"):
        service.submit_log("shop", "  ")
    with pytest.raises(AppError, match="Unknown project"):
        service.submit_log("nope", "x")
    with pytest.raises(AppError, match="must be text"):
        service.submit_log("shop", {"value": "x"})

    monkeypatch.setattr("core.config.MAX_LOG_CHARS", 3)
    with pytest.raises(AppError, match="too large"):
        service.submit_log("shop", "abcdef")


def test_project_wrappers(isolated):
    service.add_project("shop", "shop")
    assert [p["name"] for p in service.list_projects()] == ["shop"]
    service.remove_project("shop")
    assert service.list_projects() == []


# ── analyse_failure ──────────────────────────────────────────────────

def test_only_failed_tests_are_analysed(project, monkeypatch):
    calls = fake_llm(monkeypatch, [cls()])
    run = _run(project)
    with pytest.raises(AppError, match="skipped"):
        service.analyse_failure(run["id"], "tests/test_a.py::test_skip")
    with pytest.raises(AppError, match="passed"):
        service.analyse_failure(run["id"], "tests/test_a.py::test_ok")
    with pytest.raises(AppError, match="not found"):
        service.analyse_failure(run["id"], "nope")
    assert calls == []


def test_analyse_failure_classifies_and_is_idempotent(project, monkeypatch):
    run, case, calls = _analysed(monkeypatch, project)
    assert case["classification"]["category"] == "product_bug"
    assert case["capabilities"]["mode"] == "repo" and case["capabilities"]["can_patch"] is True
    assert case["sources"] and case["frames"]
    again = service.analyse_failure(run["id"], TEST_ID)
    assert again["id"] == case["id"] and len(calls) == 1
    assert any(t["case_id"] == case["id"] for t in service.get_run(run["id"])["tests"])


def test_generic_mode_without_repo_has_no_patch(isolated, monkeypatch):
    from core import projects
    (isolated.parent / "empty").mkdir()
    projects.add("empty", "empty", "pytest")
    fake_llm(monkeypatch, [cls()])
    run = service.submit_log("empty", PYTEST_FAIL)
    case = service.analyse_failure(run["id"], TEST_ID)
    assert case["capabilities"]["can_patch"] is False      # target file is not in the (empty) repo
    assert case["sources"] == []
    with pytest.raises(AppError, match="Patch generation"):
        service.generate_patch(case["id"])


def test_missing_project_directory_means_generic_mode(project, monkeypatch):
    import shutil
    from core import config
    run = _run(project)
    shutil.rmtree(config.PROJECTS_ROOT / "shop")
    fake_llm(monkeypatch, [cls()])
    case = service.analyse_failure(run["id"], TEST_ID)
    assert case["capabilities"]["mode"] == "generic" and case["capabilities"]["can_patch"] is False


# ── interactive needs-info loop ──────────────────────────────────────

def test_needs_info_then_provide_info_reanalyses(project, monkeypatch):
    unsure = cls(category="unknown", confidence=0.2, missing_info=[{"id": "q1", "question": "pod logs?"}])
    calls = fake_llm(monkeypatch, [unsure, cls(category="product_bug", confidence=0.9)])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    assert case["classification"]["needs_info"] is True
    assert case["capabilities"]["can_patch"] is False and case["capabilities"]["can_issue"] is False

    case = service.provide_info(case["id"], "NullPointerException at shop/pricing.py:3", "pod logs?")
    assert case["classification"]["category"] == "product_bug" and case["classification"]["needs_info"] is False
    assert len(case["info"]) == 1 and len(case["history"]) == 2
    prompt = calls[-1][-1]["content"]
    assert "USER-PROVIDED INFORMATION" in prompt and "NullPointerException" in prompt
    assert "PREVIOUS ASSESSMENT" in prompt


def test_provide_info_requires_text(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project)
    with pytest.raises(AppError, match="Provide"):
        service.provide_info(case["id"], "   ")


def test_timeout_is_not_blindly_accepted(project, monkeypatch):
    """A timeout without a known awaited operation must keep asking instead of advising a bigger timeout."""
    fake_llm(monkeypatch, [cls(category="flaky_timeout", confidence=0.95, waiting_on="",
                               fix_suggestion="increase the timeout")])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    assert case["classification"]["needs_info"] is True and case["classification"]["missing_info"]


# ── patch / rerun ────────────────────────────────────────────────────

def test_generate_patch_and_rerun_passed_verifies_kb(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    case = service.generate_patch(case["id"], "keep it small")
    assert case["patch"]["file"] == "shop/pricing.py" and "-    return" in case["patch"]["diff"]
    assert case["capabilities"]["can_rerun"] is True and "new_content" not in case["patch"]

    case = service.submit_rerun(case["id"], PYTEST_PASS)
    assert case["rerun"]["status"] == "passed"
    entries = kb.list_entries("shop")
    assert len(entries) == 1 and entries[0]["status"] == "verified" and entries[0]["patch_blocks"]


def test_rerun_requires_patch_and_matching_test(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    with pytest.raises(AppError, match="Generate a patch"):
        service.submit_rerun(case["id"], PYTEST_PASS)
    service.generate_patch(case["id"])
    with pytest.raises(AppError, match="does not contain"):
        service.submit_rerun(case["id"], "tests/other.py::t1 PASSED\ntests/other.py::t2 PASSED\n")
    with pytest.raises(AppError, match="skipped"):
        service.submit_rerun(case["id"], f"{TEST_ID} SKIPPED (x)\ntests/test_a.py::test_ok PASSED\n")


def test_failed_rerun_reanalyses_then_exhausts_to_issue(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY, cls(summary="second look"), PATCH_REPLY, cls(summary="third")])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    failing = f"{TEST_ID} FAILED\n=== FAILURES ===\n___ test_boom ___\nE assert 0 == 1\n"

    case = service.submit_rerun(case["id"], failing)
    assert case["rerun"] == {"status": "failed", "attempts": 1, "exhausted": False}
    assert "patch" not in case and case["classification"]["summary"] == "second look"
    assert any("Rerun with the proposed patch failed" in i["question"] for i in case["info"])

    service.generate_patch(case["id"])            # a new patch keeps the attempt counter
    case = service.submit_rerun(case["id"], failing)
    assert case["rerun"] == {"status": "failed", "attempts": 2, "exhausted": True}
    assert case["capabilities"]["can_issue"] is True


def test_attempts_accumulate_without_regenerating(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY, cls()])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    failing = f"{TEST_ID} FAILED\nE boom\n"
    first = service.submit_rerun(case["id"], failing)
    assert first["rerun"]["exhausted"] is False and first["capabilities"]["can_issue"] is True


def test_patch_error_propagates(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), "garbage"])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    with pytest.raises(PatchError):
        service.generate_patch(case["id"])


# ── knowledge base reuse ─────────────────────────────────────────────

def test_verified_kb_entry_answers_without_llm(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    service.submit_rerun(case["id"], PYTEST_PASS)

    calls = fake_llm(monkeypatch, [cls()])
    run2 = _run(project)
    case2 = service.analyse_failure(run2["id"], TEST_ID)
    assert calls == []
    assert case2["source"] == "kb" and case2["kb_entry"] and case2["patch"]["from_kb"] is True
    assert case2["classification"]["confidence"] == 1.0
    assert kb.list_entries("shop")[0]["hits"] == 1


def test_kb_fix_that_no_longer_applies_falls_back_to_llm(project, monkeypatch):
    kb.record("shop", "assert 0 == 1", "problem", "test_bug", "fix", "verified",
              "shop/pricing.py", [["does not exist", "x"]])
    calls = fake_llm(monkeypatch, [cls(summary="fresh")])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    assert len(calls) == 1 and case["source"] == "llm" and case["classification"]["summary"] == "fresh"
    assert case["kb_hints"]                                 # still offered as a hint


def test_save_to_kb_suggested_and_verified(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project)
    entry = service.save_to_kb(case["id"])["entry"]
    assert entry["status"] == "suggested"
    assert service.kb_list("shop")[0]["id"] == entry["id"]
    service.kb_delete(entry["id"])
    assert service.kb_list() == []


def test_save_to_kb_rejects_unconfident_case(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project, category="unknown", confidence=0.1)
    with pytest.raises(AppError, match="confident"):
        service.save_to_kb(case["id"])


# ── drafts and approval ──────────────────────────────────────────────

def test_issue_draft_edit_and_publish_requires_approval(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project)
    case = service.draft_issue(case["id"])
    draft = case["issue_draft"]
    assert draft["title"] == "NPE in BatchWorker" and "## Evidence" in draft["body"]

    case = service.update_draft(case["id"], "issue", " My title ", "My body")
    assert case["issue_draft"]["title"] == "My title" and case["issue_draft"]["body"] == "My body"

    for flag in (False, None, "true", 1):
        with pytest.raises(AppError, match="approval"):
            service.create_issue(case["id"], flag)
    assert service.get_case(case["id"])["published"] == []

    out = service.create_issue(case["id"], True)
    assert out["published"]["dry_run"] is True and out["case"]["published"][0]["type"] == "issue"
    assert kb.list_entries("shop")[0]["status"] == "open_issue"


def test_issue_requires_product_bug_or_failed_rerun(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project, category="test_bug")
    with pytest.raises(AppError, match="product_bug"):
        service.draft_issue(case["id"])
    with pytest.raises(AppError, match="Draft the issue"):
        service.create_issue(case["id"], True)


def test_issue_after_failed_rerun_mentions_attempts(project, monkeypatch):
    fake_llm(monkeypatch, [cls(category="test_bug"), PATCH_REPLY, cls(category="test_bug")])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    case = service.submit_rerun(case["id"], f"{TEST_ID} FAILED\nE boom\n")
    assert case["capabilities"]["can_issue"] is True
    body = service.draft_issue(case["id"])["issue_draft"]["body"]
    assert "Fix attempts" in body


def test_pr_draft_marks_verification_and_publishes_with_approval(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    unverified = service.draft_pr(case["id"])["pr_draft"]
    assert "NOT yet verified" in unverified["body"]

    service.submit_rerun(case["id"], PYTEST_PASS)
    verified = service.draft_pr(case["id"])["pr_draft"]
    assert "passed on a rerun" in verified["body"] and verified["commit_message"].startswith("Fix shop/pricing.py")

    with pytest.raises(AppError, match="approval"):
        service.create_pr(case["id"], False)
    out = service.create_pr(case["id"], True)
    assert out["published"]["branch"].startswith("log-analyser/fix-")
    assert out["case"]["published"][0]["type"] == "pull_request"


def test_pr_requires_patch_and_draft(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project)
    with pytest.raises(AppError, match="patch"):
        service.draft_pr(case["id"])
    with pytest.raises(AppError, match="draft"):
        service.create_pr(case["id"], True)


def test_update_draft_unknown_kind(project, monkeypatch):
    _, case, _ = _analysed(monkeypatch, project)
    with pytest.raises(AppError, match="No such draft"):
        service.update_draft(case["id"], "pr", "t", "b")
    with pytest.raises(AppError, match="No such draft"):
        service.update_draft(case["id"], "bogus", "t", "b")


def test_new_info_discards_stale_patch_and_drafts(project, monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY, cls()])
    run = _run(project)
    case = service.analyse_failure(run["id"], TEST_ID)
    service.generate_patch(case["id"])
    service.draft_pr(case["id"])
    case = service.provide_info(case["id"], "more detail")
    assert "patch" not in case and case["pr_draft"] is None
