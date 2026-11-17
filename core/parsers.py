"""Deterministic per-test result parsing (no LLM involved).

Every test ends up as `passed`, `failed` or `skipped`. Errors (setup/teardown/collection) are
reported as `failed` with kind="error". Only failed tests are analysed later; skipped and passed
tests are just reported.
"""
import re
import xml.etree.ElementTree as ET

from core.errors import AppError
from core.extract import build_excerpt, clip

FORMATS = ("pytest", "junit", "maven", "go", "generic")

# pytest node id; parametrization brackets may contain spaces, e.g. test_x[rbd-power off]
_NODE = r"[\w./\\-]+\.py::[^\s\[]+(?:\[[^\]]*\])?"
_PY_VERBOSE = re.compile(rf"^({_NODE})\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b(.*)$")
_PY_NODE_ONLY = re.compile(rf"^({_NODE})\s*$")
_PY_BARE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(\s+\(.*\))?\s*(\[\s*\d+%\])?\s*$")
_PY_SUMMARY = re.compile(rf"^(FAILED|ERROR)\s+({_NODE})(?:\s+-\s+(.*))?$")
_PY_SECTION = re.compile(r"^_{3,} (.+?) _{3,}\s*$")
_PY_RULE = re.compile(r"^={3,} .* ={3,}\s*$")
_PY_TOTALS = re.compile(r"(\d+) (failed|passed|skipped|errors?|xfailed|xpassed)")

_MVN_A = re.compile(r"^\[(?:ERROR|WARNING|INFO)\]\s+([\w$]+)\(([\w.$]+)\)\s+Time elapsed:\s*([\d.]+)\s*s?\s+<<<\s+(FAILURE|ERROR|SKIPPED)!")
_MVN_B = re.compile(r"^\[(?:ERROR|WARNING|INFO)\]\s+([\w.$]+)\s+--\s+Time elapsed:\s*([\d.]+)\s*s\s+<<<\s+(FAILURE|ERROR|SKIPPED)!")
_MVN_RESULT = re.compile(r"^\[ERROR\]\s{2,}([\w$.]+\.[\w$]+)(?::\d+)?\s+(.*)$")
_MVN_TOTALS = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")

_GO_RESULT = re.compile(r"^(\s*)--- (PASS|FAIL|SKIP): (\S+) \(([\d.]+)s\)")
_GO_RUN = re.compile(r"^\s*=== (?:RUN|CONT)\s+(\S+)")

_GENERIC_FAIL = re.compile(
    r"Traceback \(most recent call last\)|\bBUILD FAILURE\b|^panic:|^\s*(?:error|fatal|ERROR|FATAL)\b[:\s]"
    r"|\bFAILED\b|\bFAILURE\b|Exception in thread|^\s*Caused by:"
    r"|\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s+[1-9]|\.(?:sh|bash): line \d+:",
    re.MULTILINE)


def _test(tid: str, name: str, status: str, kind: str = "", duration: str = "",
          reason: str = "", log: str = "") -> dict:
    return {"id": tid, "name": name, "status": status,
            "kind": kind or ("failure" if status == "failed" else ""),
            "duration": duration, "reason": reason[:300], "log": log if status == "failed" else ""}


def detect_format(text: str) -> str:
    head = text[:300_000]
    if head.lstrip().startswith("<") and re.search(r"<testsuites?\b|<testcase\b", head):
        return "junit"
    if (re.search(r"^[\w./\\-]+\.py::\S+", head, re.M)
            or re.search(r"=+ (test session starts|short test summary info|FAILURES) =+", head)):
        return "pytest"
    if re.search(r"^\s*(=== RUN|--- (PASS|FAIL|SKIP):)", head, re.M):
        return "go"
    if re.search(r"Tests run:.*Failures:|<<< (FAILURE|ERROR)!", head):
        return "maven"
    return "generic"


# ── pytest ───────────────────────────────────────────────────────────

def _py_key(node: str) -> str:
    return ".".join(node.split("::")[1:])


def _py_reason(rest: str) -> str:
    m = re.search(r"\((.*)\)", rest or "")
    return m.group(1) if m else ""


