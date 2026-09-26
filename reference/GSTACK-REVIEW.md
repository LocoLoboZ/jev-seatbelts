# gstack reviewed against our Jev pipeline

`garrytan/gstack`, checked 2026-09-20. MIT, **133,729 stars**, 19,931 forks,
created 2026-03-11, pushed 2026-09-18, 918 open issues, actively maintained.
65 skills, 30 hook-related files, 1,236 test files. All counts in this
file are as read on 2026-09-20 and have not been rechecked.

This is a completely different trust profile from anything else this project
has evaluated. `jev-superpowers` was 2 days old and rejected.
`noplan-inc/limpet` had 1 star and was ported rather than installed.
gstack is a mainstream, heavily-forked, well-tested project with a real
test suite. The standing adoption rule is satisfied on every axis.

Read directly: `careful/SKILL.md`, `guard/SKILL.md`,
`careful/bin/check-careful.sh`, `hosts/claude/hooks/timeline-stop-hook.ts`,
`review/sections/review-army.md`, `review/specialists/simplification.md`,
plus the full file tree.

## The headline finding

**gstack gates destructive commands with zero LLM calls.** `check-careful.sh`
is pure shell: a regex tier system with a safe-exception list. 133k stars, an
industry-standard setup, and the safety gate does not call a model at all.

Its Stop hook does not gate either. `timeline-stop-hook.ts` is telemetry
repair with an explicit fail-open contract that always exits 0. gstack has no
completion gate at all. Quality comes from `/qa`, `/review` and `/ship`
skills the human invokes.

For the stated intent - Jev on genuine decision gates only - this is the most
useful evidence available. It marks out, from a working system at scale,
which of our seven gates do not need a model.

## Where gstack says use LESS Jev

### Gate 3 (command safety) should be mostly deterministic

Our design already logged the `jev-axi` pattern: decide obvious commands
locally, send only uncertain ones to the API. gstack proves the deterministic
half carries almost all the weight, and hands us a tested pattern set.

Three tiers, matching the allow/confirm/block shape we already settled on:

- **HIGH, hard deny**: recursive delete of `/`, `~` or `$HOME`, and force-push to
  the repo's default branch. Only for SIMPLE commands - anything containing
  `;`, `&&`, `||`, `|` or a newline falls through to ask. Their stated rule
  is "conservative failure = ask, never guess".
- **MEDIUM, ask (always overridable)**: `rm -rf`, `DROP TABLE`, `TRUNCATE`,
  `git push --force`, `git reset --hard`, `git checkout .`, `kubectl delete`,
  `docker system prune`.
- **Safe exceptions, silent allow**: recursive delete whose target ends in
  `node_modules`, `.next`, `dist`, `__pycache__`, `.cache`, `build`,
  `.turbo`, `coverage`.

`--force-with-lease` is never HIGH. They deliberately did not hard-deny
`curl | sh`, because it would block legitimate installers.

Residual Jev role for Gate 3: the genuinely ambiguous middle only. A command
that matches no family but looks wrong, or the intent/command mismatch our
own probe demonstrated (a recursive delete of a documents directory while the
stated intent was "clean up build artefacts" scored 0.95 destructive, 0.99
block, intent match 0.03). That mismatch judgment is real and gstack cannot
do it. Everything else is regex.

### Orchestration should be arithmetic before it is judgment

`review-army.md` sizes its own fan-out with no model in the loop:

- Shell detects stack, diff size, test framework and scope flags.
- **Under 50 changed lines, every specialist is skipped.** A hard cheap
  cutoff before any agent spend.
- **Adaptive gating on measured hit rates.** `gstack-specialist-stats` tracks
  findings per dispatch. A specialist with 0 findings across 10+ dispatches
  is auto-gated off. Self-calibrating from evidence, and it is arithmetic.
- **`[NEVER_GATE]`** overrides that for security and data-migration, which
  run "even when silent" because they are insurance.

Our design doc proposed feeding Gate 1's Jev confidence into the sizing
decision. That is still sound, but gstack shows the cheaper first move:
**measure hit rates and gate on them.** Only reach for Jev where counting
cannot answer the question.

The `[NEVER_GATE]` concept is independent corroboration of our own hard rule
that Gates 3 and 4 require independent review regardless of what the cost logic
says. Two unrelated systems arrived at "some checks must not be optimised
away".

## Where gstack reveals a genuine Jev-shaped gate

This is the strongest enhancement found, and it is an addition rather than a
replacement.

**`DIFF_LINES < 50` is a crude proxy for risk.** A 12-line change to token
validation is far more dangerous than a 400-line documentation rewrite. Line
count cannot tell them apart. Likewise `SCOPE_AUTH=true` is decided from file
paths, so authorisation logic living in an oddly-named file is invisible to
it.

