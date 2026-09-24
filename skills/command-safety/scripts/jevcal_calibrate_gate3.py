#!/usr/bin/env python
"""Gate 3 threshold calibration from real usage - the jevcal pattern
(lib/calibration.py: ECE, Brier, best_threshold) extended from Gate 4 to
Gate 3, per reference/DESIGN-BASIS.md's ranked build list ("Gate 3/7
calibration", 58%).

    python skills/command-safety/scripts/jevcal_calibrate_gate3.py

This is deliberately NOT jevcal_calibrate.py's own shape. That script runs a
labelled eval corpus with an independently-known ground truth (is this a
secret? does it touch risk-sensitive ground?) live against the hook. Gate 3
has no equivalent corpus for its Jev-judged tier, on purpose - see
attack_gate3.py's own docstring: "these are probabilistic-judgment cases,
not ones with one fixed correct answer". Fabricating a fixed label for
"git push --force" or "kubectl delete pod" to feed this same eval-corpus
shape would repeat the exact class of mistake this project already caught
once (see "the Gate 5 write side, built" / "Correction ... Gate 4" in
DESIGN-BASIS.md: a wrong ground-truth label, not a Jev miscalibration).

The only honest ground truth available for this tier is a human's own
after-the-fact verdict on a real decision - exactly what lib/gatelog.py
exists to collect ("Only a human writes a verdict. A gate must never
adjudicate itself."). This script is the read side: it pairs every real
GATE3_LOG entry that reached the Jev-judged tier (now carrying `danger` and
`category`, added this session so this script could exist - see the
Verdict class in command_safety.py) with a real gatelog verdict, when one
has been marked, and reports the same ECE/Brier/best_threshold numbers
jevcal_calibrate.py reports for Gate 4 - grouped by category, because
JEV_THRESHOLDS is itself per-category, not one flat number.

Ground truth mapping, gatelog's own vocabulary (lib/gatelog.py):
  true_catch      the gate denied, and denying was right  -> actually risky
  miss            the gate allowed, and it should have denied -> actually risky
  false_positive  the gate denied, but it was fine to allow -> actually fine
  ok              the gate allowed, and that was right -> actually fine
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, *[os.pardir] * 3, "lib"))
sys.path.insert(0, HERE)
from calibration import (  # noqa: E402
    best_threshold, brier_score, expected_calibration_error, reliability_diagram,
)
import gatelog  # noqa: E402
from command_safety import JEV_THRESHOLDS  # noqa: E402

MIN_TRUSTWORTHY_N = 20  # same floor jevcal_calibrate.py uses for Gate 4

RISKY_VERDICTS = ("true_catch", "miss")
FINE_VERDICTS = ("false_positive", "ok")


def harvest():
    """Real GATE3_LOG rows that reached the Jev-judged tier and have a real
    human verdict, grouped by category. A row with no verdict yet is
    counted (so the report can say how much is still unadjudicated) but
    contributes no sample."""
    decs = [d for d in gatelog.decisions()
            if gatelog._gate(d) == 3 and d.get("danger") is not None]
    verds = gatelog.latest_verdicts(
        gatelog.read_verdicts(
            gatelog._path("GATE7_VERDICTS", gatelog.VERDICT_DEFAULT)))

    by_category = {}
    total_judged, total_verdicted = 0, 0
    for d in decs:
        total_judged += 1
        cat = d.get("category") or "unresolvable"
        by_category.setdefault(cat, []).append(d)

    samples_by_category = {}
    for cat, rows in by_category.items():
        samples = []
        for d in rows:
            v = verds.get(d["id"])
            if not v:
                continue
            verdict = v.get("verdict")
            if verdict in RISKY_VERDICTS:
                samples.append((d["danger"], True))
                total_verdicted += 1
            elif verdict in FINE_VERDICTS:
                samples.append((d["danger"], False))
                total_verdicted += 1
        samples_by_category[cat] = samples
    return samples_by_category, total_judged, total_verdicted


def report(category, current_threshold, samples):
    print(f"\n=== {category} (current JEV_THRESHOLDS: {current_threshold}) ===")
    n = len(samples)
    n_pos = sum(1 for _, is_pos in samples if is_pos)
    print(f"n={n} adjudicated samples ({n_pos} actually risky, "
         f"{n - n_pos} actually fine)")
    if n == 0:
        print("no adjudicated decisions for this category yet - nothing "
             "to fit. Mark some with `python lib/gatelog.py --mark`.")
        return
    ece = expected_calibration_error(samples, num_bins=min(5, n))
    brier = brier_score(samples)
    print(f"ECE={ece:.3f}  Brier={brier:.3f}  "
         f"(0=perfect for both, 0.25 Brier=no better than a coin flip)")
    bt, acc = best_threshold(samples)
    print(f"accuracy-maximising threshold on this sample: {bt} "
         f"(accuracy {acc:.0%})")
    if n < MIN_TRUSTWORTHY_N:
        print(f"NOT TRUSTWORTHY: n={n} < {MIN_TRUSTWORTHY_N}, evaluated on "
             f"the same set it was fit from - a direction, not a verified "
             f"calibration. See CALIBRATION.md's own MIN_TRUSTWORTHY_N "
             f"discipline (borrowed from jevcal_calibrate.py).")
    elif abs(bt - current_threshold) < 0.05:
        print("current threshold is within 0.05 of the fitted value - "
             "no change indicated.")
    else:
        print(f"current threshold ({current_threshold}) differs from the "
             f"fitted value ({bt}) by more than 0.05 - worth a deliberate "
             f"review before editing JEV_THRESHOLDS, not an automatic edit.")
    for b in reliability_diagram(samples, num_bins=min(5, n)):
        if b["n"]:
            print(f"  [{b['bin_start']:.1f}-{b['bin_end']:.1f}) "
                 f"avg_predicted={b['avg_predicted']:.2f} "
                 f"observed={b['observed']:.2f} n={b['n']}")


def main():
    print("Gate 3 threshold calibration (real GATE3_LOG decisions + real "
         "gatelog verdicts - no synthetic ground truth, see this script's "
         "own docstring for why)")
    samples_by_category, total_judged, total_verdicted = harvest()
    if total_judged == 0:
        print("\nno Jev-judged decisions found in GATE3_LOG at all - "
             "nothing to calibrate against yet.")
        return 0
    print(f"\n{total_judged} Jev-judged decisions on record, "
         f"{total_verdicted} carry a usable human verdict.")
    for cat in sorted(set(JEV_THRESHOLDS) | set(samples_by_category)):
        if cat == "DEFAULT":
            continue
        report(cat, JEV_THRESHOLDS.get(cat, JEV_THRESHOLDS["DEFAULT"]),
               samples_by_category.get(cat, []))
    if total_verdicted < MIN_TRUSTWORTHY_N:
        print(f"\nOverall: {total_verdicted} verdicted samples across all "
             f"categories, below the {MIN_TRUSTWORTHY_N}-sample trust "
             f"floor even pooled. Mark more decisions with "
             f"`python lib/gatelog.py --mark <id> <verdict> \"why\"` "
             f"(see `python lib/gatelog.py --list`) before treating any "
             f"fitted number above as more than a direction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
