"""MCP server exposing the log analyser to agents (Bob, Claude, etc.).

Transports: stdio (default, for local agents) or streamable-http (Docker / remote).
Publishing tools require `approved=true`; agents must show the draft to a human first.
"""
import os
import sys

from mcp.server.mcpserver import MCPServer

from core import service
from core.errors import AppError

mcp = MCPServer(
    "log-analyser",
    instructions=(
        "Triage failed tests from CI logs. Workflow: list_projects -> submit_log (parses passed/failed/"
        "skipped; no LLM) -> analyse_failure for each FAILED test (skipped tests are never analysed; "
        "known problems are answered from the knowledge base without an LLM) -> if needs_info, call "
        "provide_info -> generate_patch for code fixes, then submit_rerun with the rerun log -> "
        "draft_pull_request when the rerun passed, draft_issue otherwise. SHOW every draft to the user "
        "and call create_issue / create_pull_request with approved=true only after explicit approval."
    ),
)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AppError as exc:
        return {"error": str(exc)}


@mcp.tool()
def list_projects() -> list | dict:
    """List registered projects (name, repo path, log format, GitHub repo)."""
    return _run(service.list_projects)


@mcp.tool()
def add_project(name: str, path: str = "", log_format: str = "auto", github_repo: str = "",
                domain_hint: str = "") -> dict:
    """Register a project. `path` is a directory under the projects root (defaults to the name)."""
    return _run(service.add_project, name, path, log_format, github_repo, domain_hint)


@mcp.tool()
def submit_log(project: str, log_text: str, name: str = "log") -> dict:
    """Parse a log into per-test results (passed/failed/skipped). Returns a run with counts."""
    return _run(service.submit_log, project, log_text, name)


@mcp.tool()
def get_run(run_id: str) -> dict:
    """Return a run and its per-test statuses."""
    return _run(service.get_run, run_id)


@mcp.tool()
def analyse_failure(run_id: str, test_id: str) -> dict:
    """Analyse ONE failed test. Uses the knowledge base first; calls the LLM only on a miss."""
    return _run(service.analyse_failure, run_id, test_id)


@mcp.tool()
def provide_info(case_id: str, answer: str, question: str = "") -> dict:
    """Give extra logs/info for a case (answering a missing_info question) and re-analyse."""
    return _run(service.provide_info, case_id, answer, question)


@mcp.tool()
def get_case(case_id: str) -> dict:
    """Return the current state of a case."""
    return _run(service.get_case, case_id)


@mcp.tool()
def generate_patch(case_id: str, notes: str = "") -> dict:
    """Generate a unified-diff patch for the diagnosed target file (needs a project repository)."""
    return _run(service.generate_patch, case_id, notes)


@mcp.tool()
def submit_rerun(case_id: str, log_text: str) -> dict:
    """Submit the log of a rerun with the patch applied. Passed -> verified in the knowledge base;
    failed -> automatic re-analysis with the new log."""
    return _run(service.submit_rerun, case_id, log_text)


@mcp.tool()
def draft_issue(case_id: str) -> dict:
    """Draft a GitHub issue (product bug, or a fix that failed its rerun). Nothing is published."""
    return _run(service.draft_issue, case_id)


@mcp.tool()
def create_issue(case_id: str, approved: bool = False) -> dict:
    """Publish the drafted issue. Requires approved=true AFTER a human reviewed the draft."""
    return _run(service.create_issue, case_id, approved)


@mcp.tool()
def draft_pull_request(case_id: str) -> dict:
    """Draft a pull request from the generated patch. Nothing is published."""
    return _run(service.draft_pr, case_id)


@mcp.tool()
def create_pull_request(case_id: str, approved: bool = False) -> dict:
    """Open the pull request. Requires approved=true AFTER a human reviewed the draft."""
    return _run(service.create_pr, case_id, approved)


@mcp.tool()
def kb_search(project: str = "") -> list:
    """List knowledge-base entries (problem statement, fix, status), optionally for one project."""
    return _run(service.kb_list, project)


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("MCP_PORT", "8001")))


if __name__ == "__main__":
    main()
