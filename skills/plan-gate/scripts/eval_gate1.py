#!/usr/bin/env python
"""Gate 1 eval: the real hook, as a subprocess, one case at a time.

    python skills/plan-gate/scripts/eval_gate1.py
    python skills/plan-gate/scripts/eval_gate1.py --baseline

The deterministic floor (D0-D7, D9) is offline and model-independent - a
plan either trips a fixed rule or it does not, on any machine. The
Jev-judged tier that runs after it is model-dependent, same caveat
eval_gate2.py's own docstring gives: `expected="judged"` exists for a
plan that clears the deterministic floor and reaches a live Jev call, on
a machine with a real key configured.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
import evalharness  # noqa: E402  for RESULTS/BASELINES and compare() only

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "plan_gate.py")
SUITE = "gate1"
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
THIS_FILE = os.path.basename(os.path.abspath(__file__))

# (name, expected, plan)
CASES = [
    ("a one-line fix: trivial work class, allowed with no check run",
     "allow", "Fix the crash in `README.md`."),
    ("an empty plan is not resolved", "ask", ""),
    ("too many unresolved [NEEDS CLARIFICATION] markers: denied", "deny",
     "Build a feature. [NEEDS CLARIFICATION: a] "
     "[NEEDS CLARIFICATION: b] [NEEDS CLARIFICATION: c] "
     "[NEEDS CLARIFICATION: d]"),
    ("a TODO placeholder remains: denied", "deny",
     "Build the export flow. TODO: figure out the API shape."),
    ("no falsifiable outcome anywhere: denied", "deny",
     "Build a better, more scalable system that improves reliability "
     "for users across the whole platform."),
    ("a named artefact does not resolve: denied", "deny",
     "Fix the retry logic within `nonexistent/made-up-file-xyz.py` so "
     "p99 latency drops to 50ms."),
    ("an override asserted but incomplete: denied", "deny",
     "## Override\nSkip the review rule, reducing load by 10%.\n"
     "Violation: skips the review rule."),
    ("an unquantified adjective, otherwise clean: needs clarification",
     "ask", f"Rewrite `{THIS_FILE}` to be fast. This reduces load by "
     f"10%, verified by `test_load.py`."),
    ("no proof command, otherwise clean: needs clarification", "ask",
     f"Update `{THIS_FILE}` to reduce load by 10%."),
    ("a fully clean, real feature plan: judged live", "judged",
     f"Update `{THIS_FILE}` to reduce load by 10%, verified by "
     f"`test_load.py`."),
]

GATE1_EVAL_LOG = os.path.join(tempfile.gettempdir(), "gate1-eval.jsonl")


def case_ok(expected, got, rec=None):
    """`rec` is the whole logged decision record, not just its rule
    name. Adversarial review, 2026-09-24, finding 4: a rule-name
    allowlist masked a real bug - `decide()` collapses the rule to the
    generic "needs-clarification" whenever more than one ask-tier
    reason fires, including when every one of those reasons is a purely
    deterministic soft check (D3/D6/D7) and Jev was never reached at
    all. Since that generic name was itself in the old allowlist, a
    "judged" case could pass on a plan Jev never actually saw. Checking
    the logged `j_probs` field instead is checking the actual fact - was
    a real Jev answer set produced - rather than pattern-matching a rule
    string that a masked path can also produce."""
    if expected == "judged":
        rec = rec or {}
        return got in ("allow", "deny", "ask") and rec.get("j_probs") is not None
    return expected == got


def _last_logged():
    try:
        with open(GATE1_EVAL_LOG, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError, IndexError):
        return {}


def run_one(plan):
    try:
        fd = os.open(GATE1_EVAL_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        os.close(fd)
    except OSError:
        pass
    payload = json.dumps({"tool_name": "ExitPlanMode",
                          "tool_input": {"plan": plan},
                          "session_id": f"eval-gate1-{evalharness.RUN_ID}", "cwd": THIS_DIR})
    env = dict(os.environ, GATE1_ENABLED="1", GATE1_LOG=GATE1_EVAL_LOG,
              JEV_FINDINGS_DIR=os.path.join(tempfile.gettempdir(),
                                            "gate1-eval-findings"))
    p = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return f"exit{p.returncode}", (p.stderr or "").strip()[:200], {}
    if not p.stdout.strip():
        return "allow", "", _last_logged()
    try:
        out = json.loads(p.stdout)["hookSpecificOutput"]
    except (ValueError, KeyError):
        return "malformed", p.stdout.strip()[:200], {}
    decision = out.get("permissionDecision") or "allow"
    return (decision, out.get("permissionDecisionReason", ""),
           _last_logged())


def main():
    results = {}
    print(f"{SUITE}: {len(CASES)} cases\n")
    for name, expected, plan in CASES:
        got, reason, rec = run_one(plan)
        ok = case_ok(expected, got, rec)
        rule = rec.get("rule")
        results[name] = {"expected": expected, "got": got, "rule": rule,
                         "j_probs": rec.get("j_probs")}
        print(f"{'PASS' if ok else 'FAIL'}  {expected:<7} -> {str(got):<7}  "
             f"{name}")
        if not ok and reason:
            print(f"          {reason[:150]}")
        if not ok and expected == "judged" and got in ("allow", "deny", "ask") \
                and rec.get("j_probs") is None:
            print(f"          masked: rule was {rule!r}, but no j_probs "
                 f"were logged - not a genuine Jev judgment, Jev was "
                 f"likely unreachable")

    n_ok = sum(1 for r in results.values()
              if case_ok(r["expected"], r["got"], r))
    missed = sum(1 for r in results.values()
                if r["expected"] == "deny" and r["got"] == "allow")
    noisy = sum(1 for r in results.values()
               if r["expected"] == "allow" and r["got"] in ("deny", "ask"))
    print(f"\n{n_ok}/{len(CASES)} correct. {missed} bad plan(s) allowed, "
         f"{noisy} fine plan(s) wrongly stopped "
         f"(excludes 'judged' cases from these two counts).")

    run = {"suite": SUITE, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "n": len(CASES), "correct": n_ok, "missed": missed, "noisy": noisy,
          "cases": results}
    os.makedirs(evalharness.RESULTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(evalharness.RESULTS, f"{SUITE}-{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
    print(f"\nresult: {os.path.relpath(out, evalharness.ROOT)}")

    base_path = os.path.join(evalharness.BASELINES, f"{SUITE}.json")
    if "--baseline" in sys.argv:
        os.makedirs(evalharness.BASELINES, exist_ok=True)
        with open(base_path, "w", encoding="utf-8") as f:
            json.dump(run, f, indent=2)
        print(f"promoted to baseline: "
             f"{os.path.relpath(base_path, evalharness.ROOT)}")
        return 0 if n_ok == len(CASES) else 1

    if os.path.exists(base_path):
        return evalharness.compare(run, base_path)
    return evalharness._no_baseline(n_ok, len(CASES), base_path)


if __name__ == "__main__":
    sys.exit(main())
