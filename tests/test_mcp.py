"""MCP tools: functions are called directly, plus a real in-memory protocol round-trip."""
import asyncio

import mcp_server
from tests.conftest import PYTEST_FAIL, PYTEST_PASS, cls, fake_llm

OLD = "    return s * (1 - d) * (1 - d)"
PATCH_REPLY = f"<<<OLD\n{OLD}\n|||\n    return s * (1 - d)\n>>>NEW"
TEST_ID = "tests/test_a.py::test_boom"

EXPECTED_TOOLS = {
    "list_projects", "add_project", "submit_log", "get_run", "analyse_failure", "analyse_all_failures", "provide_info",
    "get_case", "generate_patch", "submit_rerun", "draft_issue", "create_issue",
    "draft_pull_request", "create_pull_request", "kb_search"}


def test_agent_workflow_through_tool_functions(monkeypatch):
    fake_llm(monkeypatch, [cls(), PATCH_REPLY])
    assert mcp_server.add_project("shop", "shop", "pytest", "o/shop")["name"] == "shop"
    assert mcp_server.list_projects()[0]["name"] == "shop"

    run = mcp_server.submit_log("shop", PYTEST_FAIL, "ci")
    assert mcp_server.get_run(run["id"])["counts"]["failed"] == 1
    case = mcp_server.analyse_failure(run["id"], TEST_ID)
    assert mcp_server.get_case(case["id"])["id"] == case["id"]
    assert mcp_server.generate_patch(case["id"], "n")["patch"]["diff"]
    assert mcp_server.submit_rerun(case["id"], PYTEST_PASS)["rerun"]["status"] == "passed"
    assert mcp_server.draft_pull_request(case["id"])["pr_draft"]

    assert "error" in mcp_server.create_pull_request(case["id"])           # approval defaults to False
    assert mcp_server.create_pull_request(case["id"], True)["published"]["dry_run"] is True
    assert mcp_server.kb_search("shop")[0]["status"] == "verified"


def test_issue_and_info_tools(monkeypatch):
    fake_llm(monkeypatch, [cls(), cls()])
    mcp_server.add_project("shop", "shop", "pytest", "o/shop")
    run = mcp_server.submit_log("shop", PYTEST_FAIL)
    case = mcp_server.analyse_failure(run["id"], TEST_ID)
    assert mcp_server.provide_info(case["id"], "details", "q")["info"]
    assert mcp_server.draft_issue(case["id"])["issue_draft"]
    assert "error" in mcp_server.create_issue(case["id"])
    assert mcp_server.create_issue(case["id"], True)["published"]["dry_run"] is True


def test_errors_become_error_payloads_not_exceptions():
    assert "error" in mcp_server.submit_log("ghost", "x")
    assert "error" in mcp_server.get_case("0123456789")
    assert "error" in mcp_server.analyse_failure("0123456789", "t")


def test_all_tools_registered_over_protocol():
    from mcp import Client

    async def go():
        async with Client(mcp_server.mcp) as client:
            listed = await client.list_tools()
            return {t.name for t in listed.tools}

    assert asyncio.run(go()) == EXPECTED_TOOLS


def test_main_selects_transport(monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr("sys.argv", ["mcp_server.py", "stdio"])
    mcp_server.main()
    monkeypatch.setattr("sys.argv", ["mcp_server.py", "streamable-http"])
    monkeypatch.setenv("MCP_PORT", "9123")
    mcp_server.main()
    assert seen[0] == (("stdio",), {})
    assert seen[1][0] == ("streamable-http",) and seen[1][1]["port"] == 9123
