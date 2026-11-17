"""Evidence-based failure classification with an interactive "need more info" loop."""
import json
import re

from core import config, llm
from core.errors import LLMError

CATEGORIES = ("test_bug", "product_bug", "env_infra", "flaky_timeout", "unknown")

_SYSTEM = """You are a senior engineer triaging a failed test/job/build log.

Categories:
- test_bug: defect in the application or test code that lives **inside this repository** — wrong assertion, bad setup, stale expectation, or a bug in the source code being tested (e.g. a method returns the wrong value, an algorithm is incorrect).
- product_bug: defect in an **external** product, upstream dependency, operator, or third-party service that this repository depends on but does not own (e.g. a Kubernetes operator crashes, an upstream library has a regression, a managed service returns unexpected errors).
- env_infra: environment problem (network, cluster, quota, credentials, missing dependency).
- flaky_timeout: the product is healthy but slow or racy. Use ONLY if evidence shows what was awaited and that it was progressing.
- unknown: evidence is insufficient.

Rules:
1. A timeout, retry exhaustion or "connection refused" is a SYMPTOM, not a cause. State what was being waited on (waiting_on) and why it may not have happened. Do NOT recommend increasing a timeout unless evidence shows the awaited operation was progressing normally.
2. Use product_bug ONLY when the defective code is an external dependency outside this repository. If the bug is in a source file that is part of this repository (visible in the source excerpt or stack trace under the project path), use test_bug instead.
3. Never invent facts. Evidence must be short exact quotes from the log or source.
4. If confidence would be below {threshold}, use category "unknown" and put specific, answerable requests in missing_info (which log, command output, config or version).
5. USER-PROVIDED INFORMATION is authoritative new evidence. Incorporate it, update the assessment, and never repeat a question that was already answered.
{domain}
Respond with ONLY one JSON object, no prose, with these keys:
{{"category": one of {categories}, "confidence": 0.0-1.0, "summary": "2-3 sentences",
 "root_cause": "what actually went wrong", "evidence": ["quote", ...],
 "waiting_on": "what a timeout was waiting for, or empty",
 "suspected_component": "component/module, or empty",
 "error_signature": "short distinctive error string for searching existing issues",
 "issue_title": "concise bug title, if product_bug",
 "fix_suggestion": "concrete next step; for code changes cite file:line",
 "target_file": "repo-relative path of the file to change, or empty",
 "missing_info": [{{"id": "q1", "question": "..."}}]}}"""


def _system(domain_hint: str = "") -> str:
    domain = f"\nDomain context: {domain_hint}\n" if domain_hint else ""
    return _SYSTEM.format(threshold=config.CONFIDENCE_THRESHOLD, domain=domain,
                          categories=list(CATEGORIES))


def build_prompt(case: dict) -> str:
    parts = [f"LOG FORMAT: {case['format']}", f"LOG EXCERPT:\n{case['excerpt']}"]
    if case.get("sources"):
        parts.append("SOURCE CODE (line numbers left of '|'):")
        for s in case["sources"]:
            parts.append(f"--- {s['file']} (focus line {s['focus_line']}) ---\n{s['text']}")
    else:
        parts.append("SOURCE CODE: not available (no matching file found in the source repository).")
    if case.get("kb_hints"):
        parts.append("SIMILAR PAST PROBLEMS (partial or unverified matches, hints only):\n" + "\n".join(
            f"- {h['problem']} => {h['fix_text'][:300]} (similarity {h['score']:.2f})"
            for h in case["kb_hints"]))
    if case.get("classification"):
        prev = {k: case["classification"].get(k) for k in ("category", "confidence", "summary", "missing_info")}
        parts.append("PREVIOUS ASSESSMENT:\n" + json.dumps(prev, indent=2))
    if case.get("info"):
        parts.append("USER-PROVIDED INFORMATION:")
        for i, item in enumerate(case["info"], 1):
            question = f" (answering: {item['question']})" if item.get("question") else ""
            parts.append(f"[{i}]{question}\n{item['text']}")
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(cleaned[start:end + 1])


def normalize(data: dict) -> dict:
    category = data.get("category") if data.get("category") in CATEGORIES else "unknown"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    waiting_on = str(data.get("waiting_on") or "").strip()
    if category == "flaky_timeout" and not waiting_on:
        confidence = min(confidence, 0.4)   # a timeout without a known cause is not a diagnosis

    missing = []
    for i, item in enumerate(data.get("missing_info") or [], 1):
        if isinstance(item, dict) and item.get("question"):
            missing.append({"id": str(item.get("id") or f"q{i}"), "question": str(item["question"])})
        elif isinstance(item, str) and item.strip():
            missing.append({"id": f"q{i}", "question": item.strip()})

    needs_info = category == "unknown" or confidence < config.CONFIDENCE_THRESHOLD
    if needs_info and not missing:
        missing.append({"id": "q1", "question": "Provide additional logs or command output from "
                        "around the failure (component logs, versions, config) so the cause can be confirmed."})
    return {
        "category": category,
        "confidence": round(confidence, 2),
        "summary": str(data.get("summary") or ""),
        "root_cause": str(data.get("root_cause") or ""),
        "evidence": [str(e) for e in (data.get("evidence") or [])][:8],
        "waiting_on": waiting_on,
        "suspected_component": str(data.get("suspected_component") or ""),
        "error_signature": str(data.get("error_signature") or ""),
        "issue_title": str(data.get("issue_title") or ""),
        "fix_suggestion": str(data.get("fix_suggestion") or ""),
        "target_file": str(data.get("target_file") or ""),
        "missing_info": missing,
        "needs_info": needs_info,
    }


def classify(case: dict) -> dict:
    messages = [
        {"role": "system", "content": _system(case.get("domain_hint", ""))},
        {"role": "user", "content": build_prompt(case)},
    ]
    last_error = None
    for attempt in range(2):
        result = llm.chat(messages, max_tokens=1500, temperature=0.1)
        try:
            classification = normalize(_extract_json(result["content"]))
            classification["tokens"] = result["prompt_tokens"] + result["completion_tokens"]
            return classification
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            messages.append({"role": "assistant", "content": result["content"]})
            messages.append({"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."})
    raise LLMError(f"Model did not return valid JSON: {last_error}")
