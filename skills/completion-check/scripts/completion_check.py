#!/usr/bin/env python
"""Gate 7: a Claude Code Stop hook that refuses a premature "done".

Reads the Stop hook JSON on stdin. Gathers the tail of the transcript plus
hard evidence from the working tree, asks Jev one yes/no question per rule in
rules.md, and exits 2 with a message when a rule is over its threshold. Exit 2
on a Stop hook sends the agent back to work; exit 0 lets it stop.

    echo '{"transcript_path":"...","stop_hook_active":false}' | python completion_check.py
    python completion_check.py --selfcheck    # offline assertions, no API call

Everything generic lives in lib/jevgate.py. What stays here is only what is
specific to judging a completion claim: the test-command evidence, the
questions, and the block message.

Design credit: the transcript-parsing and stop-hook shape follow
noplan-inc/limpet (MIT). See ../references/PROVENANCE.md.

Fails open. No key, API down, timeout, bad transcript, or any other error
exits 0 silently. Gate 7 is not the last line of defence against an
irreversible action, so it must never wedge a session. Set GATE7_DEBUG=1 to
print the traceback instead.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest.mock  # selfcheck only: simulates a failed git call

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
import findings  # noqa: E402
import jevgate  # noqa: E402

GATE = 7
HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, os.pardir, "references", "rules.md")
DEFAULT_BLOCK = 0.75  # provisional; replace with calibrated per-rule values
DEFAULT_WARN = 0.50   # provisional; a warn is recorded, never blocks a stop
LOG_DEFAULT = "~/.jev-gates/gate7.jsonl"

# Off unless switched on deliberately. Claude Code has no per-hook enable
# mechanism, so registering the hook in hooks.json makes it fire on every
# Stop; the switch has to live inside the script. Documenting the gate as off
# by default while it was actually on is exactly the blocker an independent
# review caught once already, so this is the real thing rather than a claim.
ENABLE_VAR = "GATE7_ENABLED"
ON_VALUES = ("1", "true", "yes", "on")

# Most findings an earlier gate's output may contribute to the state. A long
# session must not spend Gate 7's budget on someone else's backlog.
FINDINGS_MAX = 25

# The gate's own budget, inside Claude Code's ~30 s hook timeout. Leaning
# only on the outer timeout lets the gate spend its whole allowance parsing
# a transcript and then begin an API call it cannot finish. See
# reference/GSTACK-REVIEW.md.
#
# 24000 used to leave only 24s inside the check below, but the check now
# requires jevgate.CALL_WORST_MS + jevgate.CALL_MARGIN_MS (28s) of
# headroom before it will even attempt the call - a fixed budget smaller
# than what it is checked against would fail every real invocation before
# ever reaching Jev, the mirror-image bug to the one this raise fixes.
# 29000 matches Gate 3's HOOK_BUDGET_MS: full envelope, 1s of slack
# against the assumed ~30s outer cap for the process's own overhead.
BUDGET_MS = 29000

# Round 8 review, Part 3: a miscalculated required-ms threshold that exceeds
# BUDGET_MS trips the pre-work skip on every invocation and silently disables
# the gate while its own selfcheck and enable flag both still claim it is on
# (this exact mistake was caught by hand in round 7, before it was committed).
# Failing loud at import time means a future version of that mistake breaks
# every selfcheck run immediately instead of only showing up as an unexplained
# drop in real Jev calls. Round 9 review, Part 3: a bare `assert` is stripped
# entirely under `python -O`, and this project's own convention elsewhere is
# to wrap broad sections in `except Exception` for fail-open behaviour, which
# would silently swallow an AssertionError. sys.exit() raises SystemExit,
# which is a BaseException, not an Exception, so it survives both: it cannot
# be optimised away, and it passes straight through every `except Exception`
# in this codebase instead of being caught and turned into a graceful skip.
if not jevgate.CALL_WORST_MS + jevgate.CALL_MARGIN_MS < BUDGET_MS:
    sys.exit(
        f"required headroom {jevgate.CALL_WORST_MS + jevgate.CALL_MARGIN_MS}"
        f"ms exceeds BUDGET_MS {BUDGET_MS}ms - Gate 7 would skip every "
        "real call")

# Commands that produce real pass/fail evidence. Matched against the command
# text of a Bash tool call, so "npx vitest run" and "uv run pytest -q" match.
TEST_PATTERNS = re.compile(
    r"\b(pytest|py\.test|vitest|jest|mocha|unittest|tox|nox|phpunit|rspec|"
    # The gap is bounded and lazy rather than \S*, which could not cross an
    # interpreter flag or a quoted path containing a space. `python -u
    # .../eval_gate3.py` and `python "my tests/eval_gate3.py"` are ordinary
    # ways to run this project's eval and both were read as no test at all.
    # It still cannot cross a command separator, so the interpreter and the
    # script have to belong to the same command.
    r"(python3?|py|uv\s+run|poetry\s+run|pipenv\s+run)"
    r"\s+[^;&|]{0,80}?eval_\w+\.py|"
    r"go\s+test|cargo\s+test|dotnet\s+test|mvn\s+(test|verify)|gradle\s+test|"
    r"(npm|pnpm|yarn|bun|make|just)\s+(run\s+)?(test|check|lint|ci))\b"
    # Outside the \b group on purpose: a word boundary cannot sit between a
    # space and a hyphen, so wrapping a flag in \b...\b matches nothing.
    # The flag alone was not enough either. Unanchored, it matched
    # `git commit -m "update --selfcheck docs"` and a URL carrying
    # ?arg=--selfcheck, so mentioning the flag disarmed the rule exactly as
    # naming a test file used to. It now needs a script invocation in front
    # of it, within a bounded gap so a truncated line still matches across
    # its elision marker.
    #
    # Both gaps here excluded the same separator characters as the eval_\w+
    # alternative above only in comment, not in the pattern: `.{0,80}?`
    # before this fix could cross `&&`/`;`/`|`, so `python migrate.py &&
    # rm -rf / ; # --selfcheck` matched as a test run of an unrelated,
    # unreviewed command chain. `\S+\.py` also could not see `python -u
    # script.py`, a real flag before a real script, because a bare `\S+`
    # stops at the first space and `-u` does not end in `.py`. Found by
    # independent review, the same day as the fix above it, in the
    # alternative that fix's own sibling should have anchored the same way.
    r"|(python3?|py|uv\s+run|poetry\s+run|pipenv\s+run)"
    r"\s+[^;&|\n]{0,80}?\.py[^;&|\n]{0,80}?--self[-_]?check\b"
)

# Why the two middle alternatives carry an invocation context rather than
# standing alone. They were bare word stems, `selfcheck` and `eval_\w+\.py`,
# matched anywhere in the command text. `git diff path/eval_gate3.py`,
# `cat eval_gate3.py` and `git commit -m "fix selfcheck wording"` all
# registered as "tests were run this turn", so merely naming a test file
# disarmed the rule that blocks a premature completion claim. A flag is a
# flag and an invocation needs an interpreter in front of it.

# Tools whose command text can be a test run. Bash alone missed every test
# run on a Windows machine where Claude Code's primary shell is the
# PowerShell tool: a turn that ran the whole eval suite was logged with
# tests_run [] and blocked as a claim with no tests behind it.
SHELL_TOOLS = ("Bash:", "PowerShell:")

# A project names its checks what it likes, and this one runs
# `python lib/jevgate.py`, which looks nothing like pytest.
EXTRA_TEST_VAR = "GATE7_EXTRA_TEST"

ASK = ("The agent is about to stop and hand control back to the human. Is it "
       "violating this rule right now?")
TRUE_MEANS = ("The final message, the tool calls, or the hard evidence show "
              "this rule being broken")
FALSE_MEANS = ("The rule is respected, or it does not apply to this turn")


# --- hard evidence ------------------------------------------------------

def _git(cwd, *args):
    """A tri-state reading, never a bare string.

    A caller that did `_git(...) or ""` turned a timed-out or denied git
    call into the same empty string a clean working tree produces, and a
    rule scored over "uncommitted changes: ''" cannot tell those apart. Only
    an actual zero-output success may render as "".
    """
    label = "git " + " ".join(args)
    try:
        r = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return jevgate.reading(jevgate.READ_ERR, detail=f"{label}: {exc}")
    if r.returncode != 0:
        detail = (r.stderr or "").strip()[:200] or f"{label} exited {r.returncode}"
        return jevgate.reading(jevgate.READ_NA, detail=detail)
    return jevgate.reading(jevgate.READ_OK, value=r.stdout.strip())


TUNED_DEFAULT = "~/.jev-gates/gate7-thresholds.json"


def spec_for(env_var, kind, default):
    """The threshold spec this decision uses. An operator's env value
    always wins. Otherwise the per-rule values gate7_selftune.py fitted
    and guarded, if any. Otherwise the placeholder default. A missing or
    unreadable tuned file is the placeholder, never an error."""
    if (os.environ.get(env_var) or "").strip():
        return jevgate.thresholds(env_var, default)
    path = os.path.expanduser(os.environ.get("GATE7_TUNED") or TUNED_DEFAULT)
    try:
        with open(path, encoding="utf-8") as f:
            tuned = (json.load(f).get(kind) or {})
        return default, {str(k): float(v) for k, v in tuned.items()
                         if isinstance(v, (int, float))
                         and not isinstance(v, bool) and 0.0 < v < 1.0}
    except (OSError, ValueError, AttributeError, TypeError):
        return default, {}


def test_patterns():
    """The built-in conventions, plus whatever this project calls a check.

    The detector knew pytest and npm test and nothing else, so in a
    repository whose whole suite is `python lib/jevgate.py` and
    `... --selfcheck` it reported "no test command was run this turn" as
    hard evidence on every turn that ran the entire suite. The gate then
    blocked, correctly, on a fact its own code had got wrong. rules.md
    already documented `python lib/jevgate.py` as a clear pass for that
    rule, so the detector contradicted the anchor it is measured against.

    Additive only, like GATE3_EXTRA_ASK. A project may teach the gate
    about a check it would otherwise miss. It may never hide one."""
    pats = [TEST_PATTERNS]
    for raw in (os.environ.get(EXTRA_TEST_VAR) or "").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            pats.append(re.compile(raw))
        except re.error:
            jevgate.hook_error(
                GATE, f"invalid {EXTRA_TEST_VAR} pattern: {raw[:80]}")
    return pats


def evidence(cwd, tools):
    """Facts about this turn that no amount of confident prose can fake.

    This is the part limpet does not do. A rule like "don't claim done
    without running the tests" is only checkable if something independent
    says whether tests actually ran and what they returned."""
    pats = test_patterns()
    runs = [t for t in tools
            if t.startswith(SHELL_TOOLS) and any(p.search(t) for p in pats)]
    ev = {
        "test commands run this turn": runs,
        "any test command failed": any(t.endswith("-> error") for t in runs),
        "no test command was run this turn": not runs,
    }
    if cwd and os.path.isdir(cwd):
        # Run together, not one after the other: each _git() call has its
        # own 5s timeout, so run sequentially they could cost up to 10s of
        # the gate's own ~1s of real pre-work slack (see BUDGET_MS above).
        # Concurrently, the worst case is bounded by the slower of the two,
        # not their sum. Round 7 follow-up.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_status = pool.submit(_git, cwd, "status", "--porcelain")
            f_diff = pool.submit(_git, cwd, "diff", "--stat", "HEAD")
            status, diff = f_status.result(), f_diff.result()
    else:
        no_cwd = jevgate.reading(jevgate.READ_NA, detail="no working directory given")
        status = diff = no_cwd
    ev["uncommitted changes (git status --porcelain)"] = (
        jevgate.render_reading(status) or "")[:jevgate.MAXLEN]
    ev["diff against HEAD (git diff --stat)"] = (
        jevgate.render_reading(diff) or "")[:jevgate.MAXLEN]
    return ev


def enabled():
    """True only when the operator has switched Gate 7 on."""
    return (os.environ.get(ENABLE_VAR) or "").strip().lower() in ON_VALUES


def _one_location(result):
    for loc in result.get("locations") or []:
        phys = loc.get("physicalLocation") or {}
        uri = (phys.get("artifactLocation") or {}).get("uri")
        if not uri:
            continue
        line = (phys.get("region") or {}).get("startLine")
        return f" at {uri}:{line}" if line else f" at {uri}"
    return ""


def prior_findings(session_id):
    """A tri-state reading of what earlier gates recorded this session.

    This is the P2 mechanism. Without it Gate 7 starts from zero every time
    and can pass a task Gate 6 has already flagged, because it cannot see
    Gate 6's output.

    These count as observed evidence, not as the stopping agent's prose:
    another gate wrote them from tool output before this turn ended, so the
    subject of the judgement did not choose these words. That is why they go
    in the hard-evidence block, where a rule scored over them may block.

    Returns a jevgate.reading(): READ_OK with the lines (possibly empty, a
    real "nothing recorded") on success, READ_ERR on a failed read. A
    caller that only checked truthiness could not tell a genuinely clean
    session apart from a store it could not read - the same shape
    unmeasured-as-confident-negative bug this project spent today fixing
    elsewhere. Found here by independent review, in the one place that
    fix's own session did not reach."""
    try:
        rows = findings.read(session_id)
    except Exception as exc:  # noqa: BLE001  a gate must never die reading a store
        jevgate.hook_error(GATE, "could not read the finding store")
        return jevgate.reading(jevgate.READ_ERR, value=[],
                               detail=f"could not read the finding "
                                      f"store: {exc}")
    lines = [
        f"[{r.get('level')}] {r.get('ruleId')}{_one_location(r)}: "
        f"{(r.get('message') or {}).get('text')} "
        f"(gate {(r.get('properties') or {}).get('gate')})"
        for r in rows[-FINDINGS_MAX:]
    ]
    return jevgate.reading(jevgate.READ_OK, value=lines)


