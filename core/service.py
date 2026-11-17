"""Service layer shared by the Flask UI and the MCP server.

Flow: submit_log (parse, no LLM) -> analyse_failure (KB first, LLM only on a miss) ->
generate_patch -> [rerun log] -> draft/create PR or issue (human approval required).
"""
from core import config, extract, github, kb, locator, parsers, patcher, projects, store
from core.classifier import classify
from core.errors import AppError

MAX_ATTEMPTS = 2


# ── projects (thin wrappers so the web and MCP layers share one entry point) ──

def list_projects() -> list[dict]:
    return projects.list_projects()


def add_project(name: str, path: str = "", log_format: str = "auto", github_repo: str = "",
                domain_hint: str = "") -> dict:
    return projects.add(name, path, log_format, github_repo, domain_hint)


def remove_project(name: str) -> None:
    projects.remove(name)


# ── runs ─────────────────────────────────────────────────────────────

def _run_view(run: dict) -> dict:
    tests = [{k: v for k, v in t.items() if k != "log"} for t in run["tests"]]
    return {**run, "tests": tests}


def submit_log(project: str, log_text: str, name: str = "log") -> dict:
    """Parse a log into per-test results. Deterministic; never calls the LLM."""
    proj = projects.get(project) if project else {}
    if not isinstance(log_text, str):
        raise AppError("The log must be text.")
    if not log_text.strip():
        raise AppError("The log is empty.")
    if len(log_text) > config.MAX_LOG_CHARS:
        raise AppError("The log is too large.")
    parsed = parsers.parse(log_text, proj.get("log_format", "auto"), name)
    for test in parsed["tests"]:
        test["case_id"] = None
    run = {"id": store.new_id(), "project": project, "name": name[:120], "created": store.now(),
           "format": parsed["format"], "counts": parsed["counts"], "tests": parsed["tests"]}
    store.save("runs", run)
    return _run_view(run)


def get_run(run_id: str) -> dict:
    return _run_view(store.load("runs", run_id))


def list_runs(project: str = "") -> list[dict]:
    runs = store.list_all("runs")
    return [{k: r[k] for k in ("id", "project", "name", "created", "format", "counts")}
            for r in runs if not project or r["project"] == project]


# ── cases ────────────────────────────────────────────────────────────

def _root(case: dict):
    if not case.get("project"):
        return None
    try:
        root = projects.root_of(projects.get(case["project"]))
    except AppError:
        return None
    return root if root.is_dir() else None


def capabilities(case: dict) -> dict:
    cls = case.get("classification") or {}
    root = _root(case)
    target = cls.get("target_file") or ""
    target_ok = bool(root and target) and _is_file(root, target)
    confident = not cls.get("needs_info", True)
    rerun = case.get("rerun") or {}
    repo = projects.github_repo_of(_project_or_empty(case))
    return {
        "mode": "repo" if root else "generic",
        "can_patch": bool(target_ok and confident),
        "can_issue": bool((cls.get("category") == "product_bug" and confident)
                          or rerun.get("status") == "failed"),
        "can_rerun": bool(case.get("patch")),
        "github_configured": github.configured(repo),
        "github_repo": repo,
    }


def _is_file(root, rel: str) -> bool:
    try:
        path = (root / rel).resolve()
        path.relative_to(root.resolve())
        return path.is_file()
    except (ValueError, OSError):
        return False


def _project_or_empty(case: dict) -> dict:
    try:
        return projects.get(case["project"])
    except AppError:
        return {}


def _public(case: dict) -> dict:
    out = {k: v for k, v in case.items() if k not in ("excerpt", "patch", "kb_hints")}
    out["kb_hints"] = [{k: h[k] for k in ("id", "problem", "status", "score")} for h in case.get("kb_hints", [])]
    if case.get("patch"):
        out["patch"] = {"file": case["patch"]["file"], "diff": case["patch"]["diff"],
                        "from_kb": bool(case["patch"].get("from_kb"))}
    out["capabilities"] = capabilities(case)
    return out


def _classify(case: dict) -> None:
    case["classification"] = classify(case)
    cls = case["classification"]
    case["history"].append({"at": store.now(), "category": cls["category"],
                            "confidence": cls["confidence"], "source": case["source"],
                            "info_count": len(case["info"])})


def _replay_from_kb(case: dict, root, entry: dict) -> bool:
    """Apply a verified KB fix to the current code. True on success (no LLM call)."""
    try:
        case["patch"] = patcher.replay(root, entry["patch_file"], entry["patch_blocks"])
    except AppError:
        return False                       # code changed since the fix was recorded
    case["classification"] = {
        "category": entry["category"] or "test_bug", "confidence": 1.0, "needs_info": False,
        "summary": entry["problem"], "root_cause": entry["problem"], "evidence": [],
        "waiting_on": "", "suspected_component": "", "error_signature": case["signature"],
        "issue_title": "", "fix_suggestion": entry["fix_text"], "target_file": entry["patch_file"],
        "missing_info": [], "tokens": 0}
    case["source"] = "kb"
    case["kb_entry"] = entry["id"]
    kb.touch(entry["id"])
    case["history"].append({"at": store.now(), "category": case["classification"]["category"],
                            "confidence": 1.0, "source": "kb", "info_count": 0})
    return True


def analyse_failure(run_id: str, test_id: str) -> dict:
    """Analyse one failed test. Skipped/passed tests are rejected on purpose."""
    run = store.load("runs", run_id)
    test = next((t for t in run["tests"] if t["id"] == test_id), None)
    if not test:
        raise AppError("Test not found in this run.")
    if test["status"] != "failed":
        raise AppError(f"Only failed tests are analysed (this test is {test['status']}).")
    if test.get("case_id"):
        return get_case(test["case_id"])

    proj = projects.get(run["project"]) if run["project"] else {}
    root = projects.root_of(proj) if proj else None
    root = root if (root and root.is_dir()) else None
    log = test["log"] or test["reason"] or test["name"]
    frames, sources = locator.collect_sources(log, root)
    signature = kb.signature_of(log)
    hints = kb.search(run["project"], signature)
    case = {
        "id": store.new_id(), "project": run["project"], "run_id": run_id, "test_id": test_id,
        "name": test["name"], "created": store.now(), "format": run["format"],
        "domain_hint": proj.get("domain_hint", ""), "excerpt": extract.clip(log),
        "signature": signature, "frames": frames, "sources": sources, "kb_hints": hints,
        "kb_entry": None, "source": "llm", "info": [], "classification": None, "patch": None,
        "issue_draft": None, "pr_draft": None, "published": [], "history": [], "rerun": None}

    entry = kb.reusable(hints)
    if not (entry and root and _replay_from_kb(case, root, entry)):
        _classify(case)
    store.save("cases", case)
    test["case_id"] = case["id"]
    store.save("runs", run)
    return _public(case)


def get_case(case_id: str) -> dict:
    return _public(store.load("cases", case_id))


def provide_info(case_id: str, answer: str, question: str = "") -> dict:
    answer = (answer or "").strip()
    if not answer:
        raise AppError("Provide some information (text, log lines or command output).")
    case = store.load("cases", case_id)
    case["info"].append({"question": question[:300], "text": answer[:config.MAX_INFO_CHARS], "at": store.now()})
    root = _root(case)
    _, extra = locator.collect_sources(answer, root)
    known = {(s["file"], s["focus_line"]) for s in case["sources"]}
    case["sources"] += [s for s in extra if (s["file"], s["focus_line"]) not in known]
    case["patch"] = case["issue_draft"] = case["pr_draft"] = None
    case["source"] = "llm"
    _classify(case)
    store.save("cases", case)
    return _public(case)


def generate_patch(case_id: str, notes: str = "") -> dict:
    case = store.load("cases", case_id)
    if not capabilities(case)["can_patch"]:
        raise AppError("Patch generation needs a confident diagnosis and a target file inside the "
                       "project repository (generic mode has no patch generation).")
    case["patch"] = patcher.generate(_root(case), case["classification"]["target_file"],
                                     case["classification"], notes)
    case["pr_draft"] = None
    attempts = (case.get("rerun") or {}).get("attempts", 0)
    case["rerun"] = {"status": "pending", "attempts": attempts} if attempts else None
    store.save("cases", case)
    return _public(case)


# ── rerun (manual log today; Jenkins feeds the same function later) ──

def submit_rerun(case_id: str, log_text: str) -> dict:
    """Feed the log of a rerun with the patch applied. Passed -> verified; failed -> re-analyse."""
    case = store.load("cases", case_id)
    if not case.get("patch"):
        raise AppError("Generate a patch before submitting a rerun log.")
    proj = projects.get(case["project"])
    parsed = parsers.parse(log_text, proj.get("log_format", "auto"), case["name"])
    match = next((t for t in parsed["tests"] if t["id"] == case["test_id"]), None)
    if match is None and len(parsed["tests"]) == 1:
        match = parsed["tests"][0]           # single-result logs (generic format)
    if match is None:
        raise AppError("The rerun log does not contain this test.")
    attempts = ((case.get("rerun") or {}).get("attempts", 0)) + 1
    if match["status"] == "passed":
        case["rerun"] = {"status": "passed", "attempts": attempts}
        _save_kb(case, "verified")
    elif match["status"] == "skipped":
        raise AppError("The test was skipped in the rerun, so it proves nothing.")
    else:
        case["rerun"] = {"status": "failed", "attempts": attempts, "exhausted": attempts >= MAX_ATTEMPTS}
        case["info"].append({"question": "Rerun with the proposed patch failed",
                             "text": match["log"][:config.MAX_INFO_CHARS], "at": store.now()})
        case["patch"] = None
        case["source"] = "llm"
        _classify(case)
    store.save("cases", case)
    return _public(case)


# ── knowledge base ───────────────────────────────────────────────────

def _save_kb(case: dict, status: str, link: str = "") -> dict:
    cls = case["classification"]
    patch = case.get("patch") or {}
    return kb.record(case["project"], case["signature"], (cls["summary"] or case["signature"])[:200],
                     cls["category"], cls["fix_suggestion"], status, patch.get("file", ""),
                     patch.get("blocks"), link)


def save_to_kb(case_id: str) -> dict:
    case = store.load("cases", case_id)
    if not case.get("classification") or case["classification"]["needs_info"]:
        raise AppError("Only a confident diagnosis can be saved to the knowledge base.")
    verified = (case.get("rerun") or {}).get("status") == "passed"
    entry = _save_kb(case, "verified" if verified else "suggested")
    return {"entry": entry}


def kb_list(project: str = "") -> list[dict]:
    return kb.list_entries(project)


def kb_delete(entry_id: int) -> None:
    kb.delete(entry_id)


# ── issue / PR drafts and publishing ─────────────────────────────────

def draft_issue(case_id: str) -> dict:
    case = store.load("cases", case_id)
    if not capabilities(case)["can_issue"]:
        raise AppError("An issue can only be drafted for a confident product_bug or after a failed fix rerun.")
    cls = case["classification"]
    repo = projects.github_repo_of(_project_or_empty(case))
    duplicates = github.search_issues(repo, cls.get("error_signature") or case["signature"])
    evidence = "\n".join(f"- `{e}`" for e in cls["evidence"]) or "- (none captured)"
    answers = "\n".join(f"- {i['question'] or 'Additional info'}" for i in case["info"]) or "- none"
    tried = ""
    if (case.get("rerun") or {}).get("status") == "failed":
        tried = f"\n## Fix attempts\n{case['rerun']['attempts']} automated fix attempt(s) did not make the test pass.\n"
    body = (
        f"## Summary\n{cls['summary']}\n\n## Root cause (suspected)\n{cls['root_cause']}\n\n"
        f"## Evidence\n{evidence}\n\n## Component\n{cls['suspected_component'] or 'unknown'}\n\n"
        f"## Suggested next step\n{cls['fix_suggestion']}\n{tried}\n"
        f"## Failing test\n`{case['test_id']}` (project: {case['project']})\n\n"
        f"## Log excerpt\n```\n{case['excerpt'][:3500]}\n```\n\n"
        f"## Triage details\nConfidence: {cls['confidence']}; clarifications provided:\n{answers}\n\n"
        f"_Drafted by Log Analyser and approved by a human._")
    case["issue_draft"] = {"title": cls["issue_title"] or f"Test failure: {case['name']}: {cls['summary'][:70]}",
                           "body": body, "labels": ["bug", "triage"], "duplicates": duplicates}
    store.save("cases", case)
    return _public(case)


def draft_pr(case_id: str) -> dict:
    case = store.load("cases", case_id)
    if not case.get("patch"):
        raise AppError("Generate a patch first.")
    cls, patch = case["classification"], case["patch"]
    verified = (case.get("rerun") or {}).get("status") == "passed"
    case["pr_draft"] = {
        "title": f"Fix {case['name']}: {cls['summary'][:80]}",
        "body": (f"## Problem\n{cls['summary']}\n\n## Root cause\n{cls['root_cause']}\n\n"
                 f"## Verification\n{'Test passed on a rerun with this change.' if verified else 'NOT yet verified by a rerun.'}\n\n"
                 f"## Change\n`{patch['file']}`\n```diff\n{patch['diff'][:4000]}\n```\n\n"
                 f"_Generated by Log Analyser. Review before merging._"),
        "commit_message": f"Fix {patch['file']}: {cls['summary'][:60]}"}
    store.save("cases", case)
    return _public(case)


def update_draft(case_id: str, kind: str, title: str, body: str) -> dict:
    case = store.load("cases", case_id)
    key = {"issue": "issue_draft", "pr": "pr_draft"}.get(kind)
    if not key or not case.get(key):
        raise AppError("No such draft.")
    if (title or "").strip():
        case[key]["title"] = title.strip()[:250]
    if (body or "").strip():
        case[key]["body"] = body
    store.save("cases", case)
    return _public(case)


def create_issue(case_id: str, approved: bool) -> dict:
    if approved is not True:
        raise AppError("Creating an issue requires explicit approval (approved=true).")
    case = store.load("cases", case_id)
    draft = case.get("issue_draft")
    if not draft:
        raise AppError("Draft the issue first.")
    repo = projects.github_repo_of(_project_or_empty(case))
    result = github.create_issue(repo, draft["title"], draft["body"], draft["labels"])
    case["published"].append({"type": "issue", **result})
    _save_kb(case, "open_issue", result["url"])
    store.save("cases", case)
    return {"published": result, "case": _public(case)}


def create_pr(case_id: str, approved: bool) -> dict:
    if approved is not True:
        raise AppError("Creating a pull request requires explicit approval (approved=true).")
    case = store.load("cases", case_id)
    draft, patch = case.get("pr_draft"), case.get("patch")
    if not draft or not patch:
        raise AppError("Generate a patch and draft the pull request first.")
    repo = projects.github_repo_of(_project_or_empty(case))
    result = github.create_pull_request(repo, draft["title"], draft["body"], patch["file"],
                                        patch["new_content"], draft["commit_message"])
    case["published"].append({"type": "pull_request", **result})
    store.save("cases", case)
    return {"published": result, "case": _public(case)}
