"""LLM entry point. `chat()` dispatches to Bob Shell (default) or an OpenAI-compatible HTTP endpoint,
selected by LLM_BACKEND (`bob_shell` | `http`). Every caller uses `llm.chat()`."""
import time

import httpx

from core import config
from core.errors import LLMError


def _headers() -> dict:
    key = config.LLM_API_KEY
    if not key:
        raise LLMError("LLM_API_KEY is not set (copy .env.example to .env and add a key).")
    style = config.LLM_AUTH_STYLE
    if style == "auto":
        style = "bearer" if key.startswith("gsk_") else "x-api-key"
    headers = {"Content-Type": "application/json"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["x-api-key"] = key
    return headers


def configured() -> bool:
    if config.LLM_BACKEND == "http":
        return bool(config.LLM_API_KEY)
    from core import llm_bob
    return bool(config.BOBSHELL_API_KEY and llm_bob.find_bob())


def chat(messages: list[dict], max_tokens: int | None = None, temperature: float = 0.2) -> dict:
    """Return {content, prompt_tokens, completion_tokens, elapsed_s}."""
    if config.LLM_BACKEND != "http":
        from core import llm_bob
        return llm_bob.chat(messages, max_tokens, temperature)
    return _http_chat(messages, max_tokens, temperature)


def _http_chat(messages: list[dict], max_tokens: int | None, temperature: float) -> dict:
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "temperature": temperature,
    }
    started = time.perf_counter()
    try:
        resp = httpx.post(config.LLM_API_BASE, headers=_headers(), json=payload,
                          timeout=config.LLM_TIMEOUT_S)
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMError("LLM request timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise LLMError(f"LLM API error {exc.response.status_code}: {exc.response.text[:400]}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        raise LLMError(f"Unexpected LLM response: {resp.text[:300]}") from exc

    usage = data.get("usage", {})
    return {
        "content": content,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }
