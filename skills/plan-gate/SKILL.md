---
name: plan-gate
description: >
  Gate 1. Runs before ExitPlanMode - the one point in a session where a
  whole plan exists as text before any file is touched. A one-line fix
  or a named-symptom repair passes with no model call at all. Any other
  plan runs ten deterministic checks (unresolved markers, placeholders,
  unquantified claims, at least one falsifiable outcome, named artefacts
  actually resolving, at least one proof command, MUST lines addressed,
  override completeness) and then exactly one Jev call judging goal
  match, scope, contradiction, stack confidence, and criterion
  falsifiability. Do NOT skip it because a plan "looks fine" - the whole
  point is that a plausible-reading plan can still solve the wrong
  problem or contain an unfalsifiable success criterion.
---

# Gate 1: plan gate

## What this is for

Stopping bad work before any code exists, at the cheapest possible
point - the plan itself. Per `reference/PIPELINE-RESEARCH.md`'s amended
P1 resolution: the original idea (move the old architecture-pick gate
earlier, add nothing else) was half wrong. A relocated stack-confidence
gate scores the wrong artefact - it cannot see a plan that solves the
wrong problem, restates the task as its own success criterion, or
silently expands scope, because intent and stack choice are two
artefacts with two quality bars. The amended design instead builds one
plan-stage gate with deterministic triage first and the old Gate 1's
question folded in as one field among five.

It runs as a Claude Code `PreToolUse` hook on the `ExitPlanMode` tool -
the only point a whole plan exists as one piece of text before the agent
starts touching files.

## The three outcomes

Not pass/fail. Per PIPELINE-RESEARCH.md: "pass-fail produces false
blocks on the common case where a plan is fine but one criterion is
unfalsifiable."

| Outcome | When |
| --- | --- |
| **go** (silent allow) | Trivial work class, or a feature-class plan that clears every deterministic check and every Jev field |
| **needs-clarification** (ask) | A soft deterministic signal (vague adjective, no proof command, an unaddressed MUST) or a soft Jev field (oversized, low stack confidence, an unfalsifiable criterion) |
| **block** (deny) | A hard deterministic failure (unresolved markers, a placeholder, no falsifiable outcome, a named artefact that does not exist, an incomplete override) or a hard Jev field (goal mismatch, a real contradiction) |

## D0: work-class triage, the cheap path

A plan phrased as fixing one named symptom (bug, typo, error, crash,
regression) touching at most one file, with no new dependency mentioned,
returns **go** immediately. Nothing below this runs, including the Jev
call. Deliberately conservative: classification only fires on a clear
match, because misclassifying a real feature as trivial is worse than
occasionally sending a genuinely trivial plan through the full path.

## D1-D9: the deterministic checks, feature class only

| Check | What it looks for | On failure |
| --- | --- | --- |
| D1 | More than three `[NEEDS CLARIFICATION: ...]` markers (spec-kit's own convention) | block |
| D2 | A placeholder remains - `TODO`, `TBD`, `???`, or an angle-bracket stub | block |
| D3 | An unquantified adjective (fast, scalable, secure, robust, intuitive, performant, simple, better) with no numeral in the same sentence | ask |
| D4 | Zero falsifiable outcomes anywhere - no numeral-with-unit and no named observable artefact | block |
| D5 | A path the plan says it will edit does not exist, or a named package is not in this project's manifest | block |
| D6 | No named command or test file that would prove the change | ask |
| D7 | A MUST line from a declared principles file is not addressed by name | ask (no-op today - see "Known gaps") |
| D8 | The prior gate's record exists and its schema version matches | not built - Gate 1 is first in the pipeline, so there is no prior plan-stage record to check |
| D9 | An asserted override is missing `Violation:`, `Why needed:`, or `Simpler alternative rejected because:` | block |

D1/D2/D4/D5/D9 run first, in that order, and the first hit stops the
gate outright with no Jev call - a hard failure needs no judgment. D3/D6/D7
run afterwards and are only ever collected as soft flags; they never
block alone.

**D5 stays deterministic permanently.** Per PIPELINE-RESEARCH.md: it is
"exactly the fact lookup Jev was tested on and failed" - the same
ground-truth finding that shaped Gate 2's whole design (see
`../package-check/SKILL.md`).

## J1-J5: one Jev call, five fields

Reached only when the deterministic floor is clear. One request, five
parallel Noul questions:

| Field | Question | Threshold | On trip |
| --- | --- | --- | --- |
| J1 | Does the plan solve a nearby easier goal instead of the stated one? | >= 0.7 | block - "highest value field, no deterministic proxy exists" |
| J2 | Is the plan larger than the problem requires? | >= 0.7 | ask |
| J3 | Does a decision contradict a stated requirement in substance? | >= 0.7 | block |
| J4 | Is the architecture/stack choice well-grounded? (the old Gate 1) | < 0.5 confidence | ask |
| J5 | Is at least one success criterion unfalsifiable, or a restatement of the task? | >= 0.6 | ask, never block - the exact case the three-state design exists for |

Thresholds are a reasoned starting point, **not yet calibrated** against
this project's own logged outcomes - the same caveat every other
uncalibrated threshold in this project carries.

Fail policy: no key, insufficient time budget, session call budget
spent, or the call itself failing - the gate falls back to whatever the
deterministic checks alone decided (go if none fired, needs-clarification
if a soft one fired), never a guessed block.

