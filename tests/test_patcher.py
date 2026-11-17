import pytest

from core import patcher
from core.errors import PatchError
from tests.conftest import fake_llm

OLD = "    return s * (1 - d) * (1 - d)"
NEW = "    return s * (1 - d)"


def _reply(old=OLD, new=NEW):
    return f"<<<OLD\n{old}\n|||\n{new}\n>>>NEW"


def test_generate_builds_diff_and_blocks(isolated, monkeypatch):
    fake_llm(monkeypatch, [_reply()])
    out = patcher.generate(isolated, "shop/pricing.py", {"root_cause": "r", "fix_suggestion": "f"}, "notes")
    assert "-    return s * (1 - d) * (1 - d)" in out["diff"] and "+    return s * (1 - d)\n" in out["diff"]
    assert out["blocks"] == [[OLD, NEW]] and out["tokens"] == 2
    assert "(1 - d) * (1 - d)" not in out["new_content"]


def test_generate_rejects_non_matching_and_empty(isolated, monkeypatch):
    fake_llm(monkeypatch, [_reply(old="    return nothing here")])
    with pytest.raises(PatchError, match="exactly"):
        patcher.generate(isolated, "shop/pricing.py", {})
    fake_llm(monkeypatch, ["no blocks at all"])
    with pytest.raises(PatchError, match="usable"):
        patcher.generate(isolated, "shop/pricing.py", {})


def test_generate_rejects_ambiguous_match(isolated, monkeypatch):
    (isolated / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    fake_llm(monkeypatch, [_reply(old="x = 1", new="x = 2")])
    with pytest.raises(PatchError, match="multiple"):
        patcher.generate(isolated, "dup.py", {})


def test_works_for_java_and_shell(isolated, monkeypatch):
    fake_llm(monkeypatch, [_reply(old="line5", new="line5 // fixed")])
    out = patcher.generate(isolated, "src/main/java/com/acme/shop/BatchWorker.java", {})
    assert "+line5 // fixed" in out["diff"]
    fake_llm(monkeypatch, [_reply(old="echo 3", new="echo three")])
    assert "+echo three" in patcher.generate(isolated, "deploy.sh", {})["diff"]


def test_traversal_and_missing_target(isolated):
    with pytest.raises(PatchError, match="outside"):
        patcher.resolve_target(isolated, "../../etc/passwd")
    with pytest.raises(PatchError, match="not found"):
        patcher.resolve_target(isolated, "nope.py")


def test_too_large_file(isolated, monkeypatch):
    monkeypatch.setattr("core.config.MAX_PATCH_SRC_CHARS", 5)
    with pytest.raises(PatchError, match="too large"):
        patcher.generate(isolated, "shop/pricing.py", {})
    with pytest.raises(PatchError, match="too large"):
        patcher.replay(isolated, "shop/pricing.py", [[OLD, NEW]])


def test_replay_applies_blocks_without_llm(isolated):
    out = patcher.replay(isolated, "shop/pricing.py", [[OLD, NEW]])
    assert out["from_kb"] is True and out["tokens"] == 0 and "+    return s * (1 - d)" in out["diff"]


def test_replay_fails_when_code_changed(isolated):
    with pytest.raises(PatchError):
        patcher.replay(isolated, "shop/pricing.py", [["gone", "x"]])


def test_empty_old_and_noop_changes(isolated):
    with pytest.raises(PatchError, match="empty OLD"):
        patcher.replay(isolated, "shop/pricing.py", [["  ", "x"]])
    with pytest.raises(PatchError, match="no difference"):
        patcher.replay(isolated, "shop/pricing.py", [[OLD, OLD]])


def test_crlf_files_are_normalised(isolated):
    (isolated / "win.txt").write_bytes(b"a\r\nb\r\n")
    out = patcher.replay(isolated, "win.txt", [["b", "c"]])
    assert out["new_content"] == "a\nc\n"
