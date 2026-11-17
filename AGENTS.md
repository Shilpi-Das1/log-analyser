# AGENTS.md

Context for agents and fresh chats working on **log-analyser-v2**. This folder is a standalone
project (it may be committed on its own); do not import from or edit any sibling project.

## Purpose

Failure-log triage for multiple projects. Upload a log → per-test result → analyse only failed tests →
interactive "needs more info" loop → patch → rerun log → PR (passed) or issue (failed), always after
human approval. Web UI (Flask) and MCP server share `core/service.py`. Verified fixes go into a
knowledge base and are replayed later without an LLM call.

Diagrams: `docs/flow-diagram.md`. User docs: `README.md`.

## Architecture

| Path | Role |
|---|---|
| `app.py` | Flask JSON API + `templates/index.html` (light-theme SPA, 3 tabs) |
| `mcp_server.py` | 14 MCP tools (v2 API: `from mcp.server.mcpserver import MCPServer`, `@mcp.tool()`) |
| `run.py` | Container entrypoint: gunicorn (:5000) + MCP streamable-http (:8001/mcp). Linux only |
| `core/service.py` | All orchestration; the only layer web/MCP call |
| `core/projects.py` | Registry in `DATA_DIR/projects.json`; paths must stay inside `PROJECTS_ROOT` |
| `core/parsers.py` | Deterministic per-test parsers: pytest, junit, go, maven, generic |
| `core/extract.py` | `ERROR_RE`, `clip`, `build_excerpt` (used by parsers/kb), format sniffing |
| `core/locator.py` | Stack-trace frames (py/java/go/shell/...) → files in the project repo |
| `core/classifier.py` | LLM classification + confidence gate + missing-info questions |
| `core/patcher.py` | OLD/NEW block patches, diff built locally; `replay()` for KB fixes |
| `core/kb.py` | SQLite KB (`DATA_DIR/knowledge.db`), Jaccard similarity on normalised signatures |
| `core/github.py` | REST client; every function takes `repo` first; dry-run support |
| `core/store.py` | JSON store for `runs` (max 30) and `cases` (max 200) |
| `core/llm.py` | The only entry point for LLM calls (`chat()`); dispatches on `LLM_BACKEND` |
| `core/llm_bob.py` | Default backend: runs the `bob` CLI (like the original project). Prompt on stdin, `--mode ask`, empty workspace `DATA_DIR/bob-workspace`, key from `BOBSHELL_API_KEY` |
| `projects/demo-shop/` | Demo repo with a double-discount bug in `shop/pricing.py` |
| `samples/` | Demo logs (pytest, JUnit XML, shell, go) |

## Commands

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PROJECTS_ROOT = "$PWD\projects"          # default is /workspace/projects
python app.py                                 # UI :5000
python mcp_server.py streamable-http          # MCP :8001/mcp  (no arg = stdio)
python -m pytest --cov                        # tests + coverage (config: pytest.ini, .coveragerc)
docker compose up --build                     # needs .env (copy .env.example)
```

Tests need no network, LLM or GitHub: `tests/conftest.py` isolates `DATA_DIR`/`PROJECTS_ROOT`, forces
`GITHUB_DRY_RUN=true`, and `fake_llm(monkeypatch, replies)` stubs `llm.chat`. Keep coverage ≥ 95%.

## Conventions

- Internal imports are `from core import ...` (the project root is on `sys.path`).
- All LLM calls go through `core/llm.chat()`; all GitHub calls through `core/github.py`.
- Service functions raise `AppError` subclasses (`core/errors.py`); Flask maps them to JSON 4xx,
  MCP tools return `{"error": ...}`. Do not let those escape as 500s.
- UI renders with `textContent` / DOM building only. Never use `innerHTML` with log or LLM text.
- Config comes from `core/config.py` (parses `.env` itself; `LLM_*` or `BOB_*` names).

## Safety rules (do not weaken)

- Publish only when `approved is True` (strict boolean). Drafts never publish.
- `GITHUB_DRY_RUN=true` returns placeholder URLs and never calls write APIs.
- Project repos must resolve inside `PROJECTS_ROOT`; patch targets must resolve inside the project root.
- Only `failed` tests are analysed (skipped/passed raise `AppError`). Parse errors count as failed.
- KB auto-reuse requires status `verified`, stored patch blocks, and score ≥ `KB_MATCH_THRESHOLD`; a
  replay that no longer matches the file falls back to the LLM.
- A timeout is a symptom: `flaky_timeout` without `waiting_on` is capped at 0.4 confidence
  (`classifier.normalize`). Do not add "increase the timeout" defaults.

## Gotchas

- `submit_rerun` keeps an attempt counter; `MAX_ATTEMPTS = 2`. `generate_patch` preserves it
  (`rerun.status == "pending"`), and the UI ignores `pending`. A failed rerun clears the patch.
- KB status precedence: `verified` > `suggested` > `open_issue`; a lower status never overwrites a higher one.
- Case JSON on disk holds `excerpt`, `patch.new_content` and `kb_hints`; `service._public()` strips them from API output.
- The mounted projects volume is read-only in compose; PRs use the GitHub contents API, never local writes.
- `mcp` is 2.x: `FastMCP` does not exist; use `MCPServer`; client is `from mcp import Client`.
- Windows: gunicorn does not run; use `python app.py`. `run.py` is for the Linux container.
- Tooling quirk in some editors: multi-line `search_and_replace` can fail; prefer line-anchored diffs.
- Docker image was not built in the authoring environment (no Docker). Build and smoke it before relying on it. It installs Bob Shell with the same installer as `docker/Dockerfile` in the parent repo; that step is unverified.
- Bob Shell: the prompt must go on stdin (Windows command lines are limited to ~8 KB and `bob.cmd` goes through cmd.exe). A `401` in `bob run --log-level debug` / "Error while calling Bob's backend service" means an invalid `BOBSHELL_API_KEY`, not a code bug. `llm_bob` does not support `max_tokens`/`temperature` and reports estimated token counts.

## Next steps (not built)

1. **Jenkins client**: add the project fields (job, parameter mapping), push a fix branch, trigger the job via the REST API, poll,
   fetch the log, then call `service.submit_rerun(case_id, log_text)`. Needs: approval before the first run, attempt cap, branch cleanup.
2. KB: embeddings or FTS ranking, entry editing in the UI, pruning.
3. Multi-file patches; real git-based PRs; auth on the web UI and MCP endpoint.
4. Parsers for more frameworks (TAP, Robot, Jest) by adding a function to `core/parsers.py`.
