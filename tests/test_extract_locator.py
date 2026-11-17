from core import extract, locator


def test_detect_formats():
    assert extract.detect_format("=== FAILURES ===\nx") == "pytest"
    assert extract.detect_format("at com.acme.A.run(A.java:3)") == "java"
    assert extract.detect_format("--- FAIL: TestX (0.1s)") == "go"
    assert extract.detect_format("./deploy.sh: line 14: failed") == "shell"
    assert extract.detect_format("Traceback (most recent call last):") == "python"
    assert extract.detect_format("<testsuite></testsuite>") == "junit"
    assert extract.detect_format("hello world") == "generic"


def test_clip_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    out = extract.clip(text, 50)
    assert out.startswith("A") and out.endswith("B") and "[truncated]" in out
    assert extract.clip("short", 50) == "short"


def test_build_excerpt_generic_keeps_error_context():
    lines = [f"line {i}" for i in range(200)]
    lines[100] = "FATAL error here"
    out = extract.build_excerpt("\n".join(lines), "generic")
    assert "FATAL error here" in out and "line 199" in out and "..." in out
    assert extract.build_excerpt("", "generic") == ""


def test_build_excerpt_pytest_uses_failures_section():
    text = "noise\n=== FAILURES ===\n___ t ___\nE boom\n=== short test summary info ===\nFAILED x\n"
    out = extract.build_excerpt(text, "pytest")
    assert "E boom" in out and "noise" not in out


def test_resolve_prefers_longest_suffix_and_blocks_site_packages(isolated):
    assert locator.resolve("/ci/ws/shop/pricing.py", isolated).name == "pricing.py"
    assert locator.resolve("/usr/lib/python3/site-packages/shop/pricing.py", isolated) is None
    assert locator.resolve("", isolated) is None
    assert locator.resolve("x.py", None) is None
    assert locator.resolve("missing.py", isolated) is None


def test_find_frames_python_java_shell_go(isolated):
    text = (
        'File "/ci/shop/pricing.py", line 3, in compute_total\n'
        "at com.acme.shop.BatchWorker.handle(BatchWorker.java:7)\n"
        "./deploy.sh: line 5: oops\n"
        "BatchWorker.go:9\n"
    )
    frames = locator.find_frames(text, isolated)
    files = {f["file"] for f in frames}
    assert "shop/pricing.py" in files
    assert "src/main/java/com/acme/shop/BatchWorker.java" in files
    assert "deploy.sh" in files
    assert {f["language"] for f in frames} >= {"py", "java", "sh"}


def test_find_frames_from_pytest_node_id(isolated):
    (isolated / "tests").mkdir()
    (isolated / "tests" / "test_a.py").write_text("x = 1\n\ndef test_boom():\n    pass\n", encoding="utf-8")
    locator.clear_index()
    frames = locator.find_frames("tests/test_a.py::test_boom FAILED", isolated)
    assert frames and frames[0]["file"] == "tests/test_a.py" and frames[0]["line"] == 3


def test_find_frames_without_root_is_empty(tmp_path):
    assert locator.find_frames('File "a.py", line 1', None) == []
    assert locator.find_frames('File "a.py", line 1', tmp_path / "missing") == []


def test_snippet_numbers_lines_and_rejects_escape(isolated):
    snip = locator.snippet(isolated, "shop/pricing.py", 2, context=1)
    assert snip["start"] == 1 and snip["end"] == 3 and "    2 |" in snip["text"]
    assert locator.snippet(isolated, "../../outside.py", 1) is None
    assert locator.snippet(isolated, "nope.py", 1) is None


def test_collect_sources_caps_frames_and_size(isolated, monkeypatch):
    text = 'File "shop/pricing.py", line 3\n'
    frames, snippets = locator.collect_sources(text, isolated)
    assert len(frames) == 1 and len(snippets) == 1
    monkeypatch.setattr("core.config.MAX_SOURCE_CHARS", 1)
    _, none = locator.collect_sources(text, isolated)
    assert none == []
    assert locator.collect_sources(text, None) == ([], [])


def test_collect_sources_first_and_last_three(isolated):
    lines = "\n".join(f'File "deploy.sh", line {i}' for i in range(1, 11))
    frames, _ = locator.collect_sources(lines, isolated)
    assert [f["line"] for f in frames] == [1, 2, 3, 8, 9, 10]
