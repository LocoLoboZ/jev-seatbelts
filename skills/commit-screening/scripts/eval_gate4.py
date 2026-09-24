#!/usr/bin/env python
"""Gate 4 eval: the real hook, as a subprocess, one case at a time.

    python skills/commit-screening/scripts/eval_gate4.py
    python skills/commit-screening/scripts/eval_gate4.py --baseline

Unlike Gate 3's eval, a case here is not just a command string - the
decision depends on the actual staged/working-tree state of a real git
repository at the hook's `cwd`. Each case therefore builds its own fresh
temp repo via `setup(repo)`, which returns the command to send to the
hook. `evalharness` is imported only for its RESULTS/BASELINES path
constants and its `compare`/`_no_baseline` reporting - its own
`run_suite` assumes one shared `cwd` for the whole suite, which does not
fit a gate whose verdict depends on per-case repository state.
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
                    "commit_screening.py")
SUITE = "gate4"


def _run(repo, *args):
    return subprocess.run(("git",) + args, cwd=repo, capture_output=True,
                          text=True, check=True)


def _init_repo(repo):
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "a.py"), "w", encoding="utf-8") as f:
        f.write("print('hello')\n")
    _run(repo, "add", "a.py")
    _run(repo, "commit", "-q", "-m", "init")


def _write(repo, name, content, append=False):
    mode = "a" if append else "w"
    with open(os.path.join(repo, name), mode, encoding="utf-8") as f:
        f.write(content)


# (name, expected, setup) - setup(repo) prepares git state, returns the
# command string this gate should decide on.

def _c_clean_commit(repo):
    _write(repo, "b.py", "print('clean')\n")
    _run(repo, "add", "b.py")
    return "git commit -m x"


def _c_secret_commit(repo):
    _write(repo, "b.py", "KEY = 'ghp_" + "a" * 36 + "'\n")
    _run(repo, "add", "b.py")
    return "git commit -m x"


def _c_secret_in_removed_line_only(repo):
    # The secret is already committed; this commit only REMOVES it. Not a
    # new leak, and must not be denied for deleting one.
    _write(repo, "a.py", "KEY = 'ghp_" + "a" * 36 + "'\n")
    _run(repo, "add", "a.py")
    _run(repo, "commit", "-q", "-m", "carrying a secret already")
    _write(repo, "a.py", "print('cleaned')\n")
    _run(repo, "add", "a.py")
    return "git commit -m x"


def _c_not_a_commit(repo):
    _write(repo, "b.py", "KEY = 'ghp_" + "a" * 36 + "'\n")
    return "git status"


def _c_chained_add_and_commit(repo):
    _write(repo, "b.py", "TOKEN = 'AKIAABCDEFGHIJKLMNOP'\n")
    return "git add b.py && git commit -m x"


def _c_amend_all_catches_unstaged(repo):
    _write(repo, "a.py", "print('unstaged secret next')\n")
    _run(repo, "add", "a.py")
    _run(repo, "commit", "-q", "-m", "setup")
    _write(repo, "a.py", "TOKEN = 'AKIAABCDEFGHIJKLMNOP'\n", append=True)
    return "git commit -am x"  # modified but never `git add`-ed


def _c_private_key_block(repo):
    _write(repo, "id_rsa", "-----BEGIN RSA PRIVATE KEY-----\nfake\n")
    _run(repo, "add", "id_rsa")
    return "git commit -m x"


def _c_no_secret_in_commit_message(repo):
    # The secret-shaped text is in the MESSAGE, not the diff. Phase 1 only
    # screens the diff - see the module docstring. Must not be denied.
    _write(repo, "b.py", "print('fine')\n")
    _run(repo, "add", "b.py")
    return "git commit -m 'ghp_" + "a" * 36 + "'"


def _c_risk_path_touched(repo):
    # No secret pattern anywhere - the fixed floor clears this instantly.
    # What this case actually exercises is phase 2's live round trip: a
    # real Jev call, against a real filename touching a named risk
    # category (auth), scored by whatever the served model actually
    # answers today - not a fixture pinned to one value. See "judged"
    # below for why this must be checked by rule name, not by outcome.
    os.makedirs(os.path.join(repo, "auth"), exist_ok=True)
    _write(repo, os.path.join("auth", "login.py"),
          "def check_password(user, raw):\n"
          "    return raw == user.stored_password_plaintext\n")
    _run(repo, "add", "auth/login.py")
    return "git commit -m x"


CASES = [
    ("a clean commit is silently allowed", "allow", _c_clean_commit),
    ("a github token staged for commit is denied", "deny", _c_secret_commit),
    ("removing a line with an old secret is not a new leak", "allow",
     _c_secret_in_removed_line_only),
    ("a non-commit command is untouched even with a dirty tree", "allow",
     _c_not_a_commit),
    ("a chained git add && git commit is still screened", "deny",
     _c_chained_add_and_commit),
    ("-a catches a tracked change that was never staged", "deny",
     _c_amend_all_catches_unstaged),
    ("a private key block is denied", "deny", _c_private_key_block),
    ("a secret-shaped commit message is not screened, only the diff is",
     "allow", _c_no_secret_in_commit_message),
    ("phase 2: a commit touching a named risk path is actually judged live",
     "judged", _c_risk_path_touched),
]

# A "judged" case must actually have reached the Jev-judged tier - checked
# by rule name (jev-judged-secret or jev-risk-tier or jev-judged-clean),
# not by outcome alone. An allow from a genuine judgment and an allow from
# Jev being unreachable (falls back to "resolved-safe") render identically
# on stdout, exactly the masking bug Gate 3's own eval (JUDGED_RULE)
# already exists to catch - reproduced here for the same reason.
JUDGED_RULES = ("jev-judged-secret", "jev-risk-tier", "jev-judged-clean")

GATE4_EVAL_LOG = os.path.join(tempfile.gettempdir(), "gate4-eval.jsonl")


def _last_logged_rule():
    try:
        with open(GATE4_EVAL_LOG, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        return json.loads(lines[-1]).get("rule") if lines else None
    except (OSError, ValueError, IndexError):
        return None


def case_ok(expected, got, rule=None):
    if expected == "judged":
        if rule is None:
            return got in ("allow", "deny")
        return rule in JUDGED_RULES and got in ("allow", "deny")
    return expected == got


def run_one(setup):
    repo = tempfile.mkdtemp(prefix="jev-gate4-eval-")
    try:
        _init_repo(repo)
        command = setup(repo)
        # A single cumulative log across every case in the run - truncate
        # first, or a case that logs nothing inherits the previous case's
        # rule from _last_logged_rule() (Gate 3's eval hit exactly this).
        try:
            fd = os.open(GATE4_EVAL_LOG, os.O_WRONLY | os.O_CREAT
                        | os.O_TRUNC, 0o600)
            os.close(fd)
        except OSError:
            pass
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command},
                              "session_id": "eval-gate4", "cwd": repo})
        env = dict(os.environ, GATE4_ENABLED="1", GATE4_LOG=GATE4_EVAL_LOG,
                   JEV_FINDINGS_DIR=os.path.join(
                       tempfile.gettempdir(), "gate4-eval-findings"))
        p = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return f"exit{p.returncode}", (p.stderr or "").strip()[:200], None
        if not p.stdout.strip():
            return "allow", "", _last_logged_rule()
        try:
            out = json.loads(p.stdout)["hookSpecificOutput"]
        except (ValueError, KeyError):
            return "malformed", p.stdout.strip()[:200], None
        decision = out.get("permissionDecision")
        if decision is None:
            # additionalContext-only response (the jev-risk-tier carve-out
            # in commit_screening.py's own emit() - RR-14, a non-decision,
            # never a denial). Found by independent review: this eval read
            # the missing key as an unclassifiable "None" and failed a
            # commit Gate 4 had correctly allowed, because a genuine
            # jev-risk-tier flag is the one JSON shape this gate emits
            # with no permissionDecision key at all.
            decision = "allow"
        return (decision, out.get("permissionDecisionReason")
               or out.get("additionalContext", ""), _last_logged_rule())
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def main():
    results = {}
    print(f"{SUITE}: {len(CASES)} cases\n")
    for name, expected, setup in CASES:
        got, reason, rule = run_one(setup)
        ok = case_ok(expected, got, rule)
        results[name] = {"expected": expected, "got": got, "rule": rule}
        print(f"{'PASS' if ok else 'FAIL'}  {expected:<5} -> {str(got):<5}  "
             f"{name}")
        if not ok and reason:
            print(f"          {reason[:150]}")
        if not ok and expected == "judged" and got in ("allow", "deny") \
                and rule not in JUDGED_RULES:
            print(f"          masked: rule was {rule!r}, not a genuine "
                 f"Jev judgment - Jev was likely unreachable")

    n_ok = sum(1 for name, r in results.items()
              if case_ok(r["expected"], r["got"], r["rule"]))
    missed = sum(1 for r in results.values()
                if r["expected"] == "deny" and r["got"] == "allow")
    noisy = sum(1 for r in results.values()
               if r["expected"] == "allow" and r["got"] == "deny")
    print(f"\n{n_ok}/{len(CASES)} correct. {missed} secret(s) missed, "
         f"{noisy} clean commit(s) wrongly blocked "
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
