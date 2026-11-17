import httpx
import pytest

from core import config, llm
from core.errors import LLMError


@pytest.fixture(autouse=True)
def _http_backend(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "http")


def _patch_post(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def post(url, headers=None, json=None, timeout=None):
        with httpx.Client(transport=transport) as client:
            return client.post(url, headers=headers, json=json)

    monkeypatch.setattr(llm.httpx, "post", post)


def _ok(req):
    return httpx.Response(200, json={"choices": [{"message": {"content": " hi "}}],
                                     "usage": {"prompt_tokens": 3, "completion_tokens": 4}})


def test_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        llm.chat([{"role": "user", "content": "x"}])


@pytest.mark.parametrize("key,style,header", [
    ("gsk_abc", "auto", "Authorization"), ("abc", "auto", "x-api-key"),
    ("abc", "bearer", "Authorization"), ("gsk_abc", "x-api-key", "x-api-key")])
def test_auth_header_styles(monkeypatch, key, style, header):
    monkeypatch.setattr(config, "LLM_API_KEY", key)
    monkeypatch.setattr(config, "LLM_AUTH_STYLE", style)
    assert header in llm._headers()


def test_chat_returns_content_and_usage(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    _patch_post(monkeypatch, _ok)
    out = llm.chat([{"role": "user", "content": "x"}])
    assert out["content"] == "hi" and out["prompt_tokens"] == 3 and out["completion_tokens"] == 4


@pytest.mark.parametrize("handler,match", [
    (lambda r: httpx.Response(500, text="boom"), "API error 500"),
    (lambda r: httpx.Response(200, json={"nope": 1}), "Unexpected"),
    (lambda r: httpx.Response(200, text="not json"), "Unexpected"),
])
def test_chat_errors(monkeypatch, handler, match):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    _patch_post(monkeypatch, handler)
    with pytest.raises(LLMError, match=match):
        llm.chat([])


def test_chat_timeout_and_transport_errors(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")

    def timeout(req):
        raise httpx.ReadTimeout("slow")

    _patch_post(monkeypatch, timeout)
    with pytest.raises(LLMError, match="timed out"):
        llm.chat([])

    def refused(req):
        raise httpx.ConnectError("down")

    _patch_post(monkeypatch, refused)
    with pytest.raises(LLMError, match="failed"):
        llm.chat([])


def test_configured_reflects_backend(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    assert llm.configured() is False
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    assert llm.configured() is True
    monkeypatch.setattr(config, "LLM_BACKEND", "bob_shell")
    monkeypatch.setattr(config, "BOBSHELL_API_KEY", "")
    assert llm.configured() is False
    monkeypatch.setattr(config, "BOBSHELL_API_KEY", "k")
    monkeypatch.setattr("core.llm_bob.find_bob", lambda: "bob")
    assert llm.configured() is True
    monkeypatch.setattr("core.llm_bob.find_bob", lambda: "")
    assert llm.configured() is False


def test_chat_dispatches_to_bob_shell_by_default(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "bob_shell")
    monkeypatch.setattr("core.llm_bob.chat", lambda m, mt, t: {"content": "from bob"})
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "from bob"
