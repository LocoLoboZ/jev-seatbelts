#!/usr/bin/env python
"""Gate 6 eval: the real hook, as a subprocess, against a real (throwaway)
git repository per case - not mocked git plumbing like --selfcheck, which
only exercises the pure-Python rule functions against synthetic diff text.

    python skills/code-quality/scripts/eval_gate6.py
    python skills/code-quality/scripts/eval_gate6.py --baseline

Gate 6 never denies or asks (see code_quality.py's module docstring), so
there is no allow/deny outcome to check the way Gates 1-4's evals do. The
only observable behaviour is which rule ids land in the shared finding
store for that case's session - so each case asserts EXACT equality
against the full set of rule ids expected to fire.

Independent review, 2026-09-24 (finding 7): the first pass graded a case
by subset (`expected <= got`), on the reasoning "more evidence than
promised is fine". That reasoning fails for the negative control
specifically - `frozenset() <= got` is true for ANY `got`, so a
completely broken gate that fired every rule on an innocent commit would
still report the negative control as a pass. Exact equality closes that,
and every case's expected set below has been checked against what the
gate's OWN NEVER_GATE rules (`dependency-category`,
`consequence-scoping`) legitimately also fire alongside the rule under
test - `dependency-category` always runs and will co-fire on a case whose
diff happens to add a true-external import.

The "depth" case is marked `expected=JUDGED`: it exercises a real (or, with
no key configured, skipped) Jev call and is graded on the subprocess not
crashing, never on whether Jev's own answer happened to cross the
threshold - the same discipline every other gate's `judged` case here
already carries, because a live model's answer is not the kind of thing a
static fixture should pin to one value.
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
import findings  # noqa: E402  to read back what the hook wrote

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "code_quality.py")
SUITE = "gate6"
JUDGED = "judged"

HOTSPOT_N = 15  # must match code_quality.HOTSPOT_COMMIT_THRESHOLD


def _git(repo, *args):
    subprocess.run(("git",) + args, cwd=repo, check=True,
                   capture_output=True, text=True)


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "eval@example.com")
    _git(repo, "config", "user.name", "gate6 eval")
    _write(repo, "README.md", "eval fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")


def _setup_seam_reality(repo):
    _write(repo, "skills/newmod/handler.py", "class Handler(ABC):\n    pass\n")
    _git(repo, "add", "-A")


def _setup_interface_test_surface(repo):
    _write(repo, "tests/test_thing.py",
          "from mypkg import _internal_helper\n")
    _git(repo, "add", "-A")


def _setup_test_layering(repo):
    _write(repo, "tests/test_widget.py", "def test_a():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test: existing widget test")
    _write(repo, "tests/widget_test.py", "def test_b():\n    pass\n")
    _git(repo, "add", "-A")


def _setup_dependency_category(repo):
    _write(repo, "lib/newthing.py", "import requests\n\ndef f():\n    pass\n")
    _git(repo, "add", "-A")


def _setup_health_delta(repo):
    _write(repo, "lib/calc.py", "def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: calc")
    _write(repo, "lib/calc.py",
          "def f(a):\n"
          "    if a:\n"
          "        pass\n"
          "    elif a == 2:\n"
          "        pass\n"
          "    for x in range(a):\n"
          "        pass\n"
          "    while a:\n"
          "        a -= 1\n"
          "    try:\n"
          "        pass\n"
          "    except ValueError:\n"
          "        pass\n"
          "    return 1\n")
    _git(repo, "add", "-A")


def _setup_consequence_scoping(repo):
    for i in range(HOTSPOT_N):
        _write(repo, "lib/hot.py", f"x = {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"chore: touch hot {i}")
    _write(repo, "lib/hot.py", f"x = {HOTSPOT_N}\n")
    _git(repo, "add", "-A")


def _setup_depth_judged(repo):
    body = "\n".join(f"line_{i} = {i}" for i in range(20))
    _write(repo, "lib/bigmodule.py", body + "\n")
    _git(repo, "add", "-A")


def _setup_nothing(repo):
    _write(repo, "innocuous.py", "x = 1\n")
    _git(repo, "add", "-A")


# (name, expected rule-id set (frozenset, exact match) or JUDGED, setup)
CASES = [
    ("an unrelated trivial commit earns no findings",
     frozenset(), _setup_nothing),
    ("seam-reality: a new ABC-based class with no other implementer",
     frozenset({"seam-reality"}), _setup_seam_reality),
    # `mypkg` is a fabricated third-party import, so dependency-category
    # (a NEVER_GATE rule - it always runs) legitimately co-fires here too.
    ("interface-test-surface: a test imports a private symbol directly",
     frozenset({"interface-test-surface", "dependency-category"}),
     _setup_interface_test_surface),
    ("test-layering: a new test lands beside an untouched sibling",
     frozenset({"test-layering"}), _setup_test_layering),
    ("dependency-category: a new true-external import",
     frozenset({"dependency-category"}), _setup_dependency_category),
    ("health-delta: branch points added to an existing file",
     frozenset({"health-delta"}), _setup_health_delta),
    ("consequence-scoping: a file crosses the hot-spot commit threshold",
     frozenset({"consequence-scoping"}), _setup_consequence_scoping),
    ("depth: a substantial new module, judged live", JUDGED,
     _setup_depth_judged),
]


def run_one(name, setup):
    session_id = "gate6-eval-" + re_safe(name)
    with tempfile.TemporaryDirectory() as repo, \
        tempfile.TemporaryDirectory() as findings_dir:
        _init_repo(repo)
        setup(repo)
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m x"},
            "session_id": session_id, "cwd": repo})
        env = dict(os.environ, GATE6_ENABLED="1",
                  GATE6_MEASURE_FILE=os.path.join(repo, "measure.json"),
                  JEV_FINDINGS_DIR=findings_dir)
        p = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return None, f"exit{p.returncode}: {(p.stderr or '')[:200]}"
        try:
            rows = _read_findings(findings_dir, session_id)
        except Exception as exc:  # noqa: BLE001  a read failure is data too
            return None, f"could not read findings store: {exc}"
        return {r.get("ruleId") for r in rows}, (p.stderr or "")[:200]


def _read_findings(findings_dir, session_id):
    """Read the child process's finding store from this process, which
    means pointing findings.py's own env var at the same directory the
    child was given - the hook and this eval must agree on where the
    store lives, or every read here sees an empty store regardless of
    what the child actually wrote."""
    old = os.environ.get("JEV_FINDINGS_DIR")
    os.environ["JEV_FINDINGS_DIR"] = findings_dir
    try:
        return findings.read(session_id)
    finally:
        if old is None:
            os.environ.pop("JEV_FINDINGS_DIR", None)
        else:
            os.environ["JEV_FINDINGS_DIR"] = old


def re_safe(name):
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:60]


def main():
    results = {}
    print(f"{SUITE}: {len(CASES)} cases\n")
    for name, expected, setup in CASES:
        got, detail = run_one(name, setup)
        if got is None:
            ok = False
        elif expected is JUDGED:
            ok = True  # graded on not crashing only - see module docstring
        else:
            ok = expected == got  # exact - see finding 7 in module docstring
        results[name] = {"expected": (sorted(expected)
                                      if expected is not JUDGED else JUDGED),
                         "got": sorted(got) if got is not None else None}
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print(f"          got rules: {sorted(got) if got is not None else got}")
        if not ok and detail:
            print(f"          {detail}")

    def _ok(r):
        if r["got"] is None:
            return False
        if r["expected"] == JUDGED:
            return True
        return set(r["expected"]) == set(r["got"])

    n_ok = sum(1 for r in results.values() if _ok(r))
    print(f"\n{n_ok}/{len(CASES)} correct.")

    run = {"suite": SUITE, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "n": len(CASES), "correct": n_ok, "cases": results}
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