## Known gaps, named rather than hidden

- **D7 is a no-op in this repository today.** No `PRINCIPLES.md` (or
  `GATE1_PRINCIPLES_FILE`) exists yet - P12's built resolution reused the
  shared finding store instead of a declared principles file (see
  `reference/DESIGN-BASIS.md`, "P12 built"). The check is real
  infrastructure, not a promise this repository currently keeps.
- **D8 is not built at all.** Gate 1 is first in the pipeline; there is
  no prior plan-stage record for it to check. Named for whoever adds a
  second plan-stage gate later.
- **D0's trivial/feature split is a text heuristic**, not a real
  work-item classifier. A plan that phrases a real feature using
  symptom-repair language ("fix the missing X") could be misclassified
  as trivial and skip every check below it - a known false-negative
  risk, not a promise the split is sound.
- **D3's adjective list and D4's unit list are both fixed vocabularies.**
  A claim using a word not on either list is invisible to these checks.
- **The angle-bracket placeholder regex (D2) no longer flags a bare
  identifier** (`<div>`, `<T>`, `List<String>`) - fixed after adversarial
  review found it blocking ordinary HTML markup and generics. The
  tradeoff: a genuine single-word placeholder stub written as a bare
  identifier, e.g. `<filename>`, now also passes unflagged. `TODO`/`TBD`/
  `???` are unaffected either way, they are matched separately.
- **No wrapper unwrapping of any kind** - unlike Gate 3's `unwrap()`,
  this gate reads the plan text exactly as given, with no attempt to see
  through any indirection.
- **A plan over `PLAN_CHAR_LIMIT` (6000 characters) never reaches the
  Jev tier at all** - it asks for clarification instead of judging a
  truncated view of it (fixed after adversarial review found the
  original silently truncated and judged only the head). No chunking or
  multi-call summarisation is attempted; a plan that large is asked to
  be split or shortened, not partially judged.

## The exit-code contract

Unchanged from every other gate here: exit 1 from a `PreToolUse` hook is
**neither allow nor block**, so the plan goes through unexamined. Every
path here reaches a deliberate 0 or an explicit JSON decision, including
a failure to import this gate's own dependencies.

## What it writes

Every ask and every block are recorded: a JSONL decision line at
`~/.jev-gates/gate1.jsonl`, and a SARIF finding in the shared store so a
later gate can see it - same shape as every other gate.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root, matched
on `ExitPlanMode`. To check it by hand:

```console
echo '{"tool_name":"ExitPlanMode","tool_input":{"plan":"Fix the crash in `README.md`."}}' \
    | python skills/plan-gate/scripts/plan_gate.py
```

`--selfcheck` is fully offline - every path that could reach the
Jev-judged tier mocks `jevgate.api_key`/`call_jev`, same discipline every
other gate's own selfcheck uses. `eval_gate1.py` is **not** offline for
its one `judged` case: that plan clears every deterministic check and
reaches a real Jev call.

```console
python skills/plan-gate/scripts/plan_gate.py --selfcheck
python skills/plan-gate/scripts/eval_gate1.py
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE1_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. |
| `GATE1_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate1.jsonl`. |
| `GATE1_PRINCIPLES_FILE` | Path to a MUST-line principles file for D7. Defaults to `PRINCIPLES.md` in the plan's `cwd`. Not present in this repository today - see "Known gaps". |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. Same store every other gate already uses. |

## Status: built (2026-09-24), adversarially reviewed and fixed same day

The eval is 10 cases, stored baseline at `evals/baseline/gate1.json`,
passing 10/10: one D0 trivial-class allow, one empty-plan ask, five
deterministic-floor denies (D1, D2, D4, D5, D9), two soft-flag asks (D3,
D6), and one `judged` case that clears every deterministic check and
makes a real Jev call, verified genuine by checking the logged `j_probs`
field rather than a rule-name allowlist.

**Adversarial review found and fixed seven real defects** the day it was
built: a missing `plan` field on `ExitPlanMode` silently allowed with no
decision emitted at all (the most severe - a complete, silent bypass);
D5's edit-verb detection both false-blocked a legitimate file-creation
plan and missed real edits phrased with a noun between the verb and the
path, and separately let an earlier file's edit verb bleed across a
comma onto an unrelated later file in the same sentence; D9 accepted an
override with every field label present but its value left blank; the
eval's own `JUDGED_RULES` allowlist could pass a case as "judged" when
Jev was never actually reached; D0's new-dependency check missed every
package manager but npm/pip and its path count ignored any file not
wrapped in backticks; a plan over the Jev call's character window was
silently truncated and judged clean on its head alone; and D2's
placeholder check flagged ordinary HTML tags and generic type
parameters. See the "adversarial review, 2026-09-24" comments at each
fix site in `plan_gate.py`/`eval_gate1.py` for the exact before/after.

## References

- `reference/PIPELINE-RESEARCH.md`, "P1's recorded resolution was half
  wrong" and "The plan gate's checks" - the full D0-D9/J1-J5 design this
  gate implements
- `reference/DESIGN-BASIS.md`, "P1. Nothing gates the plan or spec
  stage." - the original gap and its amended resolution
- `../package-check/SKILL.md` - the sibling gate whose conventions
  (exit-code contract, shared finding store, enable-variable pattern,
  the allow/judged distinction) this one's are deliberately copied from
