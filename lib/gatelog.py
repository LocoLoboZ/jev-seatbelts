#!/usr/bin/env python
"""Adjudicate gate decisions and compute the two rates that make a threshold
change measurable.

A threshold cannot be judged by looking at it. It is judged by two numbers:

    escape rate      - bad stops the gate let through. Target zero.
    false-block rate - fine stops the gate blocked. The cost that silently
                       kills a gate, because an operator learns to ignore it.

Neither is computable from the decision log alone, because the log records
what the gate decided and not whether it was right. This module adds the
missing half: a human verdict against a logged decision, and the arithmetic
over both.

The decision log is never rewritten. Verdicts go in their own append-only
file keyed by the decision id, latest verdict per id wins. Rewriting the
decision log would corrupt the very record calibration fits against, and an
append-only pair is also the only shape where a wrong verdict leaves a
trace.

    python lib/gatelog.py --list              # decisions awaiting a verdict
    python lib/gatelog.py --mark <id> <verdict> ["note"]
    python lib/gatelog.py --rates             # per rule, per gate
    python lib/gatelog.py                     # offline self-check

Verdicts:

    true_catch      the gate blocked, and the stop really was premature
    false_positive  the gate blocked, and the stop was fine
    miss            the gate allowed, and the stop really was premature
    ok              the gate allowed, and the stop was fine

Only a human writes a verdict. A gate must never adjudicate itself, and an
agent's own report of how it did inherits its optimism.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import jevgate  # noqa: E402

LOG_DEFAULT = "~/.jev-gates/gate7.jsonl"
GATE3_LOG_DEFAULT = "~/.jev-gates/gate3.jsonl"
GATE4_LOG_DEFAULT = "~/.jev-gates/gate4.jsonl"
VERDICT_DEFAULT = "~/.jev-gates/verdicts.jsonl"
VERDICTS = ("true_catch", "false_positive", "miss", "ok")

# A verdict says whether the decision was right. Which rate it feeds depends
# on what the gate actually did, so the two are kept separate rather than
# inferred from the word alone.
BLOCKED_VERDICTS = ("true_catch", "false_positive")
ALLOWED_VERDICTS = ("miss", "ok")

# Gates whose escape rate comes from a deterministic eval corpus rather than
# from production traffic. Their allows are counted for the denominator but
# are not a backlog awaiting a human verdict, because nobody can read every
# command a gate let through. Gate 7 is deliberately not here: a miss is by
# definition something it allowed, so production is the only place its
# escape rate can come from and its allows do need adjudicating. Gate 4
# joined for the same reason as Gate 3: eval_gate4.py is its own
# deterministic corpus, and nobody reads every commit a gate let through
# either.
EVAL_SCORED_GATES = (3, 4)

# Rule names whose ALLOW is a live Jev judgment call flagged for follow-up,
# not an outcome from the deterministic eval corpus EVAL_SCORED_GATES exists
# for. Found by independent review: adding gate 4 to EVAL_SCORED_GATES also
# swallowed its jev-risk-tier flag, the one allow phase 2 exists to raise
# for a human - it was reaching gate4.jsonl but never --list. Threshold
# calibration for RISK_THRESHOLD (commit_screening.py) needs these
# adjudicated the same way Gate 7's misses are, so they must not be
# eval-scored away.
NEEDS_VERDICT_RULES = {"jev-risk-tier"}


def _path(env_var, default):
    return os.environ.get(env_var) or os.path.expanduser(default)


read_jsonl = jevgate.read_jsonl


def latest_verdicts(rows):
    """{decision id: row}, last write wins."""
    out = {}
    for r in rows:
        if r.get("id"):
            out[r["id"]] = r
    return out


class StoreUnreadable(Exception):
    """A store file exists but this account cannot open it."""


class StoreUnwritable(Exception):
    """A row did not reach the store, and the caller must not be told it did."""


# Kept so an older caller or dispatch referring to the first name still
# resolves. The fault was never specific to verdicts.
VerdictsUnreadable = StoreUnreadable


def read_store(path, what):
    """Rows, or a hard stop when the file exists and will not open.

    Absent and unreadable are opposite facts that both returned [] and both
    printed 0%. Seen in production: four real verdicts read as none, and an
    escape rate published that ignored all four.

    The first fix guarded this with os.path.exists, which is itself a
    function that converts a permission failure into False. A parent
    directory denying traverse therefore skipped the guard and landed back
    on the silent path the fix existed to close. That is the same defect
    the fix was written to repair, inside the repair. The test that proves
    it is in selfcheck.

    read_jsonl(strict=True) now does it in one open, so there is also no
    window between checking and using."""
    try:
        return jevgate.read_jsonl(path, strict=True)
    except OSError as e:
        raise StoreUnreadable(
            "%s %s: %s" % (what, path, e.strerror or e)) from e


def read_verdicts(path):
    """Verdicts, or a hard stop. See read_store."""
    return read_store(path, "verdicts")


def decisions(log_path=None):
    """Every gate's decisions, newest last.

    Each gate writes its own log, so reading one of them was reading one
    gate. Gate 3's decisions were landing in gate3.jsonl and never reaching
    the adjudicator they exist for, which is the whole of calibration
    silently applying to Gate 7 alone. Gate 4 had the identical gap: its
    own log path was never added here, so its decisions were invisible to
    --rates however long the gate ran."""
    if log_path:
        paths = [log_path]
    else:
        paths = [_path("GATE7_LOG", LOG_DEFAULT),
                 _path("GATE3_LOG", GATE3_LOG_DEFAULT),
                 _path("GATE4_LOG", GATE4_LOG_DEFAULT)]
    rows = []
    for p in paths:
        # Strict for the same reason the verdicts are. A decision log that
        # exists and will not open dropped one whole gate out of the
        # dataset and still printed a rate for the other, which is a
        # skewed number presented as a measured one.
        rows.extend(r for r in read_store(p, "decision log") if r.get("id"))
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def mark(rid, verdict, note=""):
    """Append one verdict. Returns the row written.

    There was a `path` argument here that nothing read, so a caller aiming
    verdicts at its own file was silently written to the default instead.
    Deleted rather than implemented: the destination comes from
    GATE7_VERDICTS, and one way to choose it is enough."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    row = {"id": rid, "verdict": verdict, "note": note,
           "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")}
    # append_jsonl never raises, by design, so a gate is never taken down by
    # a full disk. It reports the failure instead, and that report was being
    # discarded: --mark printed "recorded" for a verdict that never reached
    # the file. A store that claims a write it did not make is the exact
    # fault this module was just fixed for on the read side.
    if not jevgate.log(row, "GATE7_VERDICTS", VERDICT_DEFAULT):
        raise StoreUnwritable(
            "verdict not written to %s"
            % _path("GATE7_VERDICTS", VERDICT_DEFAULT))
    return row


