---
name: code-quality
description: >
  Gate 6. Runs before a `git commit`, on the same staged diff Gate 4
  screens for secrets. Measures the diff against seven fixed rules from
  `reference/PIPELINE-RESEARCH.md` - five deterministic, one Jev (depth),
  one hybrid (interface-as-test-surface). Never denies or asks: this is a
  measurement gate, not a safety gate, and it has no standing to block a
  local, reversible commit. Writes one SARIF finding per result to the
  shared store (only when the hook input carries a session id) and flags
  only a `strong` finding via
  `additionalContext`. Before running any rule (deterministic or Jev),
  checks whether that rule has found nothing across its last ten or more
  dispatches and, if so, skips it - except `dependency-category` and
  `consequence-scoping`, which always run. Do NOT read a silent Gate 6 as
  "clean" - it may mean the diff is clean, or it may mean a rule's own
  measurement gate decided it was not worth running this time.
---

# Gate 6: code quality

## What this is for

Per `reference/PIPELINE-RESEARCH.md`, "Gate 6, code quality, now
specified": seven questions from a fixed vocabulary, asked of a commit's
staged diff, each answered strong / worth-exploring / speculative rather
than a continuous score - "hold the vocabulary fixed, a rule whose wording
drifts cannot hold a calibrated threshold."

It runs as a Claude Code `PreToolUse` hook on the `Bash` and `PowerShell`
tools (matcher `Bash|PowerShell`), the same
matcher and the same `git commit` detection Gate 4 already uses, and reads
the same staged diff Gate 4 reads (its own copy of the same helper
functions - gates in this pipeline are self-contained scripts, not a
shared diff-reading library).

## Why it never blocks

Gates 1-4 each have a real deny or ask outcome because each protects
against something that should not happen at all (a broken plan, a fake
package, a dangerous command, a leaked secret). Code quality is not that
kind of question - a seam with one implementation, a slightly worse health
delta, a hot-spot file are all things worth *knowing*, not things that
justify stopping a local, reversible commit. Gate 4's own `jev-risk-tier`
flag already established this shape (allow, but flag). Gate 6 is that
shape all the way down, for every one of its seven rules, with no deny or
ask path at all. A rule that could not run is skipped, never turned into
an ask - blocking a commit over a measurement that could not be taken
would claim a standing this gate does not have. An unreadable diff or a
rule that crashes is logged to `~/.jev-gates/hook-errors.log`. A failed
`git grep`, `git ls-files` or `git log` call inside `seam-reality`,
`test-layering` or `consequence-scoping` is skipped with no log line.

## The seven rules

| Rule | Question | How | NEVER_GATE |
| --- | --- | --- | --- |
| `depth` | Would deleting this new module just move its complexity elsewhere (shallow - a finding), or concentrate real complexity that is properly its own job (deep - good design, no finding) | Jev, one question per new file (up to 3) | no |
| `seam-reality` | Does a newly introduced interface-shaped base class (`ABC`/`Protocol`/`Interface` in its bases) have two or more implementers in the repo, or only one/zero | Deterministic: `git grep` for subclasses | no |
| `interface-test-surface` | Does a new/changed test import a private symbol directly, or use a leading-underscore identifier that might reach past the public interface | Deterministic for a private import, Jev for an ambiguous bare identifier | no |
| `test-layering` | Does a new test file land at a module whose existing test file was left completely untouched | Deterministic: `git ls-files` plus a module-stem match | no |
| `dependency-category` | Is a newly imported module stdlib, this repo's own `lib`/`skills` tree, or true-external | Deterministic: `sys.stdlib_module_names` plus a fixed local-root set | **yes** |
| `health-delta` | Did this diff add 5 or more branch points (`if`/`elif`/`for`/`while`/`except`/`and`/`or`/`case`) than it removed, in a `.py` file that already existed | Deterministic: count on added vs. removed lines, gates the delta not the absolute count | no |
| `consequence-scoping` | Have 15 or more commits touched this file (counting at most the 200 most recent that did) | Deterministic: `git log -200 --oneline -- <path>` | **yes** |

`depth` and `interface-test-surface`'s ambiguous cases share **one** Jev
request per commit (multiple questions, one call), the same
one-call-many-questions shape Gate 4's own secret/risk pair already uses.

## Measurement-gated dispatch, arithmetic before judgment

Per `reference/DESIGN-BASIS.md`, "Gate 6 gates on measurement before
judgment": gstack auto-gates a specialist with zero findings across ten or
more dispatches, and marks security and data-migration specialists
`[NEVER_GATE]` so they always run regardless. Ported here as a small
persistent counter (`~/.jev-gates/gate6-measurement.json`). Before a rule
runs at all - before the deterministic check executes, before the Jev
call is even considered - if it has gone quiet for ten or more
**consecutive** dispatches, it is skipped for this commit.
`dependency-category` (a new true-external dependency is a supply-chain
question) and `consequence-scoping` (a hot-spot file is a blast-radius
question) are this project's own analogue of gstack's security/data-
migration carve-out and always run.

