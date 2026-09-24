#!/usr/bin/env python
"""Gate 5 eval: the real hypothesis_ranker.py, as a subprocess, one case
at a time.

    python skills/debug-triage/scripts/eval_gate5.py
    python skills/debug-triage/scripts/eval_gate5.py --baseline

Unlike Gates 3/4/7, this gate has no allow/deny/ask verdict to score - its
only output is an ORDER over 2-5 hypotheses. So "correct" here means: does
the hypothesis actually documented as the case's real root cause come back
ranked first. Each case names one genuine cause and 1-3 plausible-sounding
distractors, the same shape a real debugging session hands the ranker.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
import evalharness  # noqa: E402  for RESULTS/BASELINES and compare() only

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "hypothesis_ranker.py")
SUITE = "gate5"

# (case name, expected top hypothesis id, failure evidence, hypotheses).
# Each real-cause hypothesis is deliberately not the first one listed, so a
# ranker that just echoed input order back would fail these.
CASES = [
    ("concurrent cache write race", "cause", (
        "Test suite intermittently fails with 'KeyError: session_id' in "
        "cache.py line 42, only under concurrent load, never in "
        "single-threaded runs. Logs show two requests hitting the same "
        "cache key within 2ms of each other."),
     [{"id": "distractor1", "text": "The test fixture has a typo in the "
                                    "key name that only shows up "
                                    "alphabetically"},
      {"id": "cause", "text": "The cache write is not atomic, so two "
                              "concurrent requests can interleave a "
                              "partial write and a read, leaving the key "
                              "briefly absent"},
      {"id": "distractor2", "text": "The test runner's random seed "
                                    "changed between runs"}]),
    ("off-by-one in pagination", "cause", (
        "The last item on every page is duplicated as the first item of "
        "the next page. Page size is fixed at 20 and confirmed correct in "
        "config. Reproduces on every page boundary, every run, single "
        "request, no concurrency involved."),
     [{"id": "distractor1", "text": "The database connection pool is "
                                    "exhausted under load"},
      {"id": "cause", "text": "The offset calculation uses page * "
                              "page_size instead of (page - 1) * "
                              "page_size, so consecutive pages overlap by "
                              "one page_size worth of the wrong direction"},
      {"id": "distractor2", "text": "The frontend is caching stale page "
                                    "data in local storage"}]),
    ("stale config after deploy", "cause", (
        "A feature flag flipped in the config file is not taking effect "
        "until the process is manually restarted, even though the config "
        "loader logs 'reloaded' every 30 seconds as designed. "
        "Reproduces 100% of the time after any config edit."),
     [{"id": "distractor1", "text": "The feature flag name has a typo in "
                                    "the config file"},
      {"id": "cause", "text": "The config loader reads the file into a "
                              "module-level dict on first import and the "
                              "'reload' path only logs success without "
                              "ever reassigning that same dict object, so "
                              "every existing reference still points at "
                              "the original values"},
      {"id": "distractor2", "text": "The deploy pipeline is not shipping "
                                    "the updated config file at all"}]),
]


def _class(rule):
    return rule


def run_one(failure, hyps):
    """(ranked_ids, raw_stdout) - ranked_ids is [] if the ranker could not
    rank at all (the UNRANKED fallback fired), never a guessed order."""
    payload = json.dumps({"failure": failure, "hypotheses": hyps})
    env = dict(os.environ, GATE5_ENABLED="1")
    p = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                       capture_output=True, text=True, timeout=60)
    out = p.stdout
    if "UNRANKED" in out:
        return [], out
    ranked = [ln.split()[1] for ln in out.splitlines()
             if ln.strip().startswith("RANK")]
    return ranked, out


def main():
    results = {}
    print(f"{SUITE}: {len(CASES)} cases\n")
    for name, expected_top, failure, hyps in CASES:
        ranked, out = run_one(failure, hyps)
        got_top = ranked[0] if ranked else None
        ok = got_top == expected_top
        results[name] = {"expected": expected_top, "got": got_top,
                         "ranked": ranked}
        print(f"{'PASS' if ok else 'FAIL'}  top={expected_top:<12} -> "
             f"{str(got_top):<12}  {name}")
        if not ok:
            print(f"          full order: {ranked or 'UNRANKED (jev not '
                                                       'reached)'}")

    n_ok = sum(1 for r in results.values() if r["expected"] == r["got"])
    unranked = sum(1 for r in results.values() if not r["ranked"])
    print(f"\n{n_ok}/{len(CASES)} correct. {unranked} case(s) could not be "
         f"ranked at all (excluded from the correctness count above only "
         f"in the sense that a None top never equals an id).")

    run = {"suite": SUITE, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "n": len(CASES), "correct": n_ok, "unranked": unranked,
          "cases": results}
    os.makedirs(evalharness.RESULTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(evalharness.RESULTS, f"{SUITE}-{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
    print(f"\nresult: {os.path.relpath(out_path, evalharness.ROOT)}")

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