def _rules(d):
    """The rule names a decision turned on, whichever gate wrote it.

    Gate 7 lists what it blocked in "blocked" and what it scored in
    "probs". Gate 3 records the single rule that fired in "rule". Reading
    only Gate 7's shape credited every Gate 3 decision to no rule at all,
    so the per-rule table stayed empty however much Gate 3 traffic was
    logged."""
    if d.get("rule"):
        return [d["rule"]]
    return list(d.get("blocked") or [])


def _gate(d):
    """The gate number, as a number wherever it can be one.

    A log is read back from disk, so 3 and "3" both turn up. Keyed raw
    they became two separate gates and the same gate printed twice with
    its traffic split between the halves."""
    raw = d.get("gate")
    if isinstance(raw, bool):
        return raw  # int(True) is 1, and a malformed row is not gate 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def _needs_verdict(d):
    """Could a human usefully rule on this decision?

    Every stop could. An allow could only if its gate learns about escapes
    from production, because a miss is by definition something a gate
    allowed. Gate 3 scores escapes against an offline corpus instead, and
    nobody can read every command a gate let through, so its allows are
    counted and not queued. The gate number is coerced because a log is
    read back from disk and "3" and 3 must not mean different things."""
    if _act(d) != "allow":
        return True
    # A live judgment flag stays queued even on a gate whose routine allows
    # are eval-scored away - see NEEDS_VERDICT_RULES.
    if set(_rules(d)) & NEEDS_VERDICT_RULES:
        return True
    # An unknown gate keeps its allows in the queue rather than dropping
    # them silently.
    return _gate(d) not in EVAL_SCORED_GATES


def _act(d):
    """What the gate did, in the gate's own vocabulary.

    Without this a Gate 3 deny printed as "allow" on --list, because it
    carries no "blocked" key and the old test read a missing key as "did
    not block". That is the one screen a human reads before writing a
    verdict, so it was stating the opposite of the decision."""
    if d.get("action"):
        return str(d["action"]).upper() if d["action"] != "allow" else "allow"
    return "BLOCK" if d.get("blocked") else "allow"