"Does this diff earn deeper review, and which kind?" is a semantic judgment
over supplied state, with a bounded set of answers. That is precisely
Choice/Score, it is exactly what Jev is for, and it is a genuine decision
gate rather than a check dressed up as one. It also fits the existing plan
for Gate 4, where the selector decides per commit whether a diff earns the
full two-subagent review.

Cost check: one call, sub-second, about 1,100 input tokens at the volumes we
measured. It replaces a wrong cheap heuristic, and it gates work that costs
several subagents. This is the one place adding Jev clearly pays.

## Where gstack strengthens our hooks with no Jev involved

Seven concrete items, ordered by how much they matter here.

1. **Never allow-by-default on a parse failure.** Broken install, unreadable
   payload, missing helper - all return ask. Their comment is explicit: a
   hook that gates destructive commands must not allow-by-default. This is
   the decision we already recorded for Gate 3, and gstack proves it in
   production.

2. **Parse `tool_input` with a real JSON parser.** They document the exact
   bypasses naive string extraction creates, and they are alarming:
   `git commit -m "wip" && rm -rf /` extracts as `git commit -m '` and is
   allowed. So is `bash -c "rm -rf /"`. Any future Gate 3 must use a real
   parser. Worth writing into the gate's own tests as named cases.

3. **Always record a failure somewhere.** gstack writes to
   `~/.gstack/hook-errors.log` on every internal error, best-effort. Our Gate
   7 fails open silently and only logs when it survives far enough to reach
   the log call. If it dies parsing stdin, nothing is written anywhere and
   the gate is invisibly off. **This is a real gap in code we have already
   shipped**, not a hypothetical. Fixed since: Gate 7 now logs any
   unhandled error, including a stdin parse failure, to the hook error log.

4. **Give the hook its own internal time budget.** Their Stop hook holds a
   2 s deadline, caps the file it will read at all, reads only a tail window,
   and re-checks the deadline before writing. They costed it explicitly
   because it runs on every Stop event machine-wide. Our Gate 7 has a tail
   window but no size cap and no internal deadline - it leans entirely on
   Claude Code's outer 30 s timeout. Fixed since: Gate 7 now holds its own
   time budget and skips the Jev call when too little is left.

5. **Project config may only ADD rules, never suppress.** `careful` reads
   extra patterns from a per-project file and consults them after the
   built-in families, so configuration cannot switch off a baseline warning.
   The right property for Gates 3 and 4.

6. **Structured findings with a fingerprint.** Every specialist emits one
   JSON object per finding with severity, confidence, path, line, category
   and `fingerprint` (`path:line:category`) for deduplication. Gates 4, 5 and
   6 all produce findings and had no agreed shape when this was written.
   They now share one: `lib/findings.py`, in SARIF 2.1.0.

7. **Findings ship with the test that catches them.** Specialists include a
   `test_stub` in the detected framework. A finding that arrives with a
   failing test is verifiable rather than assertable - the same discipline as
   Gate 7 demanding evidence instead of prose.

## What we should not copy

- **Their Stop hook design.** It does not gate. Ours does, deliberately, and
  that is the differentiator over `ralph-loop`'s string match. Gate 7 stays.
- **60 KB SKILL.md files.** When read on 2026-09-20, `qa-only/SKILL.md` was
  65 KB and `review/SKILL.md` was 61 KB. That is a prompt-heavy approach.
  Our gates stay small scripts with short skills.
- **Usage analytics.** Every skill appends to `~/.gstack/analytics/`. We log
  decisions for calibration, which is a different purpose. No usage
  telemetry.
- **`/freeze` as a Jev gate.** Directory-scoped edit boundaries are a useful
  idea we lack entirely, but it is a path comparison. If adopted, it is pure
  shell and never touches the API.

## Net effect on the gate plan

| Gate | Before | After this review |
| --- | --- | --- |
| 1 architecture pick | Jev Choice + confidence | Unchanged |
| 2 package check | Registry lookup, Jev for typosquat only | Unchanged, already settled |
| 3 command safety | Jev-first with a local prefilter | **Inverted.** Deterministic tiers do the work, Jev only for intent/command mismatch and the unmatched middle |
| 4 commit screening | Jev secret scan + optional deep review | Add a Jev risk judgment to decide whether the deep review runs, replacing a line-count proxy |
| 5 debug triage | Jev ranks hypotheses | Unchanged. Add fingerprints and test stubs to findings |
| 6 code quality | Jev scores the diff | Gate on measured hit rates first, Jev second |
| 7 completion check | Built, provisional | Keep. Add an error log and an internal time budget |

Jev call volume falls for Gate 3 and Gate 6, and rises by one call for Gate
4. The net is fewer calls doing more decisive work, which is the stated
intent.
