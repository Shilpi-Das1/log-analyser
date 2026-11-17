# Log Analyser v2 - Flow and Architecture

Share this file with the team. The diagrams render on GitHub, GitLab and in VS Code (Mermaid).
Items marked **(later)** are designed but not built yet.

## 1. End-to-end flow

```mermaid
flowchart TD
    A["Upload log file<br/>choose Project"] --> B["Log parser (no LLM)<br/>pytest, JUnit XML, Maven, Go, generic"]
    B --> C{"Result per test"}
    C -->|Passed| P["Report only"]
    C -->|Skipped| S["Report only<br/>never analysed"]
    C -->|"Failed or parse error"| D["Analyse failure"]

    D --> E{"Knowledge base<br/>verified fix, similarity >= 0.8?"}
    E -->|Yes| F["Replay stored fix blocks on current code<br/>no LLM call"]
    E -->|No| G["LLM analysis<br/>category, evidence, confidence<br/>source lookup from project repo"]
    G --> H{"Confident?"}
    H -->|No| I["Ask user for more info<br/>re-analyse with the answers"]
    I --> G
    H -->|Yes| J{"Category"}
    J -->|"code fix possible<br/>repo mode"| K["Generate patch<br/>OLD/NEW blocks, diff built locally"]
    J -->|"product_bug"| N
    J -->|"env_infra, other"| W["Show diagnosis<br/>optionally save to KB as suggested"]
    F --> L
    K --> L["Rerun with the patch<br/>manual rerun log upload today<br/>Jenkins trigger (later)"]

    L --> M["submit_rerun<br/>parse the rerun log"]
    M --> Q{"Test passes now?"}
    Q -->|Yes| R["Draft pull request<br/>human approval, then GitHub API"]
    Q -->|"No, attempts left (max 2)"| T["Re-analyse<br/>previous analysis + new log"]
    T --> G
    Q -->|"No, attempts exhausted"| N["Draft issue<br/>human approval, then GitHub API<br/>evidence + fix attempts"]

    R --> U["KB: problem + fix, status verified"]
    N --> V["KB: problem, status open_issue"]
```

## 2. Components

```mermaid
flowchart LR
    subgraph Clients
        UI["Web UI<br/>light theme"]
        AG["MCP clients<br/>Bob or other agents"]
    end

    subgraph Service["log-analyser-v2 container"]
        API["Flask API + MCP server<br/>both call core/service.py"]
        REG["Project registry<br/>projects.json"]
        PAR["Log parsers<br/>core/parsers.py"]
        ANA["Classifier<br/>interactive needs-info loop"]
        KB[("Knowledge base<br/>SQLite")]
        PAT["Patcher<br/>exact-match blocks + difflib"]
        GH["GitHub client<br/>approval gate, dry-run"]
        JEN["Jenkins client (later)"]
        ST[("Run and case store<br/>JSON files")]
    end

    subgraph External
        LLM["LLM endpoint<br/>OpenAI-compatible"]
        GHB["GitHub<br/>issues and PRs"]
        JK["Jenkins (later)"]
        REPOS[("Project repos<br/>mounted read-only under PROJECTS_ROOT")]
    end

    UI --> API
    AG --> API
    API --> REG
    API --> PAR
    API --> ANA
    ANA --> KB
    ANA --> LLM
    ANA --> REPOS
    API --> PAT
    PAT --> REPOS
    PAT --> LLM
    API --> GH --> GHB
    API --> ST
    API -.-> JEN -.-> JK
```

## 3. Project registry entry

| Field | Example |
|---|---|
| `name` | `ocs-ci`, `payments-java` |
| `path` | sub-folder of `PROJECTS_ROOT` containing the repo (missing folder = generic mode) |
| `log_format` | `auto`, `pytest`, `junit`, `go`, `maven`, `generic` |
| `github_repo` | `org/repo` for issues and PRs (falls back to `GITHUB_REPO`) |
| `domain_hint` | free text added to the classifier prompt |
| Jenkins job and parameters **(later)** | job name; parameters for branch and test selector |

## 4. Re-analysis loop

```mermaid
sequenceDiagram
    participant U as User or agent
    participant S as Log Analyser
    participant L as LLM
    participant G as GitHub
    U->>S: submit_log(project, log)
    S->>S: Parse per-test results
    U->>S: analyse_failure(test)
    S->>S: KB lookup (miss)
    S->>L: Classify with log + source
    L-->>S: Low confidence + questions
    S-->>U: Ask for more info
    U->>S: provide_info(answer)
    S->>L: Re-classify with the answer
    L-->>S: Diagnosis
    U->>S: generate_patch
    S->>L: Request OLD/NEW blocks
    L-->>S: Blocks (validated against the file)
    Note over U,S: Rerun with the patch (Jenkins later)
    U->>S: submit_rerun(log)
    alt Test passes
        S->>S: KB entry = verified
        S-->>U: Draft pull request
        U->>S: create_pull_request(approved=true)
        S->>G: Branch, commit, PR
    else Test still fails
        S->>L: Re-analyse with new log + previous analysis
        Note over S: After 2 attempts, issue is offered
        S-->>U: Draft issue
        U->>S: create_issue(approved=true)
        S->>G: Create issue
    end
```

## 5. Decisions worth explaining in the demo

- The parser is deterministic: pass/fail/skip never depends on the LLM.
- The KB reuses only **verified** fixes (a rerun passed) and replays them with exact-match blocks, so a stale fix fails safely and falls back to the LLM.
- A timeout is a symptom: without a stated awaited operation, confidence is capped and the user is asked questions.
- Nothing is published without `approved=true`; `GITHUB_DRY_RUN` allows rehearsals.
- Repositories are read-only inputs; PRs are created through the GitHub API.
