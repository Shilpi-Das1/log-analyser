# MCP Tools Reference

The log analyser exposes **15 tools** over the MCP protocol.  
Each tool is an independent capability; an agent (Bob, watsonx Orchestrate, or any MCP client)
calls them in sequence to triage a CI failure end-to-end.

Start the server:

```powershell
# stdio (local agent / Bob Shell)
python mcp_server.py

# HTTP (watsonx Orchestrate, remote agents)
python mcp_server.py streamable-http   # listens on :8001/mcp
```

---

## Recommended workflow

```
list_projects
    └─► submit_log
            └─► analyse_all_failures  (or analyse_failure per test)
                    └─► provide_info  (if needs_info = true)
                            └─► generate_patch  ──► submit_rerun
                                                        ├─ passed ──► draft_pull_request ──► create_pull_request
                                                        └─ failed ──► draft_issue         ──► create_issue
                                                                        kb_search  (at any point)
```

---

## 4 Collaborator Agents for watsonx Orchestrate

Orchestrate calls sub-agents **"collaborator agents"**. Create one agent per group below in the
Orchestrate Agent Builder, give each only its subset of tools, then create a **supervisor
(orchestrator) agent** that lists the 4 collaborators as its tools.

| Agent | Tools | Responsibility |
|---|---|---|
| 🔧 **Project Agent** | `list_projects`, `add_project` | Knows what repos are registered; onboards new ones |
| 🔍 **Analysis Agent** | `submit_log`, `get_run`, `analyse_failure`, `analyse_all_failures`, `provide_info`, `get_case` | Parses logs, classifies failures, handles the info loop |
| 🩹 **Fix Agent** | `generate_patch`, `submit_rerun`, `kb_search` | Proposes code fixes, validates them via rerun, checks the knowledge base |
| 🐙 **GitHub Agent** | `draft_issue`, `create_issue`, `draft_pull_request`, `create_pull_request` | Files bugs and opens PRs on GitHub — always after human approval |

See [`flow-diagram.md`](flow-diagram.md) section 5 for the full routing diagram and hand-off table.

### Step-by-step: adding to watsonx Orchestrate

**Step 1 — Start the MCP server in HTTP mode**
```powershell
python mcp_server.py streamable-http
# listens on :8001/mcp
```
Make the server reachable from Orchestrate's network (public IP, VPN, or ngrok tunnel for local dev).

**Step 2 — Register the MCP server in Orchestrate**
1. Open **watsonx Orchestrate → Agent Builder → Connections** (or **Skills** → **Add skill source**).
2. Choose **MCP server**.
3. Enter the server URL: `http://<your-host>:8001/mcp`.
4. Orchestrate discovers all 15 tools automatically.

**Step 3 — Create the 4 collaborator agents**
For each agent in the table above:
1. Click **New agent** in Agent Builder.
2. Give it a name (e.g. `Analysis Agent`) and a system prompt describing its role.
3. Under **Tools**, select only the tools for that agent from the discovered MCP tool list.
4. Save and publish the agent.

Suggested system prompts:

| Agent | System prompt |
|---|---|
| Project Agent | `You manage project registrations. Use list_projects to show known repos and add_project to register new ones. Always confirm the project name and path before adding.` |
| Analysis Agent | `You triage CI failures. Parse logs with submit_log, then call analyse_all_failures (or analyse_failure for a single test). If needs_info is true, ask the user the missing_info questions and call provide_info with their answer. Never skip the info loop.` |
| Fix Agent | `You generate and validate code fixes. Use generate_patch when can_patch=true. After a patch is applied, the user uploads the rerun log — call submit_rerun. Use kb_search to check if the problem is already known.` |
| GitHub Agent | `You file GitHub issues and open pull requests. Always call draft_issue or draft_pull_request first and show the full draft to the user. Only call create_issue or create_pull_request after the user explicitly says "approve" or "yes, publish".` |