def rates(decs, verds):
    """Per gate and per rule counts plus the two rates.

    A decision with no verdict is counted as unreviewed and excluded from
    both rates. Treating unreviewed as correct is how a gate comes to look
    perfect while doing nothing."""
    per_gate, per_rule = {}, {}
    for d in decs:
        v = verds.get(d["id"])
        gate = _gate(d)
        g = per_gate.setdefault(gate, {"decisions": 0, "allowed": 0,
                                       "judgeable": 0,
                                       "unreviewed": 0, "reviewed": 0,
                                       **{k: 0 for k in VERDICTS}})
        g["decisions"] += 1
        # A verdict already written makes a row judgeable whatever the
        # policy says, otherwise marking an escape on an allow reported
        # "adjudicated 1 of 0", more reviewed than could be reviewed.
        if _needs_verdict(d) or v:
            g["judgeable"] += 1
        # An allow interrupted nobody, so it belongs in the denominator.
        # Read through _act, because only Gate 3 writes an "action" field
        # and testing that field alone counted every Gate 7 allow as a
        # stop, which pinned its interrupt rate at 100%.
        allowed = _act(d) == "allow"
        if allowed:
            g["allowed"] += 1
        if not v:
            if _needs_verdict(d):
                g["unreviewed"] += 1
            continue
        # A verdict is honoured whether or not the gate allowed. Skipping
        # straight past an allow discarded the one verdict that produces an
        # escape rate, while --mark still reported it as recorded.
        g["reviewed"] += 1
        verdict = v.get("verdict")
        if verdict in VERDICTS:
            g[verdict] += 1
        # Attribute a blocking verdict to the rules that did the blocking,
        # and a miss to every rule that was scored, because any of them
        # could have caught it.
        named = _rules(d)
        if d.get("rule"):
            # One rule decided it, so it owns the verdict either way.
            targets = named
        else:
            scored = list((d.get("probs") or {}).keys())
            targets = named if verdict in BLOCKED_VERDICTS else scored
        for r in targets:
            pr = per_rule.setdefault(r, {k: 0 for k in VERDICTS})
            pr[verdict] = pr.get(verdict, 0) + 1

    for g in per_gate.values():
        blocks = g["true_catch"] + g["false_positive"]
        premature = g["true_catch"] + g["miss"]
        g["false_block_rate"] = (g["false_positive"] / blocks) if blocks else None
        g["escape_rate"] = (g["miss"] / premature) if premature else None
        # How often the gate interrupted at all. Needs no verdict, so it is
        # the one number available from day one, and it is the number that
        # decides whether a gate survives contact with an operator.
        g["interrupt_rate"] = ((g["decisions"] - g["allowed"]) / g["decisions"]
                               if g["decisions"] else None)
    for r in per_rule.values():
        blocks = r["true_catch"] + r["false_positive"]
        r["false_block_rate"] = (r["false_positive"] / blocks) if blocks else None
    return per_gate, per_rule


def _fmt(x):
    return "n/a" if x is None else f"{x:.0%}"


