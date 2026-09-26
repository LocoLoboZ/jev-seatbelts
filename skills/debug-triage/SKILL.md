---
name: debug-triage
description: >
  Gate 5. MUST be followed whenever you are diagnosing a bug, a failing
  test, an intermittent failure, or any behaviour that does not match what
  was expected - before writing a fix, before changing any line of
  production code to "see if this helps", and before guessing at a root
  cause from the stack trace alone. Use it whenever you are about to try a
  fix without first having a deterministic repro that fails reliably, or
  whenever you are about to change more than one thing at a time to chase
  a bug. Do NOT skip it because the fix feels obvious: an obvious fix tried
  without a falsifiable hypothesis is exactly the guess-and-check loop this
  gate exists to replace.
---

# Gate 5: debug triage

## What this is for

The failure mode this gate replaces is guess-and-check: try something
plausible, see if the symptom goes away, move on without ever confirming
*why* it went away. That produces fixes that mask a bug instead of
resolving it, and repro-free "fixes" that silently regress later.

Unlike Gates 3, 4, and 7, this is not a Claude Code hook. There is no
single tool event to fire on for "a debugging session started" the way
there is for a shell command, a commit, or a stop. This is a discipline
the agent follows directly, with one script (`scripts/hypothesis_ranker.py`)
called once, mid-way through, to remove a guess this gate can otherwise
leave in - which hypothesis to test first.

## The six phases (Matt Pocock's `diagnosing-bugs` discipline, unmodified)

1. **Build a tight, deterministic pass/fail test loop, first.** Before
   theorising about causes, get one command that reliably fails when the
   bug is present and reliably passes when it is not. A bug you cannot
   reproduce on demand cannot be diagnosed, only guessed at.
2. **Reproduce and minimise.** Strip the repro down to the smallest input,
   smallest code path, and fewest moving parts that still fail. A smaller
   repro is a sharper test of any hypothesis phase 3 generates.
3. **Generate 3-5 ranked, falsifiable hypotheses - then rank them with
   Gate 5.** See below.
4. **Test hypotheses one variable at a time**, highest-ranked first. Change
   exactly one thing, re-run the deterministic repro from phase 1, read the
   result. Never change two candidate causes in the same test - a pass or
   fail then explains nothing.
