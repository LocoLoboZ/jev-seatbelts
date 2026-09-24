#!/usr/bin/env python
"""Gate 7 threshold calibration from real usage - the jevcal pattern
(lib/calibration.py: ECE, Brier, best_threshold, auroc) extended from
Gate 4 to Gate 7, per reference/DESIGN-BASIS.md's ranked build list
("Gate 3/7 calibration", 58%), and per this gate's own
references/CALIBRATION.md, which the gate's own rules.md says is "not yet
done".

    python skills/completion-check/scripts/jevcal_calibrate_gate7.py

CALIBRATION.md's full procedure additionally describes harvesting past
stops straight out of ~/.claude/projects/*/*.jsonl and having Jev label
each one's human reaction (push/correction/question/new_request/ack) as an
automated substitute for a human verdict. That harvest-and-auto-label step
is NOT built here - it is a second, larger, separate mechanism, and this
project already has a smaller, more direct one already collecting real
labels: lib/gatelog.py's own verdict store, filled by an actual human
judging an actual past decision ("Only a human writes a verdict. A gate
must never adjudicate itself."). This script is the read side of that
store for Gate 7, mirroring jevcal_calibrate.py's shape for Gate 4 as
closely as the two gates' own logged shapes allow.

Per-rule attribution mirrors lib/gatelog.py's own `rates()` exactly, not a
new invented rule: a BLOCK verdict (true_catch/false_positive) is
attributed only to the rule(s) actually named in `blocked`, because only
those rules made the call this decision. An ALLOW verdict (miss/ok) is
attributed to every rule Gate 7 scored that turn (`probs`), because any of
them could have caught it and there is no way to know which one "should"
have from an allow alone.

Fit target differs by evidence class (see rules.md, "Evidence class"):
  [observed] rules -> GATE7_BLOCK, threshold = 95th percentile of good
    scores (CALIBRATION.md step 5), reported alongside the accuracy-
    maximising fit from lib/calibration.py for comparison.
  [stated] rules -> GATE7_WARN only, since a stated rule can never block -
    fitting a block threshold for one is meaningless (CALIBRATION.md,
    "Two tiers, and which rules get which").

AUROC gates every fit: CALIBRATION.md says a rule below ~0.55 does not
separate anything and should stay in shadow mode rather than be given a
number that implies it discriminates.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, *[os.pardir] * 3, "lib"))
sys.path.insert(0, HERE)
from calibration import auroc, best_threshold, brier_score  # noqa: E402
import gatelog  # noqa: E402
import jevgate  # noqa: E402

RULES = os.path.join(HERE, os.pardir, "references", "rules.md")
MIN_TRUSTWORTHY_N = 20  # same floor jevcal_calibrate.py uses for Gate 4
RISKY_VERDICTS = ("true_catch", "miss")
FINE_VERDICTS = ("false_positive", "ok")


def _percentile(values, pct):
    """Linear-interpolated percentile, stdlib only. pct in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (pct / 100) * (len(s) - 1)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def harvest():
    """{rule text: [(score, is_bad_stop), ...]} for every Gate 7 decision
    with a real gatelog verdict, attributed the same way gatelog.rates()
    already attributes a verdict to a rule."""
    decs = [d for d in gatelog.decisions() if gatelog._gate(d) == 7]
    verds = gatelog.latest_verdicts(
        gatelog.read_verdicts(
            gatelog._path("GATE7_VERDICTS", gatelog.VERDICT_DEFAULT)))

    by_rule = {}
    total_verdicted = 0
    for d in decs:
        v = verds.get(d["id"])
        if not v:
            continue
        verdict = v.get("verdict")
        probs = d.get("probs") or {}
        blocked = d.get("blocked") or []
        is_bad = verdict in RISKY_VERDICTS
        is_good = verdict in FINE_VERDICTS
        if not (is_bad or is_good):
            continue
        targets = blocked if verdict in ("true_catch", "false_positive") \
            else list(probs.keys())
        counted = False
        for r in targets:
            p = probs.get(r)
            if not isinstance(p, (int, float)):
                continue
            by_rule.setdefault(r, []).append((float(p), is_bad))
            counted = True
        if counted:
            total_verdicted += 1
    return by_rule, total_verdicted


def report(rule_text, evidence_class, samples):
    print(f"\n=== [{evidence_class}] {rule_text[:70]} ===")
    n = len(samples)
    n_bad = sum(1 for _, bad in samples if bad)
    print(f"n={n} adjudicated samples ({n_bad} bad stops, {n - n_bad} fine)")
    if n == 0:
        print("no adjudicated decisions for this rule yet - mark some with "
             "`python lib/gatelog.py --mark`.")
        return
    auc = auroc(samples)
    brier = brier_score(samples)
    print(f"AUROC={auc if auc is None else f'{auc:.3f}'}  Brier={brier:.3f}")
    if auc is not None and auc < 0.55:
        print("AUROC below 0.55 - this rule does not separate bad stops "
             "from fine ones on this sample. Per CALIBRATION.md: leave it "
             "in shadow mode and reword it rather than fitting a number.")
    good_scores = [p for p, bad in samples if not bad]
    p95 = _percentile(good_scores, 95)
    bt, acc = best_threshold(samples)
    field = "GATE7_BLOCK" if evidence_class == jevgate.OBSERVED else "GATE7_WARN"
    print(f"95th-percentile-of-good threshold (CALIBRATION.md step 5): "
         f"{p95 if p95 is None else round(p95, 2)}  "
         f"-> would set {field}")
    print(f"accuracy-maximising threshold (lib/calibration.py): {bt} "
         f"(accuracy {acc:.0%})" if bt is not None else "")
    if n < MIN_TRUSTWORTHY_N:
        print(f"NOT TRUSTWORTHY: n={n} < {MIN_TRUSTWORTHY_N}, no held-out "
             f"split possible at this sample size. A direction, not a "
             f"verified calibration - mark more decisions before setting "
             f"{field} from this number.")


def main():
    print("Gate 7 threshold calibration (real GATE7_LOG decisions + real "
         "gatelog verdicts, per-rule, per CALIBRATION.md)")
    tagged = jevgate.load_rules_with_class(RULES)
    klass = dict((r, c) for c, r in tagged)
    by_rule, total_verdicted = harvest()
    if not by_rule:
        print("\nno adjudicated Gate 7 decisions found at all - "
             "`python lib/gatelog.py --list` to see what is awaiting a "
             "verdict, then --mark some before this can report anything.")
        return 0
    print(f"\n{total_verdicted} verdicted Gate 7 decisions on record "
         f"(attributed across {len(by_rule)} rule(s) that were scored).")
    for _, rule_text in tagged:
        report(rule_text, klass.get(rule_text, jevgate.STATED),
               by_rule.get(rule_text, []))
    if total_verdicted < MIN_TRUSTWORTHY_N:
        print(f"\nOverall: {total_verdicted} verdicted decisions, below "
             f"the {MIN_TRUSTWORTHY_N}-sample trust floor even pooled "
             f"across rules. Mark more with "
             f"`python lib/gatelog.py --mark <id> <verdict> \"why\"` "
             f"(see `python lib/gatelog.py --list`) before setting "
             f"GATE7_BLOCK/GATE7_WARN from any number above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
