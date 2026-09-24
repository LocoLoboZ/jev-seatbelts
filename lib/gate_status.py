#!/usr/bin/env python
"""P10: answer "which gates are currently enabled" without reading
hooks/hooks.json by hand (reference/DESIGN-BASIS.md, "Pipeline-level
gaps", P10: "Nothing in the repo answers 'which gates are currently
enabled' ... A status command closes this.").

Read-only. Makes no Jev call, judges nothing, changes nothing - a report,
not a gate. With --session, also surfaces P11's aggregate reachability
signal and P6's session call budget for that one session, since P11's own
design note named this command as the natural place to show that signal
to a human instead of reading its JSON state file directly.

    python lib/gate_status.py                        # offline self-check
    python lib/gate_status.py --status [--session <id>]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import jevgate  # noqa: E402

# (label, description, the env var hooks.json's own docstring names as
# that gate's enable switch) - kept as one literal list, not derived from
# hooks.json, because hooks.json has no per-gate enable-var field to
# derive it from; each gate's own script is the source of truth for its
# ENABLE_VAR and this list is checked against that by selfcheck() below.
GATES = [
    ("Gate 1", "plan gate", "GATE1_ENABLED"),
    ("Gate 2", "package check", "GATE2_ENABLED"),
    ("Gate 3", "command safety", "GATE3_ENABLED"),
    ("Gate 4", "commit screening", "GATE4_ENABLED"),
    ("Gate 5", "debug triage", "GATE5_ENABLED"),
    ("Gate 6", "code quality", "GATE6_ENABLED"),
    ("Gate 7", "completion check", "GATE7_ENABLED"),
    ("P8", "drift guard", "DRIFTGUARD_ENABLED"),
]
ON_VALUES = ("1", "true", "yes", "on")


def _on(var):
    return (os.environ.get(var) or "").strip().lower() in ON_VALUES


def gate_lines():
    return [
        f"{label:7} {name:18} {'on' if _on(var) else 'off':<3} ({var})"
        for label, name, var in GATES]


def session_lines(session_id):
    """P11 reachability + P6 budget for one session. Empty if none given -
    this command's core answer (which gates are on) needs no session."""
    if not session_id:
        return []
    lines = [f"\nsession {session_id}:"]
    unreachable = jevgate.jev_unreachable(session_id)
    if unreachable is None:
        lines.append("  jev reachability: reachable, or no real call yet")
    else:
        lines.append(
            f"  jev reachability: UNREACHABLE - last failure "
            f"{unreachable['ts']} on gate {unreachable['gate']}: "
            f"{unreachable['reason']}")
    lines.append(
        f"  session calls used: {jevgate.session_calls_used(session_id)} "
        f"/ {jevgate.SESSION_CALL_CAP}")
    return lines


def status(session_id=None):
    return "\n".join(gate_lines() + session_lines(session_id))


def main(argv):
    if not argv or "--selfcheck" in argv:
        return selfcheck()
    session_id = None
    if "--session" in argv:
        i = argv.index("--session")
        session_id = argv[i + 1] if i + 1 < len(argv) else None
    print(status(session_id))
    return 0


# One source file per gate, for the drift check below - kept next to
# GATES rather than derived from it, since GATES pairs a var with a
# human label, not a file path.
_GATE_SOURCE_FILES = [
    "skills/plan-gate/scripts/plan_gate.py",
    "skills/package-check/scripts/package_check.py",
    "skills/command-safety/scripts/command_safety.py",
    "skills/commit-screening/scripts/commit_screening.py",
    "skills/completion-check/scripts/completion_check.py",
    "skills/debug-triage/scripts/hypothesis_ranker.py",
    "skills/code-quality/scripts/code_quality.py",
    "lib/driftcheck_hook.py",
]


def _enable_var_of(path):
    """The literal string assigned to ENABLE_VAR in a gate's own source
    file, read via ast.parse rather than import - this status tool
    reports on gates, it does not need to run them, and importing four
    other modules just to read one class attribute off each would
    execute every one of their top-level statements for no reason (an
    independent review of this file, 2026-09-23, named this exact
    concern in its design note)."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "ENABLE_VAR"
                        for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    return None


# --- offline self-check -------------------------------------------------

def selfcheck():
    import io
    import unittest.mock

    # Every gate this lists must actually be switched by the env var
    # named here - checked by statically parsing each gate script's own
    # ENABLE_VAR assignment, so this list cannot silently drift from what
    # the gates themselves use.
    root = os.path.dirname(HERE)
    real_vars = {_enable_var_of(os.path.join(root, f))
                 for f in _GATE_SOURCE_FILES}
    assert None not in real_vars, real_vars
    listed_vars = {var for _label, _name, var in GATES}
    assert listed_vars == real_vars, (listed_vars, real_vars)

    # --status with every gate off names each one "off".
    clean_env = {var: "" for _l, _n, var in GATES}
    with unittest.mock.patch.dict(os.environ, clean_env, clear=False):
        lines = gate_lines()
    assert len(lines) == len(GATES)
    for line in lines:
        assert " off " in line, line

    # --status with one gate on names only that one "on".
    with unittest.mock.patch.dict(
            os.environ, {**clean_env, "GATE3_ENABLED": "1"}, clear=False):
        lines = gate_lines()
    on_lines = [l for l in lines if " on  " in l]
    assert len(on_lines) == 1 and "Gate 3" in on_lines[0], lines

    # No --session: no session block at all.
    assert session_lines(None) == []

    # --session with no history yet: reachable-or-unknown, not a false
    # "unreachable" claim - matches jev_unreachable()'s own contract.
    with unittest.mock.patch.dict(
            os.environ, {"JEV_SESSION_STATE_DIR": "/nonexistent/p10-test",
                        "JEV_SESSION_CALLS_DIR": "/nonexistent/p10-test2"}):
        lines = session_lines("p10-selfcheck-session")
    joined = "\n".join(lines)
    assert "reachable, or no real call yet" in joined, joined
    assert "0 / " in joined, joined

    # --session after a marked failure: shows the gate and a redacted
    # reason, never the raw secret-shaped string passed in.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(
                os.environ, {"JEV_SESSION_STATE_DIR": td}):
            jevgate.mark_jev_unreachable(
                "p10-selfcheck-session", 4,
                "HTTPError 401 tsk-p10-selfcheck-secret")
            lines = session_lines("p10-selfcheck-session")
    joined = "\n".join(lines)
    assert "UNREACHABLE" in joined and "gate 4" in joined, joined
    assert "tsk-p10-selfcheck-secret" not in joined, joined
    assert "<redacted>" in joined, joined

    # main() with --session forwards the id through to the printed report.
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(
                os.environ, {"JEV_SESSION_STATE_DIR": td}), \
            unittest.mock.patch.object(sys, "stdout", out):
            r = main(["--status", "--session", "p10-cli-test"])
    assert r == 0, r
    assert "session p10-cli-test" in out.getvalue(), out.getvalue()

    print("gate_status selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