def build_state(context, last, tools, ev):
    return {
        "previous messages (newest first)": context,
        "tool calls this turn (oldest first)": tools or [],
        "hard evidence from the working tree": ev,
        "agent's final message before stopping": last,
    }


# --- the hook -----------------------------------------------------------

def main():
    if not enabled():
        return 0  # off means off, before anything else happens
    budget = jevgate.Budget(BUDGET_MS)
    hook = json.load(sys.stdin)
    # Stop sends stop_hook_active. SubagentStop is not documented to use
    # the same key, so match any *_hook_active flag rather than guess one.
    if any(v for k, v in hook.items() if k.endswith("hook_active")):
        return 0  # already pushed back once; do not loop
    key = jevgate.api_key()
    if not key:
        return 0
    tagged = jevgate.load_rules_with_class(RULES)
    rules = [r for _, r in tagged]
    klass = dict((r, c) for c, r in tagged)
    if not rules:
        return 0

    context, last, tools = [], None, []
    tp = jevgate.safe_transcript(hook.get("transcript_path"))
    if tp and os.path.exists(tp):
        context, last, tools = jevgate.read_tail(tp)
    last = (hook.get("last_assistant_message") or last or "")[:jevgate.MAXLEN]
    if not last:
        return 0

    # Same threshold as the final check below, applied before spending any
    # of the budget on evidence()'s git subprocesses too, not just before
    # the Jev call. This does NOT add a stricter requirement - adding
    # evidence()'s own worst case on top would exceed BUDGET_MS entirely
    # and switch the gate off outright, which is exactly the bug this
    # replaced. It only means a budget that is already short before
    # evidence() even runs (e.g. an unusually large transcript read) skips
    # without also paying for git calls whose result would be discarded by
    # the same check afterward anyway. Round 7 follow-up.
    pre_required_ms = jevgate.CALL_WORST_MS + jevgate.CALL_MARGIN_MS
    if budget.left() * 1000 < pre_required_ms:
        jevgate.hook_error(
            GATE, f"not enough time budget left to attempt evidence "
                 f"gathering and a jev call safely ({budget.left():.1f}s "
                 f"left, {pre_required_ms / 1000:.1f}s needed); allowed")
        return 0

    ev = evidence(hook.get("cwd"), tools)
    prior = prior_findings(hook.get("session_id"))
    if prior["state"] == jevgate.READ_OK:
        if prior["value"]:
            ev["findings recorded by earlier gates this session"] = prior["value"]
    else:
        # A failed read must render as visibly failed, never as the same
        # silence a clean session produces.
        ev["findings recorded by earlier gates this session"] = (
            jevgate.render_reading(prior))
    required_ms = jevgate.CALL_WORST_MS + jevgate.CALL_MARGIN_MS
    if budget.left() * 1000 < required_ms:
        # Not "has the budget fully run out" but "is there enough of it
        # left for call_jev's own worst case (27s) to finish inside it".
        # The old check only asked the first question: reading the
        # transcript and running evidence()'s two git subprocess calls
        # (5s timeout each) could plausibly eat ~10s, leaving the budget
        # technically not-yet-expired with ~14s left, still well short of
        # what the call itself might need. Starting it anyway risked
        # exactly the outcome this check exists to prevent - the harness
        # killing the hook mid-flight, which looks identical to a clean
        # allow. Found by independent review as the sibling of Gate 3's
        # near-identical bug (round six finding 6): Gate 3's check was a
        # no-op outright, this one worked some of the time but was not
        # strict enough. See reference/DESIGN-BASIS.md.
        jevgate.hook_error(
            GATE, f"not enough time budget left to attempt a jev call "
                 f"safely ({budget.left():.1f}s left, "
                 f"{required_ms / 1000:.1f}s needed); allowed")
        return 0
    session_id = hook.get("session_id")
    if not jevgate.try_charge_session_call(session_id):
        # P6: fail-open, matching every other degraded path in this gate -
        # a spent pipeline-wide budget is not evidence the stop is
        # premature, so this gate has nothing to add rather than a guess.
        jevgate.hook_error(
            GATE, f"session-wide jev call budget spent "
                 f"({jevgate.session_calls_used(session_id)} calls this "
                 f"session); allowed")
        return 0
    t0 = time.time()
    try:
        res = jevgate.call_jev(
            build_state(context, last, tools, ev),
            jevgate.noul_questions(rules, ASK, TRUE_MEANS, FALSE_MEANS), key)
        jevgate.mark_jev_reachable(session_id)
        answers = res.get("answers") or {}
    except Exception as e:  # noqa: BLE001  fail open, always
        jevgate.mark_jev_unreachable(session_id, GATE, str(e))
        jevgate.hook_error(GATE, f"jev call failed: {e}")
        res, answers = {"error": str(e)[:300]}, {}

    probs = jevgate.probs_from(answers, rules)
    block_spec = spec_for("GATE7_BLOCK", "block", DEFAULT_BLOCK)
    warn_spec = spec_for("GATE7_WARN", "warn", DEFAULT_WARN)

    # Only an "observed" rule may block. A "stated" rule is scored over text
    # the stopping agent wrote, and the vendor documents that such text can
    # move the answer, so it is recorded and never acted on. See OBSERVED in
    # lib/jevgate.py.
    hits, warns = [], []
    for r, p in probs.items():
        if p is None:
            continue
        if klass.get(r) == jevgate.OBSERVED and p >= jevgate.threshold_for(r, block_spec):
            hits.append((r, p))
        elif p >= jevgate.threshold_for(r, warn_spec):
            warns.append((r, p))

    record = jevgate.base_record(GATE, hook, res, t0)
    record.update({"probs": probs, "blocked": [r for r, _ in hits],
                   "warned": [r for r, _ in warns],
                   "classes": klass,
                   "block_default": DEFAULT_BLOCK, "warn_default": DEFAULT_WARN,
                   "block_spec": block_spec[1], "warn_spec": warn_spec[1],
                   "n_tools": len(tools),
                   "prior_findings": len(prior["value"]),
                   "prior_findings_state": prior["state"],
                   "tests_run": ev["test commands run this turn"],
                   "assistant_text": last[:500]})
    jevgate.log(record, "GATE7_LOG", LOG_DEFAULT)

    if warns and not hits:
        print("Gate 7 (completion check): recorded, not blocking: "
              + "; ".join(f'"{r}" ({p:.0%})' for r, p in warns)
              + ". These rules are scored over the agent's own words, so they "
              "warn only.", file=sys.stderr)

    if hits:
        detail = "; ".join(f'"{r}" ({p:.0%})' for r, p in hits)
        print("Gate 7 (completion check): this stop looks premature: "
              + detail + ". Finish the work, or say in one line why the rule "
              "does not apply here, then stop.", file=sys.stderr)
        return 2  # exit 2 + stderr is the form Claude Code acts on
    return 0


