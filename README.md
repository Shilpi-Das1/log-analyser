# Log Analyser v2

Failure-log triage for **multiple projects** (ocs-ci, Java services, shell pipelines, …). Upload a
log, get per-test results (passed / failed / skipped), analyse only the failures, answer the
analyser's follow-up questions when it is unsure, generate a patch, and open a GitHub **issue** or
**pull request** after a human approves. Exposed as both a **web UI** and an **MCP server**.

> This project is fully independent of the original `log-analyser/` directory — nothing there is imported or modified.

Diagrams: [`docs/flow-diagram.md`](docs/flow-diagram.md) · Context for agents: [`AGENTS.md`](AGENTS.md)

---

## How It Works

1. **Register a project** (Projects tab or `add_project` MCP tool). Each project has a repo folder,
   a log format (`auto` / `pytest` / `junit` / `go` / `maven` / `generic`), an optional GitHub repo,
   and a domain hint.
2. **Upload a log** → deterministic parser (no LLM) produces per-test results. Parse errors count as failed; skipped tests are never analysed.
3. **Analyse a failed test**
   - **Knowledge base first**: a *verified* fix with similarity ≥ `KB_MATCH_THRESHOLD` is replayed against the current file — no LLM call.
   - **Otherwise the LLM** classifies the failure (`test_bug | product_bug | env_infra | flaky_timeout | unknown`) using the stack-trace source from the project repo.
4. **Interactive loop**: below the confidence threshold the UI asks specific questions; answers are
   added as evidence and the analysis re-runs. A timeout without a stated *waiting_on* is capped at
   0.4 confidence and forces follow-up questions.
5. **Patch** (repo mode only): the model emits OLD/NEW blocks that must match the file exactly; the diff is built locally.
6. **Rerun**: upload the log of a rerun with the patch applied.
   - **Passed** → fix saved as *verified* in the KB; PR can be drafted.
   - **Failed** → re-analysed with the new log; after 2 attempts an issue is offered.
7. **Approval gate**: drafts never publish. `create_issue` / `create_pr` require `approved=true`.

| Mode | When | Capabilities |
|---|---|---|
| **repo** | project folder exists under `PROJECTS_ROOT` | source lookup, patch, PR, issue |
| **generic** | no folder registered | analysis, Q&A, issue drafting (no patch) |

---

## Prerequisites

| Requirement | Local | Docker |
|---|---|---|
| Python 3.12+ | ✅ Required | Not needed |
| Docker + Docker Compose | Not needed | ✅ Required |
| Bob Shell CLI + `BOBSHELL_API_KEY` | ✅ `bob` on PATH (bob 2.x) | Installed in the image; only the key is needed |
| GitHub token | Optional | Optional |

---

## 🐳 Run with Docker

### Option A — Docker Compose (recommended)

```bash
cp .env.example .env
# Edit .env: set BOBSHELL_API_KEY (IBM Bob IDE -> Settings -> API Keys -> Shell)
docker compose up --build
```

Run in the background:

```bash
docker compose up --build -d
docker compose logs -f      # tail logs
docker compose down         # stop
```

### Option B — Plain `docker run`

```bash
docker build -t log-analyser-v2 .
docker run --rm --env-file .env \
  -p 5000:5000 -p 8001:8001 \
  -v "$PWD/projects:/workspace/projects:ro" \
  -v la-data:/data \
  log-analyser-v2
```

**Endpoints after startup:**

| URL | Purpose |
|---|---|
| http://localhost:5000 | Web UI (3 tabs: Runs, Projects, KB) |
| http://localhost:5000/healthz | Health check → `{"status": "ok"}` |
| http://localhost:8001/mcp | MCP streamable-http endpoint |

> Place project repositories under `./projects/<name>/` (or set `PROJECTS_HOST_PATH` in `.env`).
> They are mounted **read-only** at `/workspace/projects` inside the container.

---

## 🖥️ Run Locally (without Docker)

> **Windows / PowerShell instructions.** On Linux/macOS replace the venv activation with `source .venv/bin/activate`.

### 1 — Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2 — Install dependencies

```powershell
pip install -r requirements.txt
```

### 3 — Configure environment

```powershell
Copy-Item .env.example .env
# Open .env and set BOBSHELL_API_KEY (and optionally GITHUB_TOKEN, GITHUB_REPO)
```

### 4 — Set the projects root (optional)

```powershell
$env:PROJECTS_ROOT = "$PWD\projects"   # default is /workspace/projects
```

### 5a — Start the Web UI

```powershell
python app.py
```

> Flask dev server starts on **http://localhost:5000**

### 5b — Start the MCP server (separate terminal)

```powershell
python mcp_server.py streamable-http   # listens on http://localhost:8001/mcp
# or:
python mcp_server.py                   # stdio mode (for local MCP clients)
```

> `run.py` starts both under gunicorn in a single process and is for the **Linux container only**
> (gunicorn does not run on Windows).

