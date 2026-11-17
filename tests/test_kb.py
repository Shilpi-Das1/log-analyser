import pytest

from core import config, kb
from core.errors import AppError


def test_signature_prefers_pytest_E_lines_and_error_lines():
    assert kb.signature_of("noise\nE   AssertionError: boom\nE   more") == "AssertionError: boom | more"
    assert "connection refused" in kb.signature_of("info\nERROR: connection refused\nend")
    assert kb.signature_of("one\ntwo") == "one | two"


def test_normalize_strips_volatile_parts():
    a = "2026-01-02T03:04:05Z pod abc-0 10.0.0.5:8080 failed after 120 s id 0xdeadbeef"
    b = "2027-05-06T07:08:09Z pod abc-0 10.9.9.9:9090 failed after 5 s id 0x1234abcd"
    assert kb.normalize(a) == kb.normalize(b)


def test_similarity_bounds():
    assert kb.similarity("same text here", "same text here") == 1.0
    assert kb.similarity("", "x y z") == 0.0
    assert kb.similarity("a", "b") == 0.0               # too few tokens
    assert 0 < kb.similarity("osd slow ops alert not firing", "osd slow ops alert did not fire") < 1


def test_record_search_and_status_precedence():
    first = kb.record("p", "TimeoutError waiting for pods ready", "pods never ready", "env_infra", "check registry")
    assert first["status"] == "suggested"
    hits = kb.search("p", "TimeoutError waiting for pods ready 120s")
    assert hits and hits[0]["id"] == first["id"] and hits[0]["score"] >= 0.8
    assert kb.search("other-project", "TimeoutError waiting for pods ready") == []

    verified = kb.record("p", "TimeoutError waiting for pods ready", "pods never ready", "env_infra",
                         "patched", "verified", "a.py", [["old", "new"]])
    assert verified["id"] == first["id"] and verified["status"] == "verified"
    downgraded = kb.record("p", "TimeoutError waiting for pods ready", "x", status="open_issue")
    assert downgraded["status"] == "verified"           # a lower-ranked status never overwrites


def test_reusable_requires_verified_patch_and_threshold():
    base = {"status": "verified", "patch_blocks": [["a", "b"]], "patch_file": "f.py", "score": 0.9}
    assert kb.reusable([base]) is base
    assert kb.reusable([{**base, "status": "suggested"}]) is None
    assert kb.reusable([{**base, "patch_blocks": []}]) is None
    assert kb.reusable([{**base, "score": config.KB_MATCH_THRESHOLD - 0.1}]) is None


def test_touch_list_delete_and_validation():
    e = kb.record("p", "disk full on node", "disk full")
    kb.touch(e["id"], "p")
    assert kb.list_entries("p")[0]["hits"] == 1
    assert kb.list_entries()[0]["id"] == e["id"]
    kb.delete(e["id"], "p")
    assert kb.list_entries() == []
    with pytest.raises(AppError):
        kb.delete(e["id"], "p")
    with pytest.raises(AppError):
        kb.record("p", "", "x")
    with pytest.raises(ValueError):
        kb.record("p", "sig text", "x", status="bogus")
