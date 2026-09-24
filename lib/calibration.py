"""Confidence calibration math, stdlib only - no third-party dependency.

Same two questions the `jevcal` project (abhixhek, MIT) answers, re-derived
here in ~50 lines because this repo's own rule (see every other file in
`lib/`) is stdlib-only: nothing a hook depends on may need a package
install, and jevcal itself pulls in pydantic + httpx for machinery
(a decision store, a backend router, a typed schema layer) this project
does not need - only its two calibration formulas do.

A "sample" everywhere in this module is (predicted_probability, is_positive):
Jev's own confidence score for "this belongs to the positive class" (a
secret is present, this touches risk-sensitive ground, ...), paired with
the actual, independently-known ground truth for that same case. This is
the standard binary calibration definition - a bin's "observed frequency"
is the fraction of its samples that were actually positive, not whether
some particular threshold happened to guess right.
"""
from __future__ import annotations


def reliability_diagram(samples, num_bins=10):
    """Bins samples by predicted probability, reports predicted-vs-observed
    frequency per bin."""
    bins = [{"sum_p": 0.0, "n_pos": 0, "n": 0} for _ in range(num_bins)]
    for p, is_positive in samples:
        idx = min(int(p * num_bins), num_bins - 1)
        b = bins[idx]
        b["sum_p"] += p
        b["n_pos"] += 1 if is_positive else 0
        b["n"] += 1

    out = []
    for idx, b in enumerate(bins):
        start, end = idx / num_bins, (idx + 1) / num_bins
        n = b["n"]
        avg_p = b["sum_p"] / n if n else (start + end) / 2
        observed = b["n_pos"] / n if n else 0.0
        out.append({"bin_start": start, "bin_end": end,
                    "avg_predicted": avg_p, "observed": observed, "n": n})
    return out


def expected_calibration_error(samples, num_bins=10):
    """ECE: sample-weighted average gap between each bin's average
    predicted probability and the actual fraction of positives in it.
    0 = perfectly calibrated, higher = the confidence numbers are not
    honest (e.g. things scored "80% likely" are not actually positive
    80% of the time)."""
    if not samples:
        return 0.0
    bins = reliability_diagram(samples, num_bins)
    total = len(samples)
    return sum((b["n"] / total) * abs(b["avg_predicted"] - b["observed"])
               for b in bins if b["n"])


def brier_score(samples):
    """Mean squared error between predicted probability and the binary
    ground truth (0/1). 0 = perfect, 0.25 = no better than always
    guessing 50%."""
    if not samples:
        return 0.0
    return sum((p - (1.0 if is_positive else 0.0)) ** 2
               for p, is_positive in samples) / len(samples)


def auroc(samples):
    """Area under the ROC curve: how well the score ranks positives above
    negatives, independent of any one threshold. 0.5 = no better than a coin
    flip, 1.0 = every positive scored above every negative. Ties count as
    half a win, the standard Mann-Whitney U treatment. Returns None when one
    class is empty - separation is undefined with only one side.

    This is what skills/completion-check/references/CALIBRATION.md's own
    procedure asks for at the "below about 0.55 the rule does not separate
    anything" step, re-derived here rather than imported for the same
    stdlib-only reason as the rest of this module."""
    pos = [p for p, is_positive in samples if is_positive]
    neg = [p for p, is_positive in samples if not is_positive]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def best_threshold(samples, candidates=None):
    """Sweeps candidate thresholds, returns (threshold, accuracy) for the
    one with the highest accuracy on these samples (predicted_probability
    >= t counts as a positive call). Ties broken toward the lower
    threshold (fewer missed positives over fewer false alarms, the safer
    default for a gate that exists to catch something risky).

    This is NOT a held-out-verified fit - with too few samples to split
    meaningfully, it is evaluated on the same set it is fit against, and
    the caller must say so. A real calibration run needs enough labelled
    history to hold out a split; this function does not pretend it has
    that when it does not."""
    if not samples:
        return None
    if candidates is None:
        candidates = [i / 100 for i in range(5, 100, 5)]
    best_t, best_acc = None, -1.0
    for t in candidates:
        correct = sum(1 for p, is_positive in samples
                     if (p >= t) == is_positive)
        acc = correct / len(samples)
        if acc > best_acc or (acc == best_acc and (best_t is None or t < best_t)):
            best_t, best_acc = t, acc
    return best_t, best_acc


def selfcheck():
    """No test file existed for this module before jevcal_calibrate.py
    started depending on it and auroc() was added for
    jevcal_calibrate_gate7.py - a bare arithmetic module is still logic
    that can be silently wrong, and both callers trust these numbers for a
    real threshold decision."""
    # A perfectly separated set: every positive scored 1.0, every negative
    # 0.0. Both error measures should read exactly zero, AUROC exactly one,
    # and the fitted threshold should land anywhere strictly between them.
    perfect = [(1.0, True), (1.0, True), (0.0, False), (0.0, False)]
    assert expected_calibration_error(perfect) == 0.0
    assert brier_score(perfect) == 0.0
    assert auroc(perfect) == 1.0
    bt, acc = best_threshold(perfect)
    assert acc == 1.0 and 0.0 < bt <= 1.0, (bt, acc)

    # A coin flip: the score carries no information at all.
    coin = [(0.5, True), (0.5, False)] * 10
    assert abs(brier_score(coin) - 0.25) < 1e-9
    assert auroc(coin) == 0.5  # every pair ties -> 0.5 by definition

    # A worse-than-random set: every positive scored below every negative.
    inverted = [(0.1, True), (0.1, True), (0.9, False), (0.9, False)]
    assert auroc(inverted) == 0.0

    # One class missing: separation is undefined, not zero or one.
    assert auroc([(0.7, True), (0.9, True)]) is None
    assert auroc([]) is None

    # Empty input must not raise, and must read as "no error" rather than
    # crash a caller that has not yet accumulated any samples.
    assert expected_calibration_error([]) == 0.0
    assert brier_score([]) == 0.0
    assert best_threshold([]) is None

    print("calibration selfcheck: ok")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selfcheck())
