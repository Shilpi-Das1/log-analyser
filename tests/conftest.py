import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, locator  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Per-test data dir, a projects root with one multi-language repo, GitHub in dry-run."""
    root = tmp_path / "projects"
    repo = root / "shop"
    (repo / "shop").mkdir(parents=True)
    (repo / "shop" / "pricing.py").write_text(
        "def compute_total(cart, d=0):\n    s = 1\n    return s * (1 - d) * (1 - d)\n", encoding="utf-8")
    java = repo / "src" / "main" / "java" / "com" / "acme" / "shop"
    java.mkdir(parents=True)
    (java / "BatchWorker.java").write_text("\n".join(f"line{i}" for i in range(1, 40)), encoding="utf-8")
    (repo / "deploy.sh").write_text("#!/bin/bash\n" + "\n".join(f"echo {i}" for i in range(30)), encoding="utf-8")

    monkeypatch.setattr(config, "PROJECTS_ROOT", root)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "GITHUB_DRY_RUN", True)
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    locator.clear_index()
    yield repo
    locator.clear_index()


@pytest.fixture
def project(isolated):
    from core import projects
    return projects.add("shop", "shop", "auto", "owner/shop")


def fake_llm(monkeypatch, replies):
    """llm.chat returns the given replies in order (last repeats); returns the recorded prompts."""
    calls, queue = [], list(replies)

    def _chat(messages, max_tokens=None, temperature=0.2):
        calls.append(messages)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return {"content": text, "prompt_tokens": 1, "completion_tokens": 1, "elapsed_s": 0}

    from core import llm
    monkeypatch.setattr(llm, "chat", _chat)
    return calls


def cls(**kw):
    base = {"category": "product_bug", "confidence": 0.9, "summary": "s", "root_cause": "r",
            "evidence": ["NullPointerException"], "waiting_on": "", "suspected_component": "BatchWorker",
            "error_signature": "NullPointerException BatchWorker.handle", "issue_title": "NPE in BatchWorker",
            "fix_suggestion": "guard null", "target_file": "shop/pricing.py", "missing_info": []}
    base.update(kw)
    return base


PYTEST_FAIL = """\
tests/test_a.py::test_ok PASSED
tests/test_a.py::test_boom FAILED
tests/test_a.py::test_skip SKIPPED (not today)
=================================== FAILURES ===================================
_________________________________ test_boom ____________________________________

>   assert compute_total([], 0) == 1
E   assert 0 == 1

shop/pricing.py:3: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_boom - assert 0 == 1
=================== 1 failed, 1 passed, 1 skipped in 0.1s ======================
"""

PYTEST_PASS = "tests/test_a.py::test_ok PASSED\ntests/test_a.py::test_boom PASSED\n"