A gated rule is not muted forever: it is re-probed once every ten skips,
and any real finding resets its silent streak to zero. The independent
review (2026-09-24, finding 1) caught the first pass getting both
directions of this wrong - a cumulative (not consecutive) count meant a
rule that had ever fired once was permanently immune from gating no
matter how long it went quiet afterwards, and a rule gated once stayed
gated forever because a skipped rule never got to update its own counter.
`should_dispatch`'s own docstring in `code_quality.py` has the full
before/after.

No cross-process lock guards this file: two commits landing in the same
instant can each undercount a dispatch by one. Accepted - it gates an
optimisation, not a security control, and the failure mode is "runs one
extra time," never "misses a real finding."

## The badge, approximated in SARIF

`reference/PIPELINE-RESEARCH.md` specifies a three-level badge mapped to
SARIF `level` and `rank`. The shared finding store (`lib/findings.py`) has
no native `rank` field - every other gate already relies on its shape, and
widening it for one gate's badge was not worth the coupling. Gate 6
approximates the badge as a `(level, confidence)` pair instead:

| Badge | SARIF level | confidence |
| --- | --- | --- |
| strong | `warning` | 0.9 |
| worth-exploring | `note` | 0.6 |
| speculative | `note` | 0.3 |

Only a `strong` finding reaches `additionalContext` on the commit about
to run (non-blocking, same shape as Gate 4's `jev-risk-tier`). Every
finding, whatever its badge, is written to the shared store - visible to
Gate 7 later in the same session, per P2.

## Known gaps, named rather than hidden

- **Not an AST-level analysis.** Every rule is a regex or line-count
  heuristic over diff text (plus, for two rules, a bounded `git
  log`/`git grep` call) - not a real parse of the changed code. False
  negatives are expected. A rule that never fires on a given repo's shape
  just stops being dispatched, which is the point of the measurement
  gate above.
- **`dependency-category`'s local-root set starts from `{"lib", "skills"}`
  and extends itself with every `.py` file actually found in this
  repository's own `lib/` directory at run time** (this codebase imports
  its own shared modules by bare name - `import findings`, `import
  jevgate` - never under a `lib.` prefix. A fixed `{"lib", "skills"}` set
  alone flagged every one of them as a supply-chain risk, independent
  review 2026-09-24, finding 5). A different project's own first-party
  layout would still need the base set widened if it does not keep shared
  code under `lib/`.
- **`seam-reality` only recognises `ABC`/`Protocol`/`Interface` literally
  named in a class's base list**, not an implicit interface (a class with
  no declared base that several others happen to duck-type against).
- **`health-delta`'s branch-keyword count is a proxy for complexity, not
  a real cyclomatic-complexity computation.** It counts keyword
  occurrences on added/removed lines only. It does not parse the file.
- **`test-layering`'s module-stem match is filename-based** (`test_x.py`/
  `x_test.py` -> `x`) and will miss a differently-named pair, or produce
  a false match for two unrelated modules that happen to share a stem.
- **The Jev-judged tier (`depth`, ambiguous `interface-test-surface`)
  caps at 3 questions each per commit** - a commit adding more than 3
  substantial new files, or more than 3 ambiguous identifiers, only ever
  gets the first 3 judged. A deliberate cost bound, not a promise of full
  coverage.
- **Thresholds (`DEPTH_THRESHOLD`, `SURFACE_THRESHOLD`,
  `HEALTH_DELTA_THRESHOLD`, `HOTSPOT_COMMIT_THRESHOLD`,
  `MEASURE_MIN_DISPATCHES`) are reasoned starting points, not yet
  calibrated** against this project's own logged outcomes - the same
  caveat every other uncalibrated threshold here carries.

## The exit-code contract