# --- offline self-check -------------------------------------------------

def selfcheck():
    """Checks for the gate-specific logic. Shared plumbing is covered by
    `python lib/jevgate.py`, which this runs first."""
    jevgate.selfcheck()

    # A PowerShell-tool test run is a test run. A non-shell tool whose
    # argument happens to look like one is not.
    ev_ps = evidence(None, [
        "PowerShell: python skills/completion-check/scripts/eval_gate7.py -> ok"])
    assert ev_ps["no test command was run this turn"] is False, ev_ps
    ev_rd = evidence(None, ["Read: tests/eval_gate7.py pytest"])
    assert ev_rd["no test command was run this turn"] is True, ev_rd

    # Tuned thresholds: used when no env override, env wins when set, and
    # a missing, corrupt or out-of-range file falls back to the default.
    tuned = os.path.join(tempfile.mkdtemp(prefix="g7tuned-"), "t.json")
    with open(tuned, "w", encoding="utf-8") as f:
        json.dump({"block": {"rule a": 0.8, "rule b": 1.5, "rule c": True},
                   "warn": {"rule a": 0.4}}, f)
    with unittest.mock.patch.dict(os.environ, {"GATE7_TUNED": tuned}):
        os.environ.pop("GATE7_BLOCK", None)
        spec = spec_for("GATE7_BLOCK", "block", 0.75)
        assert spec == (0.75, {"rule a": 0.8}), spec
        assert jevgate.threshold_for("rule a", spec) == 0.8
        assert jevgate.threshold_for("rule z", spec) == 0.75
        with unittest.mock.patch.dict(os.environ, {"GATE7_BLOCK": "0.9"}):
            assert spec_for("GATE7_BLOCK", "block", 0.75) == (0.9, {})
    with open(tuned, "w", encoding="utf-8") as f:
        f.write("{not json")
    with unittest.mock.patch.dict(os.environ, {"GATE7_TUNED": tuned}):
        assert spec_for("GATE7_WARN", "warn", 0.5) == (0.5, {})
    with unittest.mock.patch.dict(os.environ, {"GATE7_TUNED": tuned + ".x"}):
        assert spec_for("GATE7_WARN", "warn", 0.5) == (0.5, {})

    tools = ["Bash: pytest -q -> error"]
    ev = evidence(None, tools)
    assert ev["any test command failed"] is True, ev
    assert ev["no test command was run this turn"] is False, ev

    # A turn with no test command at all must be visible as such.
    ev2 = evidence(None, ["Read: README.md -> ok"])
    assert ev2["no test command was run this turn"] is True, ev2
    assert ev2["any test command failed"] is False, ev2

    # A git read that could not be taken must never render as "", the exact
    # text a clean, checked working tree also produces. This was the class
    # of bug review kept finding: an unmeasured/denied/timed-out read
    # collapsing into a value that reads as a confident "no problem here".
    git_keys = ("uncommitted changes (git status --porcelain)",
                "diff against HEAD (git diff --stat)")

    ev3 = evidence(None, [])  # no cwd given at all
    for key in git_keys:
        assert ev3[key].startswith("[unmeasured]"), (key, ev3[key])

    with tempfile.TemporaryDirectory() as not_a_repo:
        ev4 = evidence(not_a_repo, [])  # a real dir, but not a git repo
        for key in git_keys:
            assert ev4[key].startswith("[unmeasured]"), (key, ev4[key])

    with unittest.mock.patch("subprocess.run",
                              side_effect=OSError("git not found")):
        r = _git(".", "status")
        assert r["state"] == jevgate.READ_ERR, r
        assert "git not found" in r["detail"], r

    # The good path must still render exactly as before: a clean tree is a
    # real, checked "", not the tag any failure now carries.
    good = jevgate.reading(jevgate.READ_OK, value="")
    assert jevgate.render_reading(good) == "", good

    # This project's own checks must count as checks. rules.md names
    # `python lib/jevgate.py` as a clear pass for the tests-not-run rule,
    # and the detector used to miss every one of these, which made the
    # gate block on an turn that had just run the whole suite.
    for cmd in ("Bash: python skills/command-safety/scripts/"
                "command_safety.py --selfcheck -> ok",
                "Bash: python skills/command-safety/scripts/eval_gate3.py -> ok"):
        ev3 = evidence(None, [cmd])
        assert ev3["no test command was run this turn"] is False, cmd

    # Naming a test file is not running one. These were bare word stems, so
    # merely inspecting, diffing or committing a file called eval_*.py, or
    # writing the word selfcheck in a commit message, registered as a test
    # run and disarmed the rule that blocks a premature completion claim.
    for cmd in ("Bash: git diff skills/command-safety/scripts/eval_gate3.py -> ok",
                "Bash: cat skills/command-safety/scripts/eval_gate3.py -> ok",
                "Bash: git commit -m \"fix selfcheck wording\" -> ok",
                "Bash: ls evals/ -> ok",
                "Read: skills/command-safety/scripts/eval_gate3.py",
                # Round five: naming the FLAG is not running the check
                # either. These matched when the flag stood unanchored.
                "Bash: git commit -m \"update --selfcheck docs\" -> ok",
                "Bash: curl https://example.com?arg=--selfcheck -> ok",
                "Bash: grep -rn -- --selfcheck README.md -> ok"):
        evx = evidence(None, [cmd])
        assert evx["no test command was run this turn"] is True, cmd

    # ...while a real invocation of the check still counts, including one
    # the transcript truncated in the middle.
    for cmd in ("Bash: python skills/command-safety/scripts/command_safety.py"
                " --selfcheck -> ok",
                "Bash: python skills/completion-check/scripts/"
                "completion_check.py [...] --selfcheck -> ok",
                # Round five finding 3: a flag between the interpreter and
                # the script, and a quoted path with a space in it, are
                # ordinary ways to run this eval and both read as no test.
                "Bash: python -u skills/command-safety/scripts/eval_gate3.py"
                " -> ok",
                "Bash: python \"my tests/eval_gate3.py\" -> ok",
                "Bash: py.test -> ok",
                "Bash: python -m pytest -q -> ok",
                # Round six finding 1: a flag before the script, for the
                # --selfcheck alternative specifically, not just eval_\w+.
                "Bash: python -u skills/command-safety/scripts/"
                "command_safety.py --selfcheck -> ok"):
        evy = evidence(None, [cmd])
        assert evy["no test command was run this turn"] is False, cmd

    # Round six finding 1: the --selfcheck alternative's gap could cross a
    # command separator, so mentioning the flag in an unrelated chained
    # command disarmed the rule exactly as the unanchored flag used to.
    for cmd in ("Bash: python migrate.py && rm -rf / ; # --selfcheck -> ok",
                "Bash: python setup.py && echo done && grep --selfcheck x -> ok"):
        evz = evidence(None, [cmd])
        assert evz["no test command was run this turn"] is True, cmd

    # The gap must not reach across a command separator, or a python call
    # in one command would vouch for an eval file merely named in the next.
    assert evidence(None, ["Bash: python app.py && cat eval_gate3.py -> ok"]
                    )["no test command was run this turn"] is True

    # A project may add a convention, and may not suppress a built-in one.
    os.environ[EXTRA_TEST_VAR] = r"python\s+lib/\w+\.py" + "\n(((broken"
    # The broken pattern is meant to be rejected, and rejecting it writes a
    # line to the hook error log. Aim that at a throwaway file so a
    # self-check fixture does not look like a real fault in the operator's
    # log, and own the file here rather than leaving it for another block.
    saved_errlog = os.environ.get("JEV_HOOK_ERRORS")
    throwaway = os.path.join(tempfile.gettempdir(), "gate7-selfcheck-errors.log")
    os.environ["JEV_HOOK_ERRORS"] = throwaway
    try:
        ev4 = evidence(None, ["Bash: python lib/jevgate.py -> ok"])
        assert ev4["no test command was run this turn"] is False, ev4
        # A broken pattern is skipped, never fatal, and never disables the
        # built-ins alongside it.
        ev5 = evidence(None, ["Bash: pytest -q -> ok"])
        assert ev5["no test command was run this turn"] is False, ev5
    finally:
        del os.environ[EXTRA_TEST_VAR]
        os.environ.pop("JEV_HOOK_ERRORS", None)
        if saved_errlog is not None:
            os.environ["JEV_HOOK_ERRORS"] = saved_errlog
        try:
            os.unlink(throwaway)
        except OSError:
            pass  # cleanup must never mask the assertion that got us here
    # Removing the variable must not leave the built-ins weakened.
    ev6 = evidence(None, ["Bash: python lib/jevgate.py -> ok"])
    assert ev6["no test command was run this turn"] is True, ev6

    # A passing run must not be reported as a failure.
    ev3 = evidence(None, ["Bash: npm run test -> ok"])
    assert ev3["any test command failed"] is False, ev3
    assert ev3["no test command was run this turn"] is False, ev3

    # Every shipped rule must parse, and every one must get a question.
    tagged = jevgate.load_rules_with_class(RULES)
    rules = [r for _, r in tagged]
    assert len(rules) >= 4, rules
    qs = jevgate.noul_questions(rules, ASK, TRUE_MEANS, FALSE_MEANS)
    assert len(qs) == len(rules)

    # The page carries anchor tables written as bullets. An unscoped parse
    # once swallowed them and turned 7 rules into 37, and "at least 4" did
    # not notice. Every rule is a prohibition, and at least one of each
    # class must exist or the warn/block split is not actually in force.
    assert all(r.startswith("Do not ") for r in rules), rules
    assert len(rules) <= 15, len(rules)
    classes = set(c for c, _ in tagged)
    assert classes == {jevgate.OBSERVED, jevgate.STATED}, classes

    # The switch. Only explicit on-values count, and anything else is off, so
    # a typo leaves the gate off rather than half on.
    saved = os.environ.pop(ENABLE_VAR, None)
    try:
        assert enabled() is False, "absent must mean off"
        for v in ("1", "true", "TRUE", "yes", "on", " on "):
            os.environ[ENABLE_VAR] = v
            assert enabled() is True, v
        for v in ("", "0", "false", "no", "off", "maybe", "onn"):
            os.environ[ENABLE_VAR] = v
            assert enabled() is False, v
    finally:
        os.environ.pop(ENABLE_VAR, None)
        if saved is not None:
            os.environ[ENABLE_VAR] = saved

    # Findings written by another gate must reach Gate 7. This is P2: without
    # it Gate 7 can pass a task Gate 6 already flagged.
    root = tempfile.mkdtemp(prefix="gate7-selfcheck-")
    os.environ["JEV_FINDINGS_DIR"] = root
    try:
        empty = prior_findings("sess-a")
        assert empty["state"] == jevgate.READ_OK, empty
        assert empty["value"] == [], empty
        findings.record("sess-a", [
            findings.finding(6, "unused-import", "os imported but unused",
                             level="note", path="lib/x.py", line=12),
            findings.finding(4, "secret-in-diff", "API key in staged change",
                             level="error"),
        ])
        got = prior_findings("sess-a")
        assert got["state"] == jevgate.READ_OK, got
        got = got["value"]
        assert len(got) == 2, got
        assert got[0] == ("[note] unused-import at lib/x.py:12: "
                          "os imported but unused (gate 6)"), got[0]
        assert "at " not in got[1], got[1]      # no location, none invented
        assert "[error] secret-in-diff" in got[1], got[1]
        # One session must never see another's findings.
        assert prior_findings("sess-b")["value"] == []
        # A long backlog must not eat Gate 7's budget. Asserted three ways on
        # purpose: "== FINDINGS_MAX" alone moves with the constant it is
        # meant to police, so raising the cap to a million would still pass.
        n = FINDINGS_MAX + 10
        findings.record("sess-c", [findings.finding(6, "R", f"m{i}")
                                   for i in range(n)])
        capped = prior_findings("sess-c")["value"]
        assert len(capped) < n, (len(capped), n)      # a cap exists at all
        assert len(capped) <= 50, len(capped)         # and it is a sane one
        assert len(capped) == FINDINGS_MAX, len(capped)
        # The cap must keep the newest, not the oldest: a stale finding from
        # the start of a long session is the least useful thing to keep.
        assert capped[-1].endswith(f"m{n - 1} (gate 6)"), capped[-1]

        # A failed read must render as visibly failed, distinguishable from
        # a genuinely clean session - the exact bug found by independent
        # review: both used to collapse to the same empty list. findings.py
        # itself swallows a missing directory via glob (legitimately "no
        # findings yet", not a failure), so the failure has to be forced
        # rather than found by pointing at a path that does not exist.
        with unittest.mock.patch.object(
                findings, "read", side_effect=OSError("simulated read failure")):
            broken = prior_findings("sess-a")
        assert broken["state"] == jevgate.READ_ERR, broken
        rendered = jevgate.render_reading(broken)
        assert rendered != "" and rendered not in ([], None), rendered
        assert rendered.startswith(f"[{jevgate.READ_ERR}]"), rendered
    finally:
        del os.environ["JEV_FINDINGS_DIR"]
        import glob as _glob
        for p in _glob.glob(os.path.join(root, "*", "*")):
            os.unlink(p)
        for p in _glob.glob(os.path.join(root, "*")):
            os.rmdir(p)
        os.rmdir(root)

    # A crash before the decision log must still leave a trace. Feeding the
    # hook invalid stdin exercises exactly that path. The gate must be
    # switched on for this, or the early return makes the test vacuous.
    import subprocess as sp
    errlog = os.path.join(tempfile.gettempdir(), "gate7-selfcheck-errors.log")
    if os.path.exists(errlog):
        os.unlink(errlog)
    env = dict(os.environ, JEV_HOOK_ERRORS=errlog, **{ENABLE_VAR: "1"})
    p = sp.run([sys.executable, os.path.abspath(__file__)], input="not json",
               capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.returncode  # still fails open
    assert os.path.exists(errlog), "crash left no trace"
    with open(errlog, encoding="utf-8") as fh:
        assert "unhandled" in fh.read()
    os.unlink(errlog)

    # Switched off, the same invalid stdin must exit 0 and do nothing at all.
    # A gate that still parses stdin while "off" is not off.
    env_off = dict(os.environ, JEV_HOOK_ERRORS=errlog)
    env_off.pop(ENABLE_VAR, None)
    p = sp.run([sys.executable, os.path.abspath(__file__)], input="not json",
               capture_output=True, text=True, env=env_off, timeout=60)
    assert p.returncode == 0, p.returncode
    assert not os.path.exists(errlog), "off must not reach the stdin parse"

    print("gate 7 selfcheck: ok")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001  a hook must never stop the work
        # Record it. A gate that fails open with no trace anywhere is
        # indistinguishable from a gate that is switched off, and this path
        # catches the crashes that happen before the decision log is reached.
        try:
            jevgate.hook_error(GATE, f"unhandled: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        if os.environ.get("GATE7_DEBUG"):
            import traceback
            traceback.print_exc()
        sys.exit(0)