5. **Fix** the confirmed cause, re-run the repro to prove it now passes.
6. **Mandatory cleanup pass.** Remove any debug logging, temporary
   instrumentation, or print statements added during triage. Re-run the
   *original* repro (not just the fix's own test) one more time, to prove
   the cleanup did not reintroduce the bug.

None of phases 1, 2, 4, 5, or 6 are scripted by this gate - they are
ordinary debugging discipline the agent follows directly, the same
"tight, deterministic, one variable at a time" rigour Matt Pocock's own
skill describes, unmodified. Only phase 3 makes a model call.

## Phase 3 in detail: rank hypotheses with Jev, don't guess the order

Once the repro exists and 3-5 falsifiable hypotheses are written down (a
hypothesis is falsifiable if there is a concrete change or observation
that would prove it wrong - "something about the cache" is not one,
"the cache write is not atomic" is), run:

```bash
python skills/debug-triage/scripts/hypothesis_ranker.py
```

with JSON on stdin:

```json
{"failure": "<the deterministic repro's own failure evidence - test
output, error trace, or observed-vs-expected, verbatim>",
 "hypotheses": [{"id": "h1", "text": "<falsifiable prediction>"},
                {"id": "h2", "text": "..."}]}
```

Jev scores each hypothesis on how well it explains the evidence, once, in
a single request covering all of them. The output is an order, never a
verdict - phase 4 still has to test the top-ranked hypothesis and confirm
it, not accept the ranking as proof.

**When Jev cannot be reached** (no key, no time budget, a failed call),
the script prints the hypotheses back UNCHANGED, under an explicit
`UNRANKED` tag, and says so - never a guessed order wearing Jev's name.
Order them yourself in that case, the same as if this gate were off.

**Advisory only.** This script has no `permissionDecision` to emit and
nothing downstream enforces its output. Unlike Gates 3, 4, and 7, skipping
its recommendation is not caught anywhere - the discipline holds only as
long as the agent actually follows phase 3 before phase 4, the same as
every other unscripted phase here.

## Phase 4's own sizing decision: `agentic-orchestration-pattern-selector-dbs`

Testing 3-5 ranked hypotheses "one variable at a time" does not mean one
agent testing them one after another is always right. Before starting
phase 4, invoke `agentic-orchestration-pattern-selector-dbs` to decide
between two shapes:

- **Parent-only, sequential** (the default, and usually correct): test the
  top-ranked hypothesis, read the result, decide whether it confirms,
  refutes, or is inconclusive, then move to the next. Cheapest, and the
  only shape that lets an earlier test's result change what phase 4 tries
  next - which is most of the time the right call, since Matt Pocock's own
  discipline is explicitly sequential ("test them one variable at a time").
- **Isolated parallel workers**, one per hypothesis: justified only when
  testing a hypothesis is genuinely independent (does not depend on an
  earlier hypothesis's result to know what to try) AND each test needs its
  own clean environment to avoid one test's instrumentation contaminating
  another's - for example, three hypotheses each requiring their own
  temporary service instance to reproduce a concurrency bug under
  different timing conditions, where running them in the same process
  sequentially reuses state.

**Write the step-3 justification first** (per the selector's own
"Justify every proposed worker"), then hand that same text to Jev via:

```bash
python skills/debug-triage/scripts/pattern_sizer.py < input.json
```

with `{"justification": "<why isolated parallel workers might be
needed>"}` on stdin. This is the selector skill's own "Jev-assisted mode"
(see its `SKILL.md`) for step 4 specifically - supplying a working Jev
client here is what opts the whole call into that mode, no further
per-step request needed. It answers only the sequential-vs-isolated
question, never the worker count, model choice, or budget, which stay the
selector's own steps 5-8 once a topology is chosen. **When Jev cannot be
reached** (no key, no budget, a failed call), it prints `TOPOLOGY:
parent-only (default - ...)` and the selector skill's own decision table
takes over unassisted for this step only - the same "zero subagents
preferred" bias either way, so an unreachable Jev never quietly
earns a more expensive plan.

The selector's own hard rule applies here without exception regardless of
which path decided it: zero subagents is the preferred result. Do not fan
phase 4 out to parallel workers because 3-5 hypotheses "sounds like" a
parallelisable list - that is exactly the "one worker per item" default
the selector exists to refuse. Follow its own decision record format once
a topology is chosen, and when it selects parent-only, say so in one line
and continue phase 4 directly - it does not need a full report for the
common case.

## Configuration

Off until switched on, same install-switch convention as Gates 3, 4, and
7: set `GATE5_ENABLED=1`. With it unset, `hypothesis_ranker.py` prints a
one-line notice and exits 0 - rank hypotheses yourself, phase 3 is not
blocked by the gate being off, only unassisted.

The key is read from `TYPESAFE_API_KEY`
(same lookup every other gate uses, see `lib/jevgate.py`). With no
key found, phase 3 falls back to the unranked order and says why, by
design - the same "fail open, never fail closed on an advisory gate"
default Gate 4's phase 2 already documents for itself.

## How to run it

```bash
python skills/debug-triage/scripts/hypothesis_ranker.py < input.json
python skills/debug-triage/scripts/hypothesis_ranker.py --selfcheck   # offline, no key needed
python skills/debug-triage/scripts/pattern_sizer.py < input.json
python skills/debug-triage/scripts/pattern_sizer.py --selfcheck       # offline, no key needed
python skills/debug-triage/scripts/eval_gate5.py                     # live cases, needs a key
python skills/debug-triage/scripts/eval_gate5.py --baseline          # promote this run
```

## Status: built (2026-09-22, T7h)

`hypothesis_ranker.py` built and self-checked offline (no key needed:
no-key, no-budget, call-failure, and malformed-answer fallbacks all
covered, same masking discipline as Gates 3 and 4 - a genuine judgment and
an unreachable Jev must never render the same way). Also live-fire tested
against the real Jev API, both directly and via `eval_gate5.py`'s three
scripted debugging scenarios (a concurrency race, an off-by-one, a stale
in-memory cache) - 3/3, the documented root cause ranked first every time,
promoted as the baseline at `evals/baseline/gate5.json`.

`pattern_sizer.py` built the same way, self-checked offline (same
fallback coverage), and live-fire tested against two real cases - a
same-file, same-test scenario correctly stayed parent-only (7%), a
genuinely isolated three-service concurrency scenario correctly selected
parallel (68%). This is the concrete script behind `SKILL.md`'s "Optional
Jev-assisted topology check" section in the selector skill itself
(`~/.claude/skills/agentic-orchestration-pattern-selector-dbs/SKILL.md`),
added this session - opt-in only, every other caller of that skill is
unaffected, and it falls back to the selector's own unassisted decision
table on any failure.

**No hook wiring, and none is planned.** Unlike Gates 3, 4, and 7, this
gate has no entry in `hooks/hooks.json` - there is no tool event that
means "a debugging session began" to fire a `PreToolUse` or `Stop` hook
on. It is discovered and followed as a skill, per the trigger description
above, the same way `superpowers:systematic-debugging` already is.

**One hook after all: the stall check (2026-09-26).** There is still no
event for "a debugging session began", but there is one for its most
common failure: edit, re-run, same failure. `scripts/stall_check.py` runs
on `PostToolUse` for Bash and edit tools, fingerprints each failing
test-like run (last 40 lines, digits and hex masked), and when the same
fingerprint returns three times with an edit in between it tells the
agent, via `additionalContext`, to stop guessing and come back here. No
model call, never blocks, off until `GATE5_STALL_ENABLED=1`. Idea from
`awlevin/typesafe-computer-use` (MIT), see
`reference/PIPELINE-RESEARCH.md`. Self-check:
`python skills/debug-triage/scripts/stall_check.py --selfcheck`.

**Phases 1, 2, 4, 5, 6 are process, not code**, and stay that way - Matt
Pocock's own discipline, followed directly, with nothing to build. Only
phase 3's ranking needed a script, because ranking probabilistically is
the one step a model call genuinely improves over a guess.

**Run end-to-end for real (2026-09-22, T7h continued).** A genuine
off-by-one pagination bug (`total_pages()` using floor instead of
ceiling division, dropping the last page's items) was built in a
scratch module, diagnosed through all six phases: a deterministic
failing repro (phase 1), minimised to the smallest failing case - 4
items, page size 3 (phase 2), four hypotheses written down from
actually reading the code, not already knowing the answer, ranked live
against the real Jev API - h1 (the true cause) came back first at 93%
(phase 3), a live `pattern_sizer.py` call correctly picked parent-only
at 4% (phase 4), the top-ranked hypothesis confirmed by changing one
variable and re-running the same repro (phase 4/5), and a cleanup pass
that re-ran the original repro once more to prove the cleanup itself
introduced nothing (phase 6). This closes the "only scripted eval
scenarios" gap: hypotheses here were the agent's own, not
hand-constructed to already match the eval's known answer.

## References

- `reference/DESIGN-BASIS.md`, "Gate 5 — debug triage (biggest upgrade
  available)" - why Jev is scoped to phase 3 only, and not the rest of the
  discipline.
- `lib/jevgate.py` - the shared Jev client, budget/headroom discipline,
  and API key resolution every gate reuses.