**Step 4 — Create the supervisor/orchestrator agent**
1. Click **New agent**, name it `CI Triage Orchestrator`.
2. Under **Tools**, add each of the 4 collaborator agents (Orchestrate treats agents as tools for the supervisor).
3. Set a system prompt, e.g.:
   > `You coordinate CI log triage. Delegate to Project Agent for repo setup, Analysis Agent for log parsing and failure classification, Fix Agent for patch generation, and GitHub Agent for issue/PR creation. Never approve GitHub publishing on behalf of the user — always ask for explicit confirmation first.`
4. Save and publish.

**Step 5 — Set environment variables**
```
GITHUB_TOKEN=ghp_...        # personal access token with repo scope
GITHUB_REPO=owner/repo      # default repo for issues and PRs
GITHUB_DRY_RUN=false        # set to true for rehearsals (no real GitHub calls)
```

---

## 1. `list_projects`

**Purpose:** List all registered projects.  
**Parameters:** none  
**Returns:** array of `{name, path, log_format, github_repo, domain_hint, available}`

Use this first to discover what projects are registered and check whether their repo path is available.

---

## 2. `add_project`

**Purpose:** Register a new project so logs can be linked to its repository.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique project name (letters, digits, `.`, `_`, `-`) |
| `path` | string | `""` | Sub-folder of `PROJECTS_ROOT` (defaults to `name`) |
| `log_format` | string | `"auto"` | `auto`, `pytest`, `junit`, `go`, `maven`, `generic` |
| `github_repo` | string | `""` | `owner/repo` for issues and PRs |
| `domain_hint` | string | `""` | Free text added to the classifier prompt |

**Returns:** the created project dict.

---

## 3. `submit_log`

