import json

import pytest

from core import classifier
from core.errors import LLMError
from tests.conftest import cls, fake_llm


def _case(**kw):
    base = {"format": "pytest", "excerpt": "E boom", "sources": [], "info": [], "domain_hint": ""}
    base.update(kw)
    return base


def test_normalize_invalid_category_and_confidence():
    out = classifier.normalize({"category": "nonsense", "confidence": "x"})
    assert out["category"] == "unknown" and out["confidence"] == 0.0 and out["needs_info"] is True
    assert out["missing_info"]            # a default question is always provided


def test_normalize_confident_diagnosis():
    out = classifier.normalize(cls())
    assert out["needs_info"] is False and out["category"] == "product_bug"


def test_timeout_without_waiting_on_is_not_a_diagnosis():
    out = classifier.normalize(cls(category="flaky_timeout", confidence=0.95, waiting_on=""))
    assert out["confidence"] <= 0.4 and out["needs_info"] is True


def test_timeout_with_waiting_on_is_allowed():
    out = classifier.normalize(cls(category="flaky_timeout", confidence=0.9, waiting_on="pod ready"))
    assert out["needs_info"] is False


def test_missing_info_accepts_strings_and_dicts():
    out = classifier.normalize({"category": "unknown", "missing_info": ["need logs", {"question": "which version?"}, 5]})
    assert [m["question"] for m in out["missing_info"]] == ["need logs", "which version?"]


def test_prompt_contains_all_context_sections():
    case = _case(sources=[{"file": "a.py", "focus_line": 3, "text": "    3 | x"}],
                 kb_hints=[{"problem": "p", "fix_text": "f", "score": 0.5}],
                 classification={"category": "unknown", "confidence": 0.2, "summary": "s", "missing_info": []},
                 info=[{"question": "which node?", "text": "node-1"}])
    prompt = classifier.build_prompt(case)
    for needle in ("LOG EXCERPT", "a.py (focus line 3)", "SIMILAR PAST PROBLEMS", "PREVIOUS ASSESSMENT",
                   "answering: which node?", "node-1"):
        assert needle in prompt
    assert "not available" in classifier.build_prompt(_case())


def test_system_prompt_forbids_timeout_default_and_includes_domain():
    system = classifier._system("Ceph storage")
    assert "SYMPTOM" in system and "Ceph storage" in system
    assert "Domain context" not in classifier._system("")


def test_classify_returns_tokens(monkeypatch):
    fake_llm(monkeypatch, [cls()])
    out = classifier.classify(_case())
    assert out["category"] == "product_bug" and out["tokens"] == 2


def test_classify_retries_on_invalid_json(monkeypatch):
    calls = fake_llm(monkeypatch, ["not json", "```json\n" + json.dumps(cls()) + "\n```"])
    assert classifier.classify(_case())["category"] == "product_bug"
    assert len(calls) == 2 and "not valid JSON" in calls[1][-1]["content"]


def test_classify_gives_up_after_two_bad_replies(monkeypatch):
    fake_llm(monkeypatch, ["nope"])
    with pytest.raises(LLMError):
        classifier.classify(_case())
