import pytest

from core import parsers
from core.errors import AppError

OCS_STYLE = """\
tests/functional/x/test_a.py::TestA::test_pass[rbd-power off] 
-------------------------------- live log setup --------------------------------
PASSED
tests/functional/x/test_a.py::TestA::test_skip
SKIPPED (Test runs ONLY on IBM cloud)
tests/functional/x/test_a.py::TestA::test_fail
FAILED
____________________________ TestA.test_fail ____________________________
>   assert False
E   AssertionError: boom
tests/functional/x/test_b.py::test_setup_err
___ ERROR at setup of test_setup_err ___
E   RuntimeError: no cluster
=========================== short test summary info ============================
FAILED tests/functional/x/test_a.py::TestA::test_fail - AssertionError: boom
ERROR tests/functional/x/test_b.py::test_setup_err
= 1 failed, 1 passed, 1 skipped, 1 error in 5.0s =
"""


def by_id(result):
    return {t["id"]: t for t in result["tests"]}


def test_pytest_ocs_style_statuses_and_params_with_spaces():
    r = parsers.parse(OCS_STYLE)
    tests = by_id(r)
    assert r["format"] == "pytest"
    assert tests["tests/functional/x/test_a.py::TestA::test_pass[rbd-power off]"]["status"] == "passed"
    assert tests["tests/functional/x/test_a.py::TestA::test_skip"]["status"] == "skipped"
    assert tests["tests/functional/x/test_a.py::TestA::test_skip"]["reason"] == "Test runs ONLY on IBM cloud"
    assert tests["tests/functional/x/test_a.py::TestA::test_fail"]["status"] == "failed"
    assert "AssertionError: boom" in tests["tests/functional/x/test_a.py::TestA::test_fail"]["log"]
    err = tests["tests/functional/x/test_b.py::test_setup_err"]
    assert err["status"] == "failed" and err["kind"] == "error"
    assert r["counts"]["failed"] == 2 and r["counts"]["passed"] == 1 and r["counts"]["skipped"] == 1


def test_pytest_verbose_format_and_xfail():
    text = ("a.py::test_x PASSED   [ 50%]\n"
            "a.py::test_y XFAIL (known)  [100%]\n"
            "a.py::test_z XPASS\n")
    r = parsers.parse(text, "pytest")
    tests = by_id(r)
    assert tests["a.py::test_x"]["status"] == "passed"
    assert tests["a.py::test_y"]["status"] == "skipped"
    assert tests["a.py::test_z"]["status"] == "passed"


def test_pytest_failed_test_never_downgraded_by_later_pass():
    r = parsers.parse("a.py::t FAILED\na.py::t PASSED\n", "pytest")
    assert r["tests"][0]["status"] == "failed"


def test_junit_xml_failure_error_skipped_passed():
    xml = """<testsuite><testcase classname="c.A" name="ok" time="0.1"/>
    <testcase classname="c.A" name="sk"><skipped message="why"/></testcase>
    <testcase classname="c.A" name="bad"><failure message="boom">trace</failure></testcase>
    <testcase classname="c.A" name="err"><error message="npe">trace2</error><system-err>stderr text</system-err></testcase>
    </testsuite>"""
    r = parsers.parse(xml)
    tests = by_id(r)
    assert r["format"] == "junit"
    assert [tests[f"c.A.{n}"]["status"] for n in ("ok", "sk", "bad", "err")] == ["passed", "skipped", "failed", "failed"]
    assert tests["c.A.err"]["kind"] == "error" and "stderr text" in tests["c.A.err"]["log"]
    assert tests["c.A.sk"]["reason"] == "why"


def test_junit_rejects_entities_and_bad_xml():
    with pytest.raises(AppError):
        parsers.parse('<!DOCTYPE x [<!ENTITY a "b">]><testsuite><testcase name="n"/></testsuite>', "junit")
    with pytest.raises(AppError):
        parsers.parse("<testsuite><testcase", "junit")


def test_maven_text_format_a_and_b_and_totals():
    text = ("[INFO] Tests run: 5, Failures: 1, Errors: 1, Skipped: 1\n"
            "[ERROR] processBatch(com.acme.JobRunnerIT)  Time elapsed: 30.2 s  <<< ERROR!\n"
            "java.lang.NullPointerException: payload is null\n"
            "\tat com.acme.Worker.handle(Worker.java:17)\n"
            "[ERROR] com.acme.OtherIT.checks -- Time elapsed: 0.5 s <<< FAILURE!\n"
            "java.lang.AssertionError: expected 1\n"
            "[INFO] Tests run: 5, Failures: 1, Errors: 1, Skipped: 1\n")
    r = parsers.parse(text)
    assert r["format"] == "maven"
    tests = by_id(r)
    assert tests["com.acme.JobRunnerIT.processBatch"]["kind"] == "error"
    assert "NullPointerException" in tests["com.acme.JobRunnerIT.processBatch"]["log"]
    assert tests["com.acme.OtherIT.checks"]["status"] == "failed"
    assert r["counts"]["passed"] == 2 and r["counts"]["skipped"] == 1


def test_go_pass_fail_skip_and_panic():
    text = ("=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n    b_test.go:5: got 1 want 2\n"
            "--- FAIL: TestB (0.01s)\n=== RUN   TestC\n--- SKIP: TestC (0.00s)\nFAIL\n")
    r = parsers.parse(text)
    tests = by_id(r)
    assert r["format"] == "go"
    assert (tests["TestA"]["status"], tests["TestB"]["status"], tests["TestC"]["status"]) == ("passed", "failed", "skipped")
    assert "got 1 want 2" in tests["TestB"]["log"] and tests["TestB"]["reason"].startswith("b_test.go")


def test_generic_verdict_pass_and_fail():
    ok = parsers.parse("all good\nfinished", name="build.log")
    assert ok["format"] == "generic" and ok["tests"][0]["status"] == "passed"
    bad = parsers.parse("step 1\nTraceback (most recent call last):\n  boom\n", name="build.log")
    assert bad["tests"][0]["status"] == "failed" and "Traceback" in bad["tests"][0]["log"]
    shell = parsers.parse("./deploy.sh: line 14: rollout verification failed", name="d")
    assert shell["tests"][0]["status"] == "failed"


def test_forced_format_falls_back_to_generic_when_nothing_found():
    r = parsers.parse("nothing parseable here", "pytest", "x")
    assert r["format"] == "generic" and len(r["tests"]) == 1


def test_counts_and_errors():
    assert parsers.parse(OCS_STYLE)["counts"]["total"] == 4
    with pytest.raises(AppError):
        parsers.parse("   ")
    with pytest.raises(AppError):
        parsers.parse("x", "nope")


def test_detect_format():
    assert parsers.detect_format('<testsuite><testcase name="a"/></testsuite>') == "junit"
    assert parsers.detect_format("a.py::t PASSED") == "pytest"
    assert parsers.detect_format("=== RUN TestX") == "go"
    assert parsers.detect_format("Tests run: 1, Failures: 0") == "maven"
    assert parsers.detect_format("hello") == "generic"