def _parse_pytest(text: str, _name: str) -> tuple[list[dict], dict]:
    lines = text.splitlines()
    results: dict[str, dict] = {}
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    current = None
    in_summary = False
    sections: dict[str, list[str]] = {}
    section_key = None
    totals: dict[str, int] = {}

    def set_status(node: str, status: str, reason: str = "") -> None:
        status = {"XFAIL": "SKIPPED", "XPASS": "PASSED"}.get(status, status)
        old = results.get(node)
        if old and old["status"] == "failed" and status == "PASSED":
            return
        mapped = {"PASSED": "passed", "SKIPPED": "skipped"}.get(status, "failed")
        results[node] = {"status": mapped, "kind": "error" if status == "ERROR" else "failure",
                         "reason": reason or (old or {}).get("reason", "")}

    for i, line in enumerate(lines):
        if re.match(r"=+ short test summary info =+", line):
            in_summary = True
        m = _PY_VERBOSE.match(line)
        if m:
            set_status(m.group(1), m.group(2), _py_reason(m.group(3)))
            continue
        if in_summary:
            m = _PY_SUMMARY.match(line)
            if m:
                set_status(m.group(2), m.group(1), m.group(3) or "")
                continue
        m = _PY_NODE_ONLY.match(line)
        if m:
            current = m.group(1)
            starts.setdefault(current, i)
            continue
        m = _PY_BARE.match(line)
        if m and current:
            set_status(current, m.group(1), _py_reason(m.group(2)))
            ends[current] = i
            continue
        m = _PY_SECTION.match(line)
        if m:
            section_key = re.sub(r"^ERROR at (?:setup|teardown) of ", "", m.group(1))
            sections.setdefault(section_key, []).append(line)
            continue
        if _PY_RULE.match(line):
            section_key = None
            if re.search(r"\d+ (failed|passed|skipped|errors?)", line):
                for count, word in _PY_TOTALS.findall(line):
                    key = "failed" if word.startswith("error") or word == "failed" else \
                        "passed" if word in ("passed", "xpassed") else "skipped"
                    totals[key] = totals.get(key, 0) + int(count)
            continue
        if section_key is not None:
            sections[section_key].append(line)

    tests = []
    for node, res in results.items():
        log = ""
        if res["status"] == "failed":
            key = _py_key(node)
            body = "\n".join("\n".join(v) for k, v in sections.items()
                             if key and (k == key or key.endswith("." + k) or k.endswith("." + key)))
            if not body and node in starts:
                segment = lines[starts[node]:ends.get(node, starts[node] + 400) + 1]
                body = build_excerpt("\n".join(segment), "generic")
            log = clip(body or res["reason"])
        tests.append(_test(node, node.split("::")[-1], res["status"], res["kind"] if res["status"] == "failed" else "",
                           reason=res["reason"], log=log))
    return tests, totals


# ── JUnit XML ────────────────────────────────────────────────────────

def _parse_junit(text: str, _name: str) -> tuple[list[dict], dict]:
    if re.search(r"<!DOCTYPE|<!ENTITY", text):
        raise AppError("XML with DOCTYPE/ENTITY declarations is not accepted.")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise AppError(f"Invalid JUnit XML: {exc}") from exc
    tests = []
    for case in root.iter("testcase"):
        cls, name = case.get("classname", ""), case.get("name", "")
        tid = f"{cls}.{name}" if cls else name
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        node = failure if failure is not None else error
        if node is not None:
            body = "\n".join(p for p in (node.get("message", ""), node.text or "") if p)
            err_out = case.findtext("system-err") or ""
            tests.append(_test(tid, name, "failed", "error" if failure is None else "failure",
                               case.get("time", ""), node.get("message", ""), clip(body + ("\n" + err_out if err_out else ""))))
        elif skipped is not None:
            tests.append(_test(tid, name, "skipped", duration=case.get("time", ""), reason=skipped.get("message", "")))
        else:
            tests.append(_test(tid, name, "passed", duration=case.get("time", "")))
    return tests, {}


# ── Maven surefire / failsafe text ───────────────────────────────────