### 6 — Run the test suite

```powershell
python -m pytest --cov
```

> No network, LLM, or GitHub access needed. All stubs are in `tests/conftest.py`.
> Target coverage: **≥ 95%**.

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and edit the values below.

| Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | *(empty)* | **Required.** Your LLM API key |
| `LLM_API_BASE` | IBM Bob inference URL | OpenAI-compatible chat completions endpoint |
| `LLM_MODEL` | `premium-ide` | Model name to use |
| `LLM_AUTH_STYLE` | `auto` | `auto` / `bearer` / `x-api-key` |
| `PROJECTS_HOST_PATH` | `./projects` | Host path mounted into the container (Docker only) |
| `PROJECTS_ROOT` | `/workspace/projects` | Projects root for local runs |
| `GITHUB_TOKEN` | *(empty)* | GitHub token (`repo` scope, or fine-grained Issues + Contents + PRs write) |
| `GITHUB_REPO` | *(empty)* | Fallback repo `owner/name` for projects without their own |
| `GITHUB_DRY_RUN` | `true` | `true` → never call GitHub write APIs; returns placeholder URLs |
| `CONFIDENCE_THRESHOLD` | `0.6` | Minimum confidence to skip follow-up questions |
| `KB_MATCH_THRESHOLD` | `0.8` | Minimum similarity for a KB fix to be reused without LLM |

> A `401` / "Error while calling Bob's backend service" from `bob` means the key is expired or invalid; create a new one in the Bob IDE.

---

## 🤖 MCP Tools

Register the server in your MCP client config:

```json
{
  "mcpServers": {
    "log-analyser": {
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

**Available tools:** `list_projects`, `add_project`, `submit_log`, `get_run`, `analyse_failure`,
`provide_info`, `get_case`, `generate_patch`, `submit_rerun`, `draft_issue`, `create_issue`,
`draft_pull_request`, `create_pull_request`, `kb_search`

Errors are returned as `{"error": "..."}` (never as 500s).

---

## 🧪 Demo Script (≈ 5 min)

1. **Projects tab** → add `demo-shop` (path `demo-shop`, format `pytest`, GitHub repo `owner/demo-shop`).
2. **Analyse tab** → load sample `pytest_demo_shop.log` → 1 failed, 2 passed, 1 skipped. The skipped test has no *Analyse* button.
3. Analyse the failure → double-discount bug in product code → **Generate patch** → view diff.
4. Upload a passing rerun log → *verified* → draft PR → approve (dry-run mode).
5. Upload the failing log again → answered from the **knowledge base** without an LLM call.
6. Add a `generic` project, load `shell_deploy_failure.log`: the timeout is a symptom (`ImagePullBackOff`); answer the questions and watch the confidence change; draft an issue.
7. Load `java_junit_results.xml` / `go_test_output.log` to see other log formats.

---

## 📂 Project Layout

```
app.py              Flask JSON API + single-page UI
mcp_server.py       14 MCP tools (v2 MCPServer API)
run.py              Linux container entrypoint (gunicorn + MCP)
core/
  config.py         Env-var parsing (.env loaded here)
  service.py        All orchestration — the only layer web/MCP call
  projects.py       Project registry (DATA_DIR/projects.json)
  parsers.py        Deterministic per-test parsers: pytest, junit, go, maven, generic
  extract.py        ERROR_RE, clip, build_excerpt, format sniffing
  locator.py        Stack-trace frames → files in the project repo
  classifier.py     LLM classification + confidence gate + missing-info questions
  patcher.py        OLD/NEW block patches; replay() for KB fixes
  kb.py             SQLite knowledge base (DATA_DIR/knowledge.db)
  github.py         GitHub REST client; dry-run support
  store.py          JSON store: last 30 runs, last 200 cases
  llm.py            Single LLM call point (dispatches to llm_bob or HTTP)
  llm_bob.py        Bob Shell CLI backend (prompt on stdin, `ask` mode, empty workspace)
  errors.py         AppError subclasses (mapped to JSON 4xx)
templates/          Single-page UI (light theme, 3 tabs)
samples/            Demo logs (pytest, JUnit XML, shell, Go)
projects/demo-shop/ Sample buggy repo (double-discount in shop/pricing.py)
tests/              pytest suite (no network/LLM needed)
docs/               Flow diagrams
```

---

## ⚠️ Known Limits

- Verdicts are evidence-based, not guaranteed. The confidence gate and human approval are the safeguards.
- One file per patch / PR. PRs are created via the GitHub Contents API, so the mounted repo is never written to.
- KB matching uses Jaccard token similarity on normalised error text (no embeddings).
- Data retention: last **30 runs** and **200 cases** are kept; the KB is not pruned automatically.
- `run.py` (gunicorn) is **Linux-only**. On Windows run `python app.py` and `python mcp_server.py` separately.