**Purpose:** Parse a CI log into per-test results. **No LLM call** — deterministic parsing only.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project` | string | required | Registered project name (or `""` for no-project / generic mode) |
| `log_text` | string | required | Full log text |
| `name` | string | `"log"` | Display name for this run |

**Returns:** `{id, project, name, format, counts:{passed,failed,skipped,total}, tests:[...]}`

The `id` returned here is the `run_id` needed by all subsequent tools.

---

## 4. `get_run`

**Purpose:** Retrieve a previously parsed run by ID (useful after a server restart).  
**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `run_id` | string | Run ID from `submit_log` |

**Returns:** same shape as `submit_log`, with `case_id` filled in for already-analysed tests.

---

## 5. `analyse_failure`

**Purpose:** Analyse **one** failed test. Checks the knowledge base first; calls the LLM only on a cache miss.  
**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `run_id` | string | Run ID from `submit_log` |
| `test_id` | string | Test ID from the run's `tests` array |

**Returns:** a case dict with `{id, classification:{category, confidence, summary, root_cause, evidence, fix_suggestion, needs_info, missing_info, target_file}, capabilities:{can_patch, can_issue, mode}, source}`

If `needs_info` is true, call `provide_info` before proceeding.

---

## 6. `analyse_all_failures`

**Purpose:** Analyse **every** failed test in a run sequentially, in configurable batches. Already-cached cases return instantly without an LLM call. Best tool for Orchestrate flows.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `run_id` | string | required | Run ID from `submit_log` |
| `batch_size` | int | `5` | Tests per batch (1–20). Controls LLM concurrency. |

**Returns:**
```json
{
  "run_id": "...",
  "total_failed": 14,
  "analysed": 14,
  "results": [
    {"test_id": "...", "case_id": "...", "category": "env_infra",
     "confidence": 0.92, "needs_info": false, "summary": "..."}
  ],
  "errors": []
}
```

---

## 7. `provide_info`

**Purpose:** Supply additional context (logs, versions, command output) to answer a `missing_info` question and re-run the analysis.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `case_id` | string | required | Case ID from `analyse_failure` |
| `answer` | string | required | The additional information |
| `question` | string | `""` | The question being answered (for context) |

**Returns:** updated case dict (same shape as `analyse_failure`).

---

## 8. `get_case`

**Purpose:** Retrieve the current state of a case without re-analysing.  
**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `case_id` | string | Case ID |

**Returns:** case dict (same shape as `analyse_failure`).

---

## 9. `generate_patch`

**Purpose:** Generate a unified-diff patch for the diagnosed bug. Requires a project with a local repository and `capabilities.can_patch = true`.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `case_id` | string | required | Case ID |
| `notes` | string | `""` | Optional guidance for the LLM (e.g. "prefer minimal change") |

**Returns:** case dict with `patch:{file, diff, from_kb}` populated.

---

## 10. `submit_rerun`

**Purpose:** Feed the log of a test rerun after the patch was applied.  
- **Passed** → KB entry marked `verified`; PR can be drafted.  
- **Failed** → re-analysis with the new log; up to 2 attempts total.

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `case_id` | string | Case ID |
| `log_text` | string | Full rerun log text |

**Returns:** updated case dict with `rerun:{status, attempts}`.

---

## 11. `draft_issue`

**Purpose:** Draft a GitHub issue for a confident `product_bug` or after a fix failed its rerun. **Nothing is published.**  
**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `case_id` | string | Case ID |

**Returns:** case dict with `issue_draft:{title, body, duplicates}` populated.  
⚠️ Always show the draft to a human before calling `create_issue`.

---

## 12. `create_issue`

**Purpose:** Publish the drafted issue to GitHub.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `case_id` | string | required | Case ID with a populated `issue_draft` |
| `approved` | bool | `false` | **Must be `true`** — only set after a human reviewed the draft |

**Returns:** `{published:{url, number, dry_run}}` and updated case.  
Requires `GITHUB_TOKEN` and `GITHUB_REPO` (or project-level `github_repo`) in `.env`.

---

## 13. `draft_pull_request`

**Purpose:** Draft a pull request from the generated patch after a successful rerun. **Nothing is published.**  
**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `case_id` | string | Case ID with a patch and `rerun.status = "passed"` |

**Returns:** case dict with `pr_draft:{title, body}` populated.  
⚠️ Always show the draft to a human before calling `create_pull_request`.

---

## 14. `create_pull_request`

**Purpose:** Open the pull request on GitHub.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `case_id` | string | required | Case ID with a populated `pr_draft` |
| `approved` | bool | `false` | **Must be `true`** — only set after a human reviewed the draft |

**Returns:** `{published:{url, number, dry_run}}` and updated case.

---

## 15. `kb_search`

**Purpose:** List knowledge-base entries — known problems with their fix text and status. Useful for checking what the system already knows before starting a new triage.  
**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project` | string | `""` | Filter by project name; omit to list all entries |

**Returns:** array of `{id, project, problem, fix_text, status, patch_blocks, hits}`

KB statuses: `verified` (a rerun passed) > `suggested` (pending) > `open_issue` (issue filed).

---

## Safety rules

- `create_issue` and `create_pull_request` **require `approved=true`**. An agent must never pass `approved=true` without first showing the draft to a human and receiving explicit confirmation.
- `GITHUB_DRY_RUN=true` in `.env` makes both create tools return placeholder URLs and never call GitHub write APIs — safe for demos and rehearsals.
- Only `failed` tests can be analysed; calling `analyse_failure` on a passed or skipped test returns an error.
- Patch generation is unavailable in generic mode (no project repository). `capabilities.can_patch` indicates whether it is possible.

---

## Connecting to watsonx Orchestrate

1. Start the server: `python mcp_server.py streamable-http`
2. In Orchestrate's MCP settings, add the server URL: `http://<host>:8001/mcp`
3. Orchestrate discovers all 15 tools automatically.
4. Set `GITHUB_TOKEN`, `GITHUB_REPO`, and `GITHUB_DRY_RUN=false` in `.env` to enable real issue creation.
