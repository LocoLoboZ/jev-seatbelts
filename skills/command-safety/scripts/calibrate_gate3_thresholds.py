#!/usr/bin/env python
"""Build Gate 3's per-category deny threshold from Jev, once, not per-hook.

    python skills/command-safety/scripts/calibrate_gate3_thresholds.py

One flat JEV_DENY_AT for every category (the pre-calibration state, see
reference/DESIGN-BASIS.md) treats "git push --force" - usually routine solo
work - the same as "DROP TABLE" - rarely routine and much costlier to get
wrong. This asks Jev, once, for a base-rate read per category: how often is
a real instance of this situation ordinary work rather than something that
warrants caution? That probability is converted into a deny threshold with
a documented, linear formula, not asked for as a bare number, because this
project's plumbing only has a proven "noul" (probability of a stated
criterion) question type - see lib/jevgate.py - and guessing at an unproven
question-type schema against the live, metered API was judged the wrong
place to take that risk.

This is a design-time calibration run, not something Gate 3 calls per
command: one batched POST covers every category, and the result is meant to
be read off this script's output and hard-coded into JEV_THRESHOLDS in
command_safety.py, cited with the run's timestamp. Re-run by hand when the
category list changes or a recalibration is wanted; nothing in the hook
path depends on this script executing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
import jevgate  # noqa: E402

# One entry per category Gate 3's Jev-judged tier can route to today: the
# family rule names from command_safety.py, plus "unresolvable" for every
# structural case (an expansion, a wrapper, code on the command line, and
# the rest) that sets `reason` instead of a family verdict. "project-rule"
# is deliberately absent: it is arbitrary, operator-supplied regex with no
# fixed meaning to calibrate against, and keeps a conservative fixed
# default instead - see JEV_THRESHOLDS's DEFAULT entry.
CATEGORIES = {
    "delete-unresolved-target":
        "A recursive delete (`rm -r`/`rm -R`) whose target path is not a "
        "plain literal string - built from a variable or expansion - "
        "attempted by an autonomous coding agent in a real development "
        "session",
    "delete-recursive":
        "A recursive delete (`rm -r`/`rm -R`) of a literal, named "
        "directory that is not a routine build-output directory (not "
        "node_modules, dist, .next, and the like), attempted by an "
        "autonomous coding agent in a real development session",
    "git-force-push":
        "A `git push --force` (or `-f`, not `--force-with-lease`), "
        "attempted by an autonomous coding agent in a real development "
        "session",
    "git-reset-hard":
        "A `git reset --hard`, which discards uncommitted work, attempted "
        "by an autonomous coding agent in a real development session",
    "git-discard-worktree":
        "A `git checkout .` or `git restore .`, which discards every "
        "uncommitted change in the working tree, attempted by an "
        "autonomous coding agent in a real development session",
    "git-clean":
        "A `git clean -f`, which permanently deletes untracked files, "
        "attempted by an autonomous coding agent in a real development "
        "session",
    "git-rewrite-history":
        "A `git filter-branch`, which rewrites every commit in the "
        "repository's history, attempted by an autonomous coding agent in "
        "a real development session",
    "kubectl-delete":
        "A `kubectl delete` against a live Kubernetes cluster resource, "
        "attempted by an autonomous coding agent in a real development "
        "session",
    "container-prune":
        "A `docker`/`podman` prune, which removes more resources than the "
        "ones named, attempted by an autonomous coding agent in a real "
        "development session",
    "container-force-remove":
        "A force-remove of a container, image, or volume (`docker rm -f`, "
        "`docker volume rm`), attempted by an autonomous coding agent in "
        "a real development session",
    "remote-code-to-shell":
        "Output fetched from the internet (`curl`/`wget`) being piped "
        "directly into a shell interpreter, attempted by an autonomous "
        "coding agent in a real development session",
    "sql-destructive":
        "A `DROP TABLE` or `TRUNCATE TABLE` statement run through a "
        "database client, attempted by an autonomous coding agent in a "
        "real development session",
    "unresolvable":
        "A shell command whose exact behaviour a deterministic parser "
        "could not fully pin down - for example, a word built at run "
        "time, code handed directly to an interpreter on the command "
        "line, or the real command being read from another command's "
        "input - attempted by an autonomous coding agent in a real "
        "development session",
}

ASK = ("Among real-world instances of the situation described, what "
       "fraction are ordinary, intended development work rather than "
       "something that should give a human operator real pause?")


def to_threshold(p_ordinary):
    """The documented formula, not a black box.

    p_ordinary=0 (this category is essentially never routine) -> 0.40: a
    lower bar to deny, because being cautious here costs little relative
    to what a miss costs. p_ordinary=1 (essentially always routine) ->
    0.80: a higher bar, because most instances are fine and a low bar
    would turn this into a nuisance. Bounded well short of the extremes on
    both ends deliberately: 0.40 still requires the model to lean toward
    "harmful" before denying, and 0.80 still denies a genuinely clear case,
    so neither end degenerates into "always deny this category" or "never
    deny this category" regardless of what Jev sees on the command itself."""
    return round(0.40 + 0.40 * max(0.0, min(1.0, p_ordinary)), 2)


def main():
    key = jevgate.api_key()
    if not key:
        print("No Jev key found (env var or Windows registry). "
              "Calibration needs a live call and cannot run without one.",
              file=sys.stderr)
        return 1

    names = list(CATEGORIES)
    questions = {
        f"c{i}": {
            "type": "noul",
            "instructions": f"{ASK} Situation: {CATEGORIES[name]}",
            "criteria": {
                "true": "this situation is usually ordinary, intended work",
                "false": "this situation usually warrants real caution, "
                         "even short of outright malice",
            },
        }
        for i, name in enumerate(names)
    }
    res = jevgate.call_jev({}, questions, key)
    answers = res.get("answers") or {}

    table = {}
    print(f"{'category':<26} {'p_ordinary':>10} {'deny_at':>8}")
    for i, name in enumerate(names):
        p = (answers.get(f"c{i}") or {}).get("noul")
        if not isinstance(p, (int, float)):
            print(f"{name:<26} {'(no answer)':>10}", file=sys.stderr)
            continue
        deny_at = to_threshold(float(p))
        table[name] = deny_at
        print(f"{name:<26} {p:>10.2f} {deny_at:>8.2f}")

    table["DEFAULT"] = 0.50  # project-rule and anything not listed above
    print("\nJEV_THRESHOLDS = " + json.dumps(table, indent=4))
    return 0


if __name__ == "__main__":
    sys.exit(main())
