#!/usr/bin/env python
"""P8: a pipeline-level model-drift signal, once per session.

Per-gate baselines only catch drift a gate's own eval suite happens to
re-run. Nothing catches a Jev model-version bump moving every gate's
underlying judgment at once, mid-session, while nobody is running an eval
at all (reference/DESIGN-BASIS.md, "Pipeline-level gaps", P8:
"Pipeline-level model drift check - a Jev version bump moving all seven
gates at once, not caught by any per-gate baseline"). `driftguard.check()`
(lib/driftguard.py) is the primitive already built for exactly that
question - "when Jev answers, do the answers still look right" - reviewed
and correct, but deliberately left unwired: "driftguard.check() is still
not wired into any gate. Deciding what a gate does with degraded vs
unknown... is real design work, deliberately left open until a session
with room to do it properly."

This is that wiring, decided as follows. **A SessionStart hook, not a
per-tool-call one.** driftguard's own probes are deliberately uncalibrated
against any one gate's real edge cases - they only separate "obviously one
thing" from "obviously the other" - so running the same fixed probe more
than once per session buys no more signal, only more budget spent and more
latency in the hot path every other gate already shares. **Silent on
healthy and unknown.** This makes no gating decision (driftguard's own
contract, unchanged here) and "Jev unreachable or no key configured" is
not information worth surfacing every session start - logged for the
operator to find if they go looking, the same restraint every other gate
already applies to its own "could not judge" case, never surfaced as if it
were a finding. **Surfaced once, via `additionalContext`, only on
degraded** - not blocked, not warned through any gate's own decision path,
since driftguard's contract is explicit that an unreachable Jev or a
degraded score is evidence about Jev, not about any one gate's verdict,
and conflating the two was the exact mistake this module's own source
article flagged in its own postmortem.

    python lib/driftcheck_hook.py         # as the hook, stdin: SessionStart JSON
    python lib/driftcheck_hook.py --selfcheck
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import driftguard  # noqa: E402
import jevgate  # noqa: E402

GATE = "P8"
ENABLE_VAR = "DRIFTGUARD_ENABLED"
ON_VALUES = ("1", "true", "yes", "on")
HOOK_BUDGET_MS = 29000  # matches driftguard.CALL_BUDGET_MS; one call, same
                        # headroom discipline as every other gate


def enabled():
    return (os.environ.get(ENABLE_VAR) or "").strip().lower() in ON_VALUES


def emit(result):
    """Silence for healthy/unknown, one additionalContext note for
    degraded. Never a permissionDecision - SessionStart has none to set,
    and driftguard makes no gating decision of its own either way."""
    if result.get("status") != "degraded":
        return 0
    pass_rate = result.get("pass_rate")
    shown = f"{pass_rate:.0%}" if isinstance(pass_rate, float) else "n/a"
    wrong = [name for name, _expect, _p, ok in result.get("detail", [])
            if not ok]
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            f"Pipeline drift signal (P8): a live sanity check of Jev "
            f"itself came back degraded this session start, pass_rate "
            f"{shown} on its fixed known-answer probes"
            + (f" ({', '.join(wrong)} scored wrong)" if wrong else "")
            + ". This is not evidence any one gate is wrong - it means "
              "Jev's own answers on obvious cases look off right now. "
              "Treat gate verdicts with extra scrutiny this session and "
              "flag it to the operator; do not silence or work around "
              "any gate because of this alone."),
    }}))
    return 0


def main():
    if not enabled():
        return 0
    session_id = None
    try:
        hook = json.load(sys.stdin)  # SessionStart payload
        if isinstance(hook, dict):
            session_id = hook.get("session_id")
    except Exception:  # noqa: BLE001  malformed/absent stdin is not fatal
        pass
    budget = jevgate.Budget(HOOK_BUDGET_MS)
    result = driftguard.check(budget=budget, session_id=session_id)
    if result["status"] == "unknown":
        jevgate.hook_error(
            GATE, f"pipeline drift check skipped or failed: "
                 f"{result.get('reason', 'no reason given')}")
        return 0
    if result["status"] == "degraded":
        jevgate.hook_error(
            GATE, f"pipeline drift check degraded, pass_rate "
                 f"{result.get('pass_rate')}")
    return emit(result)


# --- offline self-check -------------------------------------------------

def selfcheck():
    import io
    import unittest.mock

    # Off: never calls driftguard, never prints.
    _mock = unittest.mock.MagicMock()
    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "0"}):
        with unittest.mock.patch.object(driftguard, "check", _mock):
            out = io.StringIO()
            with unittest.mock.patch.object(sys, "stdout", out), \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("{}")):
                r = main()
    assert r == 0, r
    assert not _mock.called
    assert out.getvalue() == ""

    # On, healthy: silent, no additionalContext.
    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "1"}):
        with unittest.mock.patch.object(
                driftguard, "check",
                return_value={"status": "healthy", "pass_rate": 1.0,
                             "detail": []}):
            out = io.StringIO()
            with unittest.mock.patch.object(sys, "stdout", out), \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("{}")):
                r = main()
    assert r == 0, r
    assert out.getvalue() == ""

    # On, unknown: silent on stdout, logged via hook_error - never
    # surfaced as if it were a finding.
    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "1"}):
        with unittest.mock.patch.object(
                driftguard, "check",
                return_value={"status": "unknown", "pass_rate": None,
                             "detail": [],
                             "reason": "no Jev key configured"}):
            with unittest.mock.patch.object(jevgate, "hook_error") as herr:
                out = io.StringIO()
                with unittest.mock.patch.object(sys, "stdout", out), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO("{}")):
                    r = main()
    assert r == 0, r
    assert out.getvalue() == ""
    assert herr.called

    # On, degraded: additionalContext printed, logged, names which probes
    # scored wrong so the signal is actionable, not just a number.
    with unittest.mock.patch.dict(os.environ, {ENABLE_VAR: "1"}):
        with unittest.mock.patch.object(
                driftguard, "check",
                return_value={"status": "degraded", "pass_rate": 0.5,
                             "detail": [
                                 ("secret-present", True, 0.1, False),
                                 ("secret-absent", False, 0.05, True),
                                 ("command-catastrophic", True, 0.9, True),
                                 ("command-safe", False, 0.9, False)]}):
            with unittest.mock.patch.object(jevgate, "hook_error") as herr:
                out = io.StringIO()
                with unittest.mock.patch.object(sys, "stdout", out), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO("{}")):
                    r = main()
    assert r == 0, r
    printed = json.loads(out.getvalue())
    ctx = printed["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart", ctx
    assert "50%" in ctx["additionalContext"], ctx
    assert "secret-present" in ctx["additionalContext"], ctx
    assert "command-safe" in ctx["additionalContext"], ctx
    assert "secret-absent" not in ctx["additionalContext"], ctx
    assert "command-catastrophic" not in ctx["additionalContext"], ctx
    assert herr.called

    print("driftcheck_hook selfcheck: ok")
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