def _parse_maven(text: str, _name: str) -> tuple[list[dict], dict]:
    lines = text.splitlines()
    tests: list[dict] = []
    totals: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = _MVN_A.match(line)
        if m:
            tid, dur, outcome = f"{m.group(2)}.{m.group(1)}", m.group(3), m.group(4)
            name = m.group(1)
        else:
            m = _MVN_B.match(line)
            if not m:
                continue
            tid, dur, outcome = m.group(1), m.group(2), m.group(3)
            name = tid.rsplit(".", 1)[-1]
        if outcome == "SKIPPED":
            tests.append(_test(tid, name, "skipped", duration=dur))
            continue
        body = [line]
        for nxt in lines[i + 1:i + 60]:
            if nxt.startswith("[") or not nxt.strip():
                break
            body.append(nxt)
        tests.append(_test(tid, name, "failed", "error" if outcome == "ERROR" else "failure", dur,
                           body[1].strip() if len(body) > 1 else "", clip("\n".join(body))))
    known = [t["id"] for t in tests]
    for line in lines:
        m = _MVN_RESULT.match(line)
        if m and not any(k.endswith(m.group(1)) or m.group(1).endswith(k) for k in known):
            tests.append(_test(m.group(1), m.group(1).rsplit(".", 1)[-1], "failed", reason=m.group(2), log=line))
            known.append(m.group(1))
    summary = [m for line in lines if "Time elapsed" not in line and not re.search(r"\sin\s+[\w.$]+\s*$", line)
               for m in [_MVN_TOTALS.search(line)] if m]
    if summary:
        run, fail, err, skip = (int(x) for x in summary[-1].groups())
        totals = {"passed": max(0, run - fail - err - skip), "skipped": skip}
    return tests, totals


# ── Go test ──────────────────────────────────────────────────────────

def _parse_go(text: str, _name: str) -> tuple[list[dict], dict]:
    lines = text.splitlines()
    run_start: dict[str, int] = {}
    tests = []
    for i, line in enumerate(lines):
        m = _GO_RUN.match(line)
        if m:
            run_start.setdefault(m.group(1), i)
            continue
        m = _GO_RESULT.match(line)
        if not m:
            continue
        indent, outcome, name, dur = len(m.group(1)), m.group(2), m.group(3), m.group(4)
        if outcome == "PASS":
            tests.append(_test(name, name, "passed", duration=dur))
        elif outcome == "SKIP":
            tests.append(_test(name, name, "skipped", duration=dur))
        else:
            start = run_start.get(name, max(0, i - 20))
            body = lines[start:i + 1]
            for nxt in lines[i + 1:i + 31]:
                if nxt.startswith(("panic:", "goroutine", "\t")) or (len(nxt) - len(nxt.lstrip())) > indent:
                    body.append(nxt)
                else:
                    break
            reason = next((b.strip() for b in body if b.strip() and not b.strip().startswith(("===", "---"))), "")
            tests.append(_test(name, name, "failed", duration=dur, reason=reason, log=clip("\n".join(body))))
    return tests, {}


# ── generic (any other log) ──────────────────────────────────────────

def _parse_generic(text: str, name: str) -> tuple[list[dict], dict]:
    label = (name or "log").strip() or "log"
    if _GENERIC_FAIL.search(text):
        return [_test(label, label, "failed", log=build_excerpt(text, "generic"))], {}
    return [_test(label, label, "passed")], {}


_PARSERS = {"pytest": _parse_pytest, "junit": _parse_junit, "maven": _parse_maven,
            "go": _parse_go, "generic": _parse_generic}


def parse(text: str, fmt: str = "auto", name: str = "log") -> dict:
    """Return {format, tests, counts}. Unknown/empty parses fall back to the generic parser."""
    if not text or not text.strip():
        raise AppError("The log is empty.")
    chosen = detect_format(text) if fmt in ("", "auto", None) else fmt
    if chosen not in _PARSERS:
        raise AppError(f"Unknown log format '{fmt}'.")
    tests, totals = _PARSERS[chosen](text, name)
    if not tests and chosen != "generic":
        chosen = "generic"
        tests, totals = _parse_generic(text, name)
    counts = {s: sum(1 for t in tests if t["status"] == s) for s in ("passed", "failed", "skipped")}
    counts["passed"] = max(counts["passed"], totals.get("passed", 0))
    counts["skipped"] = max(counts["skipped"], totals.get("skipped", 0))
    counts["total"] = counts["passed"] + counts["failed"] + counts["skipped"]
    return {"format": chosen, "tests": tests, "counts": counts}