Same as every other gate: exit 1 from a `PreToolUse` hook is neither
allow nor block, so the command runs regardless. Every path here reaches
a deliberate 0, including a failure to import this gate's own
dependencies or a crash inside any single rule (which costs that one
rule's finding, never the commit or the other six rules).

## What it writes

A JSONL decision line at `~/.jev-gates/gate6.jsonl` naming which rules
fired and their badges, and one SARIF finding per result in the shared
store (`~/.jev-gates/findings`, `lib/findings.py`) - same store, same
shape, every other gate already uses. A rule can return more than one
result. Nothing reaches the store when the hook input carries no
session id.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root. To check
it by hand:

```console
echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"},"cwd":"."}' \
    | python skills/code-quality/scripts/code_quality.py
```

`--selfcheck` is fully offline - every rule function is tested against
synthetic diff text with `git`/Jev calls mocked, the same discipline
every other gate's own selfcheck uses. `eval_gate6.py` is **not**
offline: it builds a real throwaway git repository per case and runs the
actual hook as a subprocess against it, including a real (or, with no key
configured, skipped) Jev call for the `depth` case.

```console
python skills/code-quality/scripts/code_quality.py --selfcheck
python skills/code-quality/scripts/eval_gate6.py
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE6_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. `install.py` sets it to `1` when absent and never overwrites a value you set. A plugin install through `hooks/hooks.json` sets nothing, so the gate stays off there. An install switch, not a rule toggle - see Gate 3's `SKILL.md` for why. |
| `GATE6_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate6.jsonl`. |
| `GATE6_MEASURE_FILE` | Where the per-rule dispatch/finding counter lives. Defaults to `~/.jev-gates/gate6-measurement.json`. |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. Same store every other gate already uses. |

## Status: built and reviewed (2026-09-24)

The eval is 8 cases, stored baseline at `evals/baseline/gate6.json`,
passing 8/8 with exact rule-set matching per case (not a subset check -
see `eval_gate6.py`'s own docstring for why that mattered): one negative
control (a trivial commit earns no findings), one case per deterministic
rule confirming it fires on the shape it names, and one `depth` case that
exercises a real Jev call without pinning its answer.

Put through an independent adversarial review once the whole gate was
built (this project reviews a gate only after it is fully built). The
review returned 13 findings
(2 critical, 4 high, 5 medium, 2 low) - all fixed and individually
re-verified against their own repro before this status was written:

- **Critical**: the measurement gate's own counters were broken in both
  directions - permanent lockout after ten quiet commits, and permanent
  immunity after one early finding. Fixed with a consecutive-streak
  counter plus a periodic re-probe (see "Measurement-gated dispatch"
  above).
- **Critical**: `hooks.json` registered a 10-second timeout against a
  29-second internal hook budget that can make a real Jev call - Claude
  Code would kill the process mid-call. Raised to 30s, matching Gates 1
  and 7.
- **High**: `interface-test-surface`'s regex excluded every dot-accessed
  attribute (`obj._private`) - the actual shape a test reaching into
  internals takes - and Jev was asked to judge "the line" while only ever
  being given the bare identifier string. Both fixed.
- **High**: `depth`'s polarity was inverted - it flagged a well-designed
  "deep" module (Ousterhout's term: concentrates its own complexity) and
  stayed silent on a shallow, pointless wrapper. Rephrased so the finding
  fires on "shallow", not "deep" - confirmed against a real Jev call in
  the eval's own `depth` case, which now genuinely fires on a
  deliberately trivial fixture module.
- **High**: `dependency-category` flagged this repository's own `lib/`
  modules (`import findings`, `import jevgate`) as third-party
  dependencies, because they import by bare name, not under a `lib.`
  prefix the fixed root set expected. Fixed with a dynamic local-module
  listing.
- **Medium**: every `git log`/`git grep`/`git ls-files` call resolved its
  pathspec relative to the hook's own `cwd`, not the repo root the diff's
  own paths are relative to - silently missing every lookup when the
  agent ran the commit from a subdirectory. Fixed by resolving `git
  rev-parse --show-toplevel` once per commit and using it as the cwd for
  every subsequent git call.
- **Medium**: the eval's negative control used a subset check
  (`frozenset() <= got`), which passes unconditionally regardless of what
  actually fired. Switched every case to exact-set equality.
- **Medium**: `seam-reality` used `git grep -l` (matching files, not
  lines), collapsing two distinct implementers in the same file into one
  and missing a base class that was not the first token in a multi-
  inheritance list. Fixed with `-n` and a widened pattern.
- **Medium**: `interface-test-surface`'s dispatch was recorded twice per
  commit (once for its deterministic half, once for its Jev half), and
  `depth`'s counter advanced even on a commit with no new file at all,
  because it shares one Jev call with `interface-test-surface`'s ambiguous
  cases. Both counters are now recorded exactly once, and only when that
  specific rule actually had something of its own to check.
- **Medium**: a bare relative filename in `GATE6_MEASURE_FILE` made
  `os.path.dirname` return `""`, and `os.makedirs("")` raises on Windows -
  silently swallowed, so measurement was never actually persisted. Fixed
  with a `or "."` fallback.
- **Low**: `health-delta` counted branch keywords appearing as English
  prose inside a new docstring or comment. Added a best-effort comment/
  docstring stripper before counting.
- **Low**: reading an untracked file with no size cap or binary check
  could load an arbitrarily large or non-text blob into memory. Added a
  2MB cap and a null-byte sniff.
- **Low**: a garbled sentence in the `dependency-category` finding
  message. Reworded.

## References

- `reference/PIPELINE-RESEARCH.md`, "Gate 6, code quality, now
  specified" - the seven-rule table and the three-level badge this gate
  implements
- `reference/DESIGN-BASIS.md`, "Gate 6 gates on measurement before
  judgment" - the gstack-derived auto-gating and `[NEVER_GATE]` carve-out
- `../commit-screening/SKILL.md` - the sibling gate this one's diff
  reading, exit-code contract, and shared-finding-store conventions are
  deliberately copied from
