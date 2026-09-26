#!/usr/bin/env python
"""Gate 5 stall check: warn when edits stop moving a failing test.

    python stall_check.py              # as the hook, stdin: PostToolUse JSON
    python stall_check.py --selfcheck  # offline, no network, no Jev call

WHAT THIS DOES. The guessed-fix loop has one observable signature: the
agent edits, re-runs the same test command, and gets the same failure,
again and again. This hook fingerprints the tail of each failing
test-like Bash run (digits and hex masked, so timings and line numbers
do not count as change) and counts how many times that same fingerprint
comes back with an Edit/Write in between. At STALL_AT it tells the agent,
via additionalContext, to stop guessing and debug. It never blocks and
never calls Jev: the evidence is exact, so a model adds nothing but cost.

A re-run with no edit in between is not a guess and does not count. A
different failure, or a pass, resets the count.

Idea from awlevin/typesafe-computer-use (runner.py, MIT): a state
signature plus a count of actions already tried on that same state, so a
loop that changes nothing is stopped by a fact, not a judgement.
Re-derived here, stdlib only.

Inert until GATE5_STALL_ENABLED is set.
"""
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
import jevgate  # noqa: E402

GATE = "5-stall"
ENABLE_VAR = "GATE5_STALL_ENABLED"
ON_VALUES = ("1", "true", "yes", "on")
STALL_DIR = "~/.jev-gates/stall"
STALL_AT = 3
TAIL_LINES = 40
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|"
    r"go\s+test|cargo\s+test|dotnet\s+test|mvn\s+\S*\s*test|gradlew?\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|make\s+test|ctest)\b|"
    r"--selfcheck\b|\beval_\w+\.py\b")
FAIL_RE = re.compile(
    r"\b(FAIL(?:ED|URES?)?|ERROR|Traceback|AssertionError|panicked)\b|"
    r"\b[1-9]\d* (?:failed|failing|errors?)\b")
_MASK_RE = re.compile(r"0x[0-9a-fA-F]+|\d+(?:\.\d+)?")


def enabled():
    return (os.environ.get(ENABLE_VAR) or "").strip().lower() in ON_VALUES


def fingerprint(output):
    """Hash of the last TAIL_LINES non-blank lines, digits/hex masked."""
    lines = [_MASK_RE.sub("#", ln.strip())
             for ln in output.splitlines() if ln.strip()]
    tail = "\n".join(lines[-TAIL_LINES:])
    return hashlib.sha256(tail.encode("utf-8")).hexdigest()[:16]


def _state_path(session_id):
    root = os.path.expanduser(os.environ.get("JEV_STALL_DIR") or STALL_DIR)
    return os.path.join(root, jevgate._session_slug(session_id) + ".json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def step(state, tool, tool_input, tool_response):
    """Pure update: (new_state, repeat_count or None). A count is returned
    only for a failing test run. The caller warns at STALL_AT or above."""
    state = dict(state)
    if tool in EDIT_TOOLS:
        state["edited"] = True
        return state, None
    if tool != "Bash":
        return state, None
    cmd = " ".join(str((tool_input or {}).get("command") or "").split())
    if not TEST_CMD_RE.search(cmd):
        return state, None
    resp = tool_response if isinstance(tool_response, dict) else {}
    output = f"{resp.get('stdout') or ''}\n{resp.get('stderr') or ''}"
    runs = dict(state.get("runs") or {})
    edited = bool(state.get("edited"))
    state["edited"] = False
    if not FAIL_RE.search(output):
        runs.pop(cmd, None)
        state["runs"] = runs
        return state, None
    fp = fingerprint(output)
    prev = runs.get(cmd) or {}
    if prev.get("fp") != fp:
        count = 1
    else:
        count = int(prev.get("count") or 1) + (1 if edited else 0)
    runs[cmd] = {"fp": fp, "count": count}
    state["runs"] = runs
    return state, count


def message(count):
    return (f"Stall check (Gate 5): the same test failure has now come back "
            f"{count} times with code edited in between each run. The edits "
            f"are not moving it. Stop guessing at fixes. Re-read the full "
            f"error, form one explicit hypothesis about the cause and test "
            f"it (debug-triage can rank candidates), or tell the operator "
            f"what you have tried and ask.")


def main():
    if not enabled():
        return 0
    hook = json.load(sys.stdin)
    if not isinstance(hook, dict):
        return 0
    path = _state_path(hook.get("session_id"))
    state, count = step(_load(path), hook.get("tool_name"),
                        hook.get("tool_input"), hook.get("tool_response"))
    jevgate._write_session_state(path, state)
    if count is not None and count >= STALL_AT:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message(count)}}))
    return 0


# --- offline self-check -------------------------------------------------

def selfcheck():
    fail = {"stdout": "test_x FAILED\nAssertionError: 1 != 2 (0.12s)"}
    fail_moved = {"stdout": "test_x FAILED\nKeyError: 'y' (0.31s)"}
    ok = {"stdout": "3 passed in 0.10s"}
    run = {"command": "python -m pytest tests/test_x.py"}

    def play(events):
        state, last = {}, None
        for tool, inp, resp in events:
            state, c = step(state, tool, inp, resp)
            if c is not None:
                last = c
        return last

    edit = ("Edit", {"file_path": "a.py"}, None)
    test = ("Bash", run, fail)
    # Three identical failures, each after an edit: the third one warns.
    assert play([test, edit, test, edit, test]) == 3
    # Same failure re-run with no edit in between is not a guess.
    assert play([test, test, test]) == 1
    # A changed failure resets, and a pass clears.
    assert play([test, edit, test, edit, ("Bash", run, fail_moved)]) == 1
    assert play([test, edit, test, edit, ("Bash", run, ok), edit, test]) == 1
    # Timings and line numbers alone are not progress.
    assert fingerprint("E line 12 took 0.5s") == fingerprint("E line 99 took 3s")
    # Non-test commands and passing runs never count.
    assert step({}, "Bash", {"command": "ls -la"}, fail)[1] is None
    assert step({}, "Bash", run, ok)[1] is None
    assert step({}, "Bash", run, {"stdout": "5 passed, 0 failed"})[1] is None
    assert step({}, "Read", {}, None)[1] is None
    # Garbage input never raises.
    assert step({}, "Bash", None, "not a dict")[1] is None

    # End to end through main(): off is silent, on warns at STALL_AT.
    import io
    import tempfile
    import unittest.mock
    tmp = tempfile.mkdtemp(prefix="jev-stall-")

    def hook(tool, inp, resp):
        payload = json.dumps({"session_id": "stall-selfcheck",
                              "tool_name": tool, "tool_input": inp,
                              "tool_response": resp})
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                unittest.mock.patch.object(sys, "stdout", out):
            assert main() == 0
        return out.getvalue()

    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "0",
                                               "JEV_STALL_DIR": tmp}):
        assert hook("Bash", run, fail) == ""
        assert not os.listdir(tmp)
    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "1",
                                               "JEV_STALL_DIR": tmp}):
        outs = [hook(*e) for e in (test, edit, test, edit, test)]
    assert outs[:4] == ["", "", "", ""], outs
    got = json.loads(outs[4])["hookSpecificOutput"]
    assert got["hookEventName"] == "PostToolUse"
    assert "3 times" in got["additionalContext"]

    print("gate 5 stall_check selfcheck: ok")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001  a hook must never stop the work
        try:
            jevgate.hook_error(GATE, f"unhandled: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
