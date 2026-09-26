#!/usr/bin/env python
"""Baseline-comparing eval harness, shared by every gate.

A gate's eval file supplies only its cases. This runs them against the real
hook, stores a dated result file, and diffs against a stored baseline so
"did this change help?" has an answer instead of a feeling.

    python <gate>/scripts/eval_<gate>.py              # run and compare
    python <gate>/scripts/eval_<gate>.py --baseline   # promote this run

Why a stored baseline and not just a pass count: a change can keep the same
9/9 while moving every probability to the edge of its threshold. A pass count
hides that. The diff reports score movement per case, so a result that is
about to break is visible before it breaks.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir))
RESULTS = os.path.join(ROOT, "evals", "results")
BASELINES = os.path.join(ROOT, "evals", "baseline")

# A probability that moves by more than this between runs is called out even
# when the verdict did not change. Jev is stochastic, so small drift is
# normal; this is set above the noise we have observed, not at zero.
DRIFT = 0.10

# One id per eval run, suffixed onto every eval session_id. A fixed id
# ("eval") let jevgate's per-session call cap accumulate across every run
# ever made, so once 200 lifetime eval calls were spent every later run
# failed closed and reported budget exhaustion as gate regressions.
RUN_ID = f"{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"


def transcript(human, tool_calls, final):
    """One human turn as Claude Code transcript rows.

    tool_calls: list of (tool, command_or_path, outcome|None) where outcome
    is "ok", "error", "denied" or None for no result."""
    rows = [{"type": "user", "message": {"content": human}}]
    for i, (name, arg, outcome) in enumerate(tool_calls):
        key = "command" if name == "Bash" else "file_path"
        rows.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name,
             "input": {key: arg}}]}})
        if outcome is not None:
            rows.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "is_error": outcome == "error"}]}})
    rows.append({"type": "assistant",
                 "message": {"content": [{"type": "text", "text": final}]}})
    return rows


def _run_one(hook_path, rows, cwd, log_path):
    """(verdict, stderr, top_probability) for one case."""
    fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        hook = {"transcript_path": path, "stop_hook_active": False,
                "cwd": cwd, "session_id": f"eval-{RUN_ID}"}
        p = subprocess.run([sys.executable, hook_path], input=json.dumps(hook),
                           capture_output=True, text=True, timeout=90)
        top = None
        # The gate logs its own probabilities. Read the line it just wrote
        # rather than re-deriving them, so the harness measures what the gate
        # actually decided on.
        try:
            with open(log_path, encoding="utf-8") as f:
                f.seek(before)
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            if lines:
                vals = [v for v in json.loads(lines[-1]).get("probs", {}).values()
                        if isinstance(v, (int, float))]
                top = max(vals) if vals else None
        except (OSError, ValueError):
            pass
        return ("block" if p.returncode == 2 else "allow",
                (p.stderr or "").strip(), top)
    finally:
        os.unlink(path)


def run_suite(name, cases, hook_path, log_env, cwd=None):
    """cases: list of (case_name, expected, rows). expected is block|allow."""
    cwd = cwd or ROOT
    log_path = os.path.join(tempfile.gettempdir(), f"{name}-eval.jsonl")
    if os.path.exists(log_path):
        os.unlink(log_path)
    os.environ[log_env] = log_path

    results = {}
    print(f"{name}: {len(cases)} cases\n")
    for case_name, expected, rows in cases:
        verdict, msg, top = _run_one(hook_path, rows, cwd, log_path)
        ok = verdict == expected
        results[case_name] = {"expected": expected, "got": verdict, "top": top}
        margin = f"{top:.2f}" if isinstance(top, float) else "  - "
        print(f"{'PASS' if ok else 'FAIL'}  {expected:<5} -> {verdict:<5} "
              f"top={margin}  {case_name}")
        if msg and not ok:
            print(f"          {msg[:150]}")

    n_ok = sum(1 for r in results.values() if r["expected"] == r["got"])
    missed = sum(1 for r in results.values()
                 if r["expected"] == "block" and r["got"] == "allow")
    noisy = sum(1 for r in results.values()
                if r["expected"] == "allow" and r["got"] == "block")
    print(f"\n{n_ok}/{len(cases)} correct. {missed} bad case(s) missed, "
          f"{noisy} fine case(s) wrongly blocked.")

    run = {"suite": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "n": len(cases), "correct": n_ok, "missed": missed, "noisy": noisy,
           "cases": results}
    os.makedirs(RESULTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(RESULTS, f"{name}-{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
    print(f"\nresult: {os.path.relpath(out, ROOT)}")

    base_path = os.path.join(BASELINES, f"{name}.json")
    if "--baseline" in sys.argv:
        os.makedirs(BASELINES, exist_ok=True)
        with open(base_path, "w", encoding="utf-8") as f:
            json.dump(run, f, indent=2)
        print(f"promoted to baseline: {os.path.relpath(base_path, ROOT)}")
        return 0 if n_ok == len(cases) else 1

    return compare(run, base_path) if os.path.exists(base_path) else _no_baseline(
        n_ok, len(cases), base_path)


def _no_baseline(n_ok, total, base_path):
    print(f"\nNo baseline at {os.path.relpath(base_path, ROOT)}. "
          "Re-run with --baseline to record this run as the reference.")
    return 0 if n_ok == total else 1


def compare(run, base_path):
    with open(base_path, encoding="utf-8") as f:
        base = json.load(f)
    print(f"\n--- vs baseline {base['ts']} ---")
    regressions, fixes, drifts, added, removed = [], [], [], [], []

    for name, now in run["cases"].items():
        was = base["cases"].get(name)
        if was is None:
            added.append(name)
            continue
        was_ok = was["expected"] == was["got"]
        now_ok = now["expected"] == now["got"]
        if was_ok and not now_ok:
            regressions.append(f"{name}: {was['got']} -> {now['got']}")
        elif now_ok and not was_ok:
            fixes.append(f"{name}: {was['got']} -> {now['got']}")
        elif (isinstance(was.get("top"), float)
              and isinstance(now.get("top"), float)
              and abs(now["top"] - was["top"]) > DRIFT):
            drifts.append(f"{name}: {was['top']:.2f} -> {now['top']:.2f}")
    removed = [n for n in base["cases"] if n not in run["cases"]]

    for label, items in (("REGRESSION", regressions), ("fixed", fixes),
                         ("score drift", drifts), ("new case", added),
                         ("removed case", removed)):
        for it in items:
            print(f"  {label}: {it}")
    if not any((regressions, fixes, drifts, added, removed)):
        print("  no change")

    print(f"\ncorrect: {base['correct']}/{base['n']} -> "
          f"{run['correct']}/{run['n']}")
    if regressions:
        print("FAILED: this change breaks cases the baseline passed.")
        return 1
    return 0 if run["correct"] == run["n"] else 1


def selfcheck():
    rows = transcript("do a thing", [("Bash", "pytest -q", "error")], "done")
    assert rows[0]["message"]["content"] == "do a thing"
    assert rows[-1]["message"]["content"][0]["text"] == "done"
    assert rows[2]["message"]["content"][0]["is_error"] is True

    base = {"ts": "t0", "n": 2, "correct": 2,
            "cases": {"a": {"expected": "block", "got": "block", "top": 0.90},
                      "b": {"expected": "allow", "got": "allow", "top": 0.30}}}
    fd, p = tempfile.mkstemp(suffix=".json", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(base, f)
    try:
        # A verdict that flips from right to wrong must fail the run.
        bad = {"ts": "t1", "n": 2, "correct": 1,
               "cases": {"a": {"expected": "block", "got": "allow", "top": 0.4},
                         "b": {"expected": "allow", "got": "allow", "top": 0.3}}}
        assert compare(bad, p) == 1
        # An unchanged, fully correct run must pass.
        assert compare(json.loads(json.dumps(base)), p) == 0
    finally:
        os.unlink(p)
    print("evalharness selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(selfcheck())
