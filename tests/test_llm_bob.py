import json
import subprocess

import pytest

from core import config, llm_bob
from core.errors import LLMError

MSGS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"}, {"role": "user", "content": "Q2"}]


@pytest.fixture(autouse=True)
def _bob(monkeypatch):
    monkeypatch.setattr(config, "BOBSHELL_API_KEY", "key")
    monkeypatch.setattr(config, "BOB_SHELL_MODE", "")
    monkeypatch.setattr(llm_bob, "_version", (2, 0))
    monkeypatch.setattr(llm_bob, "find_bob", lambda: "bob")


class Result:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def _run(monkeypatch, result):
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(llm_bob.subprocess, "run", fake)
    return seen


def test_collapse_single_and_multi_turn():
    assert llm_bob.collapse([{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]) == "S\n\nQ"
    out = llm_bob.collapse(MSGS)
    assert out.startswith("SYS") and "=== CONTEXT ===" in out and "[ASSISTANT]\nA1" in out
    assert out.endswith("=== QUESTION ===\nQ2")
    assert llm_bob.collapse([]) == ""


def test_extract_content_shapes():
    result = json.dumps({"type": "result", "last_message": "done"})
    assert llm_bob.extract_content("Model: x\n6 tokens\n" + result) == "done"
    assert llm_bob.extract_content('[{"role": "assistant", "content": "arr"}]') == "arr"
    assert llm_bob.extract_content("plain text") == "plain text"
    assert llm_bob.extract_content("") == ""


def test_clean_strips_noise_and_keeps_blocks_hidden_in_thinking():
    assert llm_bob.clean("<thinking>x</thinking>answer") == "answer"
    assert llm_bob.clean("a\n---output---\nbody\n---output---\nb") == "body"
    assert llm_bob.clean("---output---\nbody") == "body"
    assert llm_bob.clean("[using tool read_file]\nreal") == "real"
    hidden = "<thinking><<<OLD\na\n|||\nb\n>>>NEW</thinking>done"
    assert "<<<OLD" in llm_bob.clean(hidden)
    assert llm_bob.clean("<thinking>no blocks</thinking>text") == "text"


def test_chat_sends_prompt_on_stdin_in_isolated_ask_workspace(monkeypatch):
    seen = _run(monkeypatch, Result(out=json.dumps({"last_message": "pong"})))
    out = llm_bob.chat(MSGS)
    assert out["content"] == "pong" and out["prompt_tokens"] >= 1 and out["completion_tokens"] >= 1
    cmd = seen["cmd"]
    assert cmd[:2] == ["bob", "run"] and cmd[cmd.index("--mode") + 1] == "ask"
    assert "--disable-mcp" in cmd and "Q2" not in " ".join(cmd)          # prompt never on the command line
    assert "Q2" in seen["kw"]["input"]
    assert seen["kw"]["env"]["BOBSHELL_API_KEY"] == "key"
    assert str(seen["kw"]["cwd"]).endswith("bob-workspace")
    assert cmd[cmd.index("--workspace") + 1] == str(seen["kw"]["cwd"])


def test_mode_override_and_bob1_command(monkeypatch):
    monkeypatch.setattr(config, "BOB_SHELL_MODE", "plan")
    seen = _run(monkeypatch, Result(out=json.dumps({"content": "ok"})))
    llm_bob.chat(MSGS)
    assert seen["cmd"][seen["cmd"].index("--mode") + 1] == "plan"
    monkeypatch.setattr(llm_bob, "_version", (1, 0))
    seen = _run(monkeypatch, Result(out=json.dumps({"content": "ok"})))
    llm_bob.chat(MSGS)
    assert "--output-format" in seen["cmd"] and seen["cmd"][-2:] == ["-p", "-"] and "run" not in seen["cmd"]


def test_missing_cli_or_key(monkeypatch):
    monkeypatch.setattr(llm_bob, "find_bob", lambda: "")
    with pytest.raises(LLMError, match="not found"):
        llm_bob.chat(MSGS)
    monkeypatch.setattr(llm_bob, "find_bob", lambda: "bob")
    monkeypatch.setattr(config, "BOBSHELL_API_KEY", "")
    with pytest.raises(LLMError, match="BOBSHELL_API_KEY"):
        llm_bob.chat(MSGS)


@pytest.mark.parametrize("result,match", [
    (Result(err="Error: Request Failed", code=1), "exited with code 1"),
    (Result(err="[IDEClient] Failed to connect", code=1), "IDE companion"),
    (Result(out="<thinking>only</thinking>"), "empty reply"),
    (subprocess.TimeoutExpired("bob", 1), "timed out"),
    (FileNotFoundError("nope"), "Could not launch"),
])
def test_failures_become_llm_errors(monkeypatch, result, match):
    _run(monkeypatch, result)
    with pytest.raises(LLMError, match=match):
        llm_bob.chat(MSGS)


def test_find_bob_and_version_detection(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(llm_bob.shutil, "which", lambda n: "/x/bob" if n in ("bob", "bob.cmd") else None)
    assert llm_bob.find_bob() in ("bob", "bob.cmd")
    monkeypatch.setattr(llm_bob.shutil, "which", lambda n: None)
    assert llm_bob.find_bob() == ""

    monkeypatch.setattr(llm_bob, "_version", None)
    _run(monkeypatch, Result(out="2.0.1\ncommit: abc"))
    assert llm_bob._version_of("bob") == (2, 0)
    monkeypatch.setattr(llm_bob, "_version", None)
    _run(monkeypatch, Result(out="1.4.0"))
    assert llm_bob._version_of("bob") == (1, 4)
    monkeypatch.setattr(llm_bob, "_version", None)
    _run(monkeypatch, Result(out="garbage"))
    assert llm_bob._version_of("bob") == (2, 0)
    monkeypatch.setattr(llm_bob, "_version", None)
    _run(monkeypatch, OSError("boom"))
    assert llm_bob._version_of("bob") == (2, 0)