def main(argv):
    # Ahead of the store, so the offline check stays runnable on a machine
    # whose verdicts file this account happens not to be able to open.
    if not argv or argv == ["--selfcheck"]:
        return selfcheck()

    try:
        decs = decisions()
    except StoreUnreadable as e:
        print("cannot read %s" % e, file=sys.stderr)
        return 2

    def load_verdicts():
        """Read them only where a wrong answer could be printed.

        --mark neither reads a verdict nor prints a rate, and reading the
        store up front locked the operator out of the one command that
        recovers an unreadable store. A guard that blocks its own repair
        is not a safe default."""
        return latest_verdicts(
            read_verdicts(_path("GATE7_VERDICTS", VERDICT_DEFAULT)))

    def refuse(e):
        print("cannot read %s" % e, file=sys.stderr)
        print("Refusing to print a rate that would silently ignore every "
              "verdict already written. Grant this account read access to "
              "that file, or run this as the account that owns it. "
              "--mark still works and does not need this file to be "
              "readable.", file=sys.stderr)
        return 2

    if argv[0] == "--list":
        try:
            verds = load_verdicts()
        except StoreUnreadable as e:
            return refuse(e)
        # An allow from an offline-scored gate is logged to be counted, not
        # to be judged. Queueing every one would bury the handful of stops
        # that need a human under everything that interrupted nobody.
        # "or in verds" matches what rates() counts. rates() was widened to
        # honour a verdict on a row that would not otherwise be queued, and
        # --list was not, so the two printed different totals for the same
        # word, judgeable.
        judgeable = [d for d in decs
                     if _needs_verdict(d) or d["id"] in verds]
        pending = [d for d in judgeable if d["id"] not in verds]
        if not pending:
            print(f"{len(judgeable)} decisions can take a verdict, "
                  f"none awaiting one.")
            return 0
        # Not "stops". Gate 7's allows are in this queue too, because an
        # escape it made is only visible on an allow, and calling the list
        # stops while printing rows labelled allow contradicted itself.
        print(f"{len(pending)} of {len(judgeable)} decisions await "
              f"a verdict:")
        for d in pending[-25:]:
            print(f"  {d['id']}  {d.get('ts')}  gate {d.get('gate')}"
                  f"  {_act(d)}  {', '.join(_rules(d))[:60]}")
        print(f"\nmark one:  python lib/gatelog.py --mark <id> "
              f"<{'|'.join(VERDICTS)}> \"why\"")
        return 0

    if argv[0] == "--mark":
        if len(argv) < 3:
            print("usage: --mark <id> <verdict> [note]", file=sys.stderr)
            return 2
        known = {d["id"] for d in decs}
        if argv[1] not in known:
            print(f"no decision with id {argv[1]} in the log", file=sys.stderr)
            return 2
        try:
            row = mark(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        except StoreUnwritable as e:
            print("%s - the verdict was NOT recorded" % e, file=sys.stderr)
            return 2
        print(f"recorded: {row['id']} -> {row['verdict']}")
        return 0

    if argv[0] == "--rates":
        try:
            verds = load_verdicts()
        except StoreUnreadable as e:
            return refuse(e)
        per_gate, per_rule = rates(decs, verds)
        if not per_gate:
            print("no decisions logged yet.")
            return 0
        for gate, g in sorted(per_gate.items(), key=lambda kv: str(kv[0])):
            # Two partitions, stated separately. Printing allowed and
            # unreviewed side by side implied they were exclusive, and a
            # Gate 7 allow is in both: it is in the denominator and it
            # still needs a verdict. Read as one row it looked like more
            # outcomes than there were decisions.
            print(f"gate {gate}: {g['decisions']} decisions "
                  f"= {g['allowed']} allowed + "
                  f"{g['decisions'] - g['allowed']} stopped")
            # "decisions", not "commands". Gate 7 rules on stop events, so
            # the word was wrong for half the table.
            print(f"  interrupt rate   {_fmt(g['interrupt_rate'])}   "
                  f"({g['decisions'] - g['allowed']} stopped of "
                  f"{g['decisions']} decisions)")
            print(f"  adjudicated      {g['reviewed']} of {g['judgeable']} "
                  f"that can take a verdict, {g['unreviewed']} outstanding")
            print(f"  escape rate      {_fmt(g['escape_rate'])}   "
                  f"(target 0%, {g['miss']} missed)")
            print(f"  false-block rate {_fmt(g['false_block_rate'])}   "
                  f"({g['false_positive']} of "
                  f"{g['true_catch'] + g['false_positive']} blocks)")
        if per_rule:
            print("\nper rule:")
            for r, c in sorted(per_rule.items()):
                print(f"  {_fmt(c['false_block_rate']).rjust(4)} false-block  "
                      f"{c['true_catch']}tc {c['false_positive']}fp "
                      f"{c['miss']}miss  {r[:52]}")
        # Per gate, not across all of them. A gate with no verdicts at all
        # has unknown rates whatever a different gate has been adjudicated
        # to, and an all() over both let one marked Gate 3 decision silence
        # the warning for a completely unadjudicated Gate 7.
        for gate, g in sorted(per_gate.items(), key=lambda kv: str(kv[0])):
            if g["judgeable"] and not g["reviewed"]:
                print(f"\nGate {gate} has no verdicts, so both its rates are "
                      f"unknown, not good. Mark some of its decisions before "
                      f"changing one of its thresholds.")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


def selfcheck():
    decs = [
        {"id": "a", "gate": 7, "blocked": ["R1"], "probs": {"R1": 0.9, "R2": 0.1}},
        {"id": "b", "gate": 7, "blocked": ["R1"], "probs": {"R1": 0.8, "R2": 0.1}},
        {"id": "c", "gate": 7, "blocked": [], "probs": {"R1": 0.2, "R2": 0.3}},
        {"id": "d", "gate": 7, "blocked": [], "probs": {"R1": 0.2, "R2": 0.3}},
        {"id": "e", "gate": 7, "blocked": [], "probs": {"R1": 0.1, "R2": 0.1}},
    ]
    verds = latest_verdicts([
        {"id": "a", "verdict": "true_catch"},
        {"id": "b", "verdict": "false_positive"},
        {"id": "c", "verdict": "miss"},
        {"id": "d", "verdict": "ok"},
        {"id": "b", "verdict": "true_catch"},   # a later verdict wins
    ])
    assert verds["b"]["verdict"] == "true_catch", verds["b"]

    g, per_rule = rates(decs, verds)
    s = g[7]
    assert s["decisions"] == 5, s
    assert s["unreviewed"] == 1, s          # "e" was never adjudicated
    assert s["true_catch"] == 2 and s["miss"] == 1 and s["ok"] == 1, s
    assert s["false_positive"] == 0, s
    assert s["false_block_rate"] == 0.0, s
    assert abs(s["escape_rate"] - 1 / 3) < 1e-9, s   # 1 miss of 3 premature

    # A miss is attributed to every rule that was scored, since any of them
    # could have caught it. A block is attributed only to the rule that fired.
    assert per_rule["R1"]["true_catch"] == 2, per_rule["R1"]
    assert per_rule["R2"]["true_catch"] == 0, per_rule["R2"]
    assert per_rule["R1"]["miss"] == 1 and per_rule["R2"]["miss"] == 1, per_rule

    # With nothing adjudicated, a rate must read unknown rather than perfect.
    g2, _ = rates([{"id": "z", "gate": 7, "blocked": [], "probs": {}}], {})
    assert g2[7]["escape_rate"] is None and g2[7]["false_block_rate"] is None, g2

    # A corrupt line must be skipped, not fatal.
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('{"id": "x", "verdict": "ok"}' + "\n")
            f.write("{ not json" + "\n")
            f.write('{"id": "y", "verdict": "miss"}' + "\n")
        assert len(read_jsonl(tmp)) == 2, read_jsonl(tmp)
    finally:
        os.unlink(tmp)

    # A path that exists but will not open must stop the tool rather than
    # read as zero verdicts. A directory is the portable way to get that.
    d = tempfile.mkdtemp()
    try:
        read_verdicts(d)
        raise AssertionError("an unreadable verdicts path must raise")
    except StoreUnreadable:
        pass
    finally:
        os.rmdir(d)

    # The same denial reached through a parent that refuses traverse. The
    # first version guarded on os.path.exists, which turns PermissionError
    # into False, so this case walked straight past the guard and returned
    # [] - the exact silence the guard existed to prevent. Simulated by
    # denying the open while the existence probe reports absent.
    import unittest.mock as _m
    _real_open = open

    def _denied(p, *a, **k):
        if "verdict-probe" in str(p):
            raise PermissionError(13, "Permission denied")
        return _real_open(p, *a, **k)

    with _m.patch("builtins.open", _denied), \
            _m.patch("os.path.exists", return_value=False):
        try:
            got = read_verdicts("/nowhere/verdict-probe.jsonl")
            raise AssertionError(
                "a denied open must raise even when exists() says no, "
                "got %r" % (got,))
        except StoreUnreadable:
            pass

    # Absent is still absent, and must stay silent.
    assert read_verdicts(os.path.join(tempfile.mkdtemp(), "none.jsonl")) == []

    # Gate 4's jev-risk-tier allow is a live judgment call flagged for
    # follow-up, not the deterministic eval corpus EVAL_SCORED_GATES exists
    # for - it must still reach --list, or the one flag phase 2 exists to
    # raise for calibration vanishes into a log nobody reads. Found by
    # independent review, which reproduced it.
    assert _needs_verdict(
        {"id": "r", "gate": 4, "action": "allow", "rule": "jev-risk-tier"})
    assert not _needs_verdict(
        {"id": "s", "gate": 4, "action": "allow", "rule": "resolved-safe"})
    assert not _needs_verdict(
        {"id": "t", "gate": 4, "action": "allow", "rule": "jev-judged-clean"})

    # --list and --rates must agree on what "judgeable" counts.
    _d = [{"id": "q", "gate": 3, "action": "allow", "rule": ""}]
    _v = {"q": {"id": "q", "verdict": "ok"}}
    _lst = [x for x in _d if _needs_verdict(x) or x["id"] in _v]
    _g, _ = rates(_d, _v)
    assert len(_lst) == _g[3]["reviewed"] + _g[3]["unreviewed"], (_lst, _g)

    try:
        mark("a", "looks-fine")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown verdict must be refused")

    # A write that did not land must not be reported as one. --mark printed
    # "recorded" for a verdict the store never received, because log()
    # discarded what append_jsonl told it.
    _saved = os.environ.get("GATE7_VERDICTS")
    os.environ["GATE7_VERDICTS"] = tempfile.mkdtemp()   # a directory, so the
    try:                                                # append cannot land
        mark("a", "ok", "probe")
        raise AssertionError("a failed verdict write must raise")
    except StoreUnwritable:
        pass
    finally:
        os.environ.pop("GATE7_VERDICTS", None)
        if _saved is not None:
            os.environ["GATE7_VERDICTS"] = _saved

    print("gatelog selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
