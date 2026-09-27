# jev-seatbelts

[![Watch the intro](docs/images/hero-thumbnail.png)](https://github.com/user-attachments/assets/3e3f327d-c464-4319-80c1-6e35ab1306a7)

Your AI coding agent is quick, confident, and every now and then about
as careful as a wombat behind the wheel of a stolen ute. It'll happily
install a package that doesn't exist, `rm` something it can't get
back, or tell you "all done, tests pass" without running a single
test. jev-seatbelts is the seatbelt it didn't know it needed.

## What it actually does (no jargon, promise)

Seven checkpoints sit quietly in the background while an AI coding
agent works, and only speak up when something looks dodgy:

1. **Before it plans** - does the plan actually hold together, or is
   it flying blind?
2. **Before it installs anything** - is that package real, or a typo
   that only looks real?
3. **Before it runs a risky command** - is this about to delete,
   force-push, or drop something you can't get back?
4. **Before it commits** - is a password or a secret key about to go
   into the commit?
5. **When it's stuck debugging** - is it guessing, or does it
   actually know what's wrong?
6. **After it writes code** - did it just paper over the problem
   instead of fixing it?
7. **Before it says "done"** - did it actually check, or is it just
   telling you what you want to hear?

Most of these checks are instant and free - no AI needed, just a
sharp, boring rule (a lookup, a pattern match, a hard line that never
moves). Only the genuinely tricky calls get sent to a small, cheap AI
model ([Jev](https://typesafe.ai)) built to answer exactly one narrow
question, fast, for a fraction of a cent. Cheap and boring first,
smart and paid only when it's actually earned.

## The numbers, for the sceptics

This wasn't vibes-coded and shipped on a Friday arvo. Every figure
below comes straight off this repository's own test results and
review history - nothing here is a marketing number.

| What was checked | Result |
| --- | --- |
| Automated test scenarios, all 7 gates combined | 176 / 176 passing (100%) |
| Independent adversarial review rounds on the highest-risk gate (command safety) | 11 rounds, and later reviews still find real defects, which are fixed before release |
| Gate whose score cannot yet tell a right block from a wrong one | 1 of 7 (Gate 7 - flagged honestly below, not buried) |

"She'll be right" is not a test result. These are.

![Every saved test run, per gate](docs/images/test-runs-per-gate.svg)

Every eval run saved during the build, 2026-09-20 to 2026-09-26. A run
with a failure means at least one case did not get the answer it should
have. That is a test doing its job. Every gate's latest run passes all
of its cases.

![Gate 3 test cases over time](docs/images/gate3-test-growth.svg)

The riskiest gate's test suite grew as each review found a new way to
write a dangerous command. Each new trick became a test case, and every
stored baseline passed all of its cases.

`python evals/make_charts.py` redraws both charts. The runs chart needs
the saved runs in `evals/results`, which are not in git, so a fresh clone
can redraw only after it runs the evals.

## Status

All seven gates are built. Read this before installing - it's the fine
print, but it's short, and none of it is hidden in a footnote.

| Gate | Catches | State |
| --- | --- | --- |
| 1 Plan check | A plan that reads fine but solves the wrong problem, contradicts itself, or has no falsifiable success criterion | Built, adversarially reviewed |
| 2 Package check | A hallucinated or mistyped package, a typosquat, an abandoned package | Built. Public registries only (npm, PyPI); cargo/go/gem not covered |
| 3 Command safety | An unreviewed force-push, mass delete, `DROP TABLE`, or other hard-to-reverse command | Built. Deterministic parser first, Jev only for a command the parser cannot resolve |
| 4 Commit screening | A hardcoded secret or backdoor-looking diff about to be committed | Built, both phases |
| 5 Debug triage | A guessed fix tried without a falsifiable root-cause hypothesis | Built (the one phase that needs a model call, the rest of the discipline is unscripted process). Plus a no-model stall check hook that warns when three edits in a row leave the same test failure unchanged |
| 6 Code quality | Sloppy work that still compiles - measures, never blocks | Built, adversarially reviewed |
| 7 Completion check | A "done"/"tests pass" claim with no fresh evidence behind it | Built. **Threshold checked against real history and kept, but the score separates right from wrong blocks poorly - see below** |

- **`install.py` switches every gate on.** Each gate reads its own
  environment variable: `GATE1_ENABLED`, `GATE2_ENABLED`,
  `GATE3_ENABLED`, `GATE4_ENABLED`, `GATE6_ENABLED`, `GATE7_ENABLED`,
  plus `GATE5_STALL_ENABLED`, `GATE7_SELFTUNE_ENABLED` and
  `DRIFTGUARD_ENABLED`. `install.py` sets each one to `1` unless you
  already set it, so a flag you set to `0` stays off. A plugin install
  from `hooks/hooks.json` sets nothing, so there every gate stays off
  until you set its variable. Gate 5's ranker (`GATE5_ENABLED`) has no
  hook and is never switched on for you - see its own `SKILL.md`. Run
  `python lib/gate_status.py --status` to see which gates are on.
- **With no key, Gate 3 denies what it cannot read.** Gate 3 is on after
  `install.py`. Its fixed floor needs no key, but a command its parser
  cannot resolve goes to Jev, and with no key that command is denied, not
  asked. Expect some ordinary but unusual commands to be refused until a
  key is set. See "Requirements".
- **Gate 7's threshold is checked, not fitted.** The 0.75 block / 0.50
  warn values were checked once against real, human-labelled stops on
  2026-09-26 and kept. On that data the score for a right block and a
  wrong block overlapped almost completely (held-out AUROC 0.50 for the
  main blocking rule). Read
  `skills/completion-check/references/CALIBRATION.md` before trusting
  Gate 7 to block anything real.
- **Gate 2 only knows public registries.** A private or internal package
  looks identical to a hallucinated one to a registry lookup - it will be
  denied too. See `skills/package-check/SKILL.md`, "Known gaps".
- **Gate 3 is a speed bump, not a boundary.** A list of banned commands
  can never catch every way to write a command, in any shell. The real
  wall is the sandbox and the permissions of the account running Claude
  Code. Known gaps are numbered in
  `skills/command-safety/references/RESIDUAL-RISKS.md`.
- **The gates have not been proven working together on a real session
  yet.** `evals/eval_pipeline.py` runs one realistic debugging-to-commit
  session through every enabled gate and checks that Gate 5's finding,
  Gate 3's allow, Gate 4's allow, and Gate 7's block all land correctly in
  sequence - run it and read the result before trusting the pipeline as a
  whole, not just each gate's own isolated eval.
- **PowerShell is covered, with known limits.** Windows folk get the
  same seatbelt. Gate 3 reads every command run through Claude Code's
  PowerShell tool with a PowerShell reader of its own, so
  `Remove-Item -Recurse -Force C:\` meets the same checks as `rm -rf /`,
  aliases and shortened parameter names included. Gates 2, 4 and 6 check
  a PowerShell command when it names their topic (`git ... commit`, or a
  package install). What still escapes is listed under RR-18 in
  `skills/command-safety/references/RESIDUAL-RISKS.md`.
- **Developed and tested on Windows.** The key lookup falls back to the
  Windows user registry. Nothing here is deliberately Windows-only, but
  nothing has been verified on macOS or Linux either.

Do not use this as the only control stopping a destructive action in an
environment you care about. It is a second line of defence, not a first
one.

## Install

```console
python install.py
```

Adds the seven gates and the drift check as hooks in your global
`~/.claude/settings.json`, alongside whatever is already there - nothing
else is changed, though this plugin's own entries move to the end of each
hook event when they are refreshed. It also sets every gate's enable
variable to `1` unless you already set it (see "Status" above). Safe to
run more than once: it adds only the hooks and flags not already there, so re-running
after an upgrade picks up a newly added gate, and a flag you set to 0
stays 0.

```console
python install.py --dry-run    # show what would change, change nothing
python install.py --uninstall  # remove exactly what install.py added
```

Prefer to wire it up by hand, or only for one project? Copy the relevant
entries out of `hooks/hooks.json` into that project's own
`.claude/settings.json` instead, using absolute paths (a project-local
settings file has no `${CLAUDE_PLUGIN_ROOT}` to resolve against unless
this repo is installed as a marketplace plugin).

## Why

Using a fast, cheap, typed-judgment model to gate risky agent behaviour -
bad packages, destructive commands, unverified "done" claims - is a good
idea, one that several small unrelated projects converged on
independently. This one is self-built rather than assembled from
third-party binaries: Claude Code already has a native hooks system
(`PreToolUse`, `Stop`, `SessionStart`), and a Jev API key is all a hook
needs. Every gate here is a script you can read start to finish.

Two findings worth knowing before copying any of this. Gate 2 stopped
being Jev-first after Jev failed package existence against real npm
ground truth (0.71 for a real package, 0.38-0.41 for two fabricated ones -
no usable threshold between them), so a registry lookup is the actual
floor and Jev only judges what a lookup cannot: typosquat resemblance and
abandonment. Gate 3 was inverted to deterministic tiers first after
`garrytan/gstack` (100k+ stars) was found gating the same class of
commands in production with zero model calls. A fast judge is not a
universal win over a cheap deterministic check - verify per gate, on your
own evidence, before trusting either.

## Requirements

- Python 3, standard library only. No third-party Python dependencies.
- Claude Code, for the hooks to have anything to attach to.
- A [TypeSafe](https://typesafe.ai) Jev API key, needed by the gate evals
  that make live calls and by every gate's Jev-judged tier once its
  `GATEn_ENABLED` is set. The offline checks below need no key and no
  network. With no key found, every gate logs the gap and falls back to
  its fixed checks alone, with one exception: Gate 3's fixed floor needs
  no key, but a command that reaches its Jev-judged tier is denied when
  no judgement can be had (set `GATE3_JEV_FAIL_OPEN=1` to change that).

The key is read from `TYPESAFE_API_KEY`, or on Windows from the user
registry when a hook does not inherit user variables. No key is stored in
this repository.

## Layout

| Path | What |
| --- | --- |
| `install.py` | One-command install/uninstall of the gates as global Claude Code hooks |
| `lib/jevgate.py` | Shared plumbing every gate uses: key lookup, transcript parsing, thresholds, the Jev call with retry, logging, session-wide call budget |
| `lib/bashparse.py` | The quote-aware shell reader behind Gate 3: word provenance, lifted substitutions, caps that fail closed |
| `lib/psparse.py` | The PowerShell reader behind Gate 3: turns a PowerShell command into the same pieces `bashparse.py` produces, and refuses what it cannot read |
| `lib/findings.py` | The shared SARIF finding store. Gates 1 to 6 and the bypass write to it, Gate 7 reads it |
| `lib/evalharness.py` | Runs a gate's eval cases, stores a dated result, diffs against the stored baseline |
| `lib/gatelog.py` | Adjudicates logged gate decisions and computes escape rate and false-block rate |
| `lib/gate_status.py` | Which gates are enabled, and one session's Jev-reachability + call-budget state |
| `lib/bypass.py` | A sanctioned, logged, one-time bypass ticket for specific fail-closed DENYs - never the fixed-floor catastrophic/secret-scan DENYs |
| `skills/<gate>/` | One gate: `SKILL.md`, `references/`, `scripts/` |
| `evals/baseline/` | The reference result each gate is compared against |
| `evals/eval_pipeline.py` | One realistic session run through every enabled gate together, not just each gate's own isolated suite |
| `hooks/hooks.json` | Hook registration for a marketplace-style plugin install |
| `reference/DESIGN-BASIS.md` | Why each gate is shaped the way it is, including the decisions that turned out wrong and the evidence that changed them |

## Running the checks yourself

Every offline check (no key, no network needed):

```console
python lib/jevgate.py
python lib/bashparse.py
python lib/psparse.py
python lib/findings.py
python lib/gatelog.py
python lib/gate_status.py
python lib/bypass.py
python lib/evalharness.py
python lib/driftguard.py
python lib/driftcheck_hook.py --selfcheck
python skills/plan-gate/scripts/plan_gate.py --selfcheck
python skills/command-safety/scripts/command_safety.py --selfcheck
python skills/completion-check/scripts/completion_check.py --selfcheck
python skills/completion-check/scripts/gate7_selftune.py --selfcheck
python skills/commit-screening/scripts/commit_screening.py --selfcheck
python skills/package-check/scripts/package_check.py --selfcheck
python skills/code-quality/scripts/code_quality.py --selfcheck
python skills/debug-triage/scripts/hypothesis_ranker.py --selfcheck
python skills/debug-triage/scripts/pattern_sizer.py --selfcheck
python skills/debug-triage/scripts/stall_check.py --selfcheck
```

`package_check.py --selfcheck` is offline (the registry lookup itself is
mocked), but Gate 2's own eval, `eval_gate2.py`, always makes a live
registry call for every case and a live Jev call for its `judged` cases -
run it with the key-needing evals below.

Every check that needs a real Jev key:

```console
python skills/plan-gate/scripts/eval_gate1.py
python skills/package-check/scripts/eval_gate2.py
python skills/command-safety/scripts/eval_gate3.py
python skills/commit-screening/scripts/eval_gate4.py
python skills/code-quality/scripts/eval_gate6.py
python skills/completion-check/scripts/eval_gate7.py
python skills/debug-triage/scripts/eval_gate5.py
python evals/eval_pipeline.py
```

Gate 3's and Gate 4's evals are mostly fixed-rule cases that need no
key. Their `judged` cases make a live Jev call, and each one checks the
answer really came from Jev. With no key, those cases fail on purpose
rather than pass quietly. A test that passes because nobody was home is
not a test. See each gate's own `SKILL.md` for the detail.

Gate 5's eval needs a key for every case. It has no fixed-rule tier at
all - ranking hypotheses is the whole of what it does.

## Reference

`reference/DESIGN-BASIS.md` is the main reference. It records why each
gate is shaped the way it is, including the decisions that turned out
wrong and the evidence that changed them - kept rather than tidied away,
because the wrong turns are as useful as the right ones.

`reference/jev-superpowers/SOURCE.md` records a third-party bundle that
was evaluated and not adopted, and why. No third-party file contents are
redistributed here.

## Feedback

Issues are open and welcome, especially counter-examples that break a
threshold or a gate. See `CONTRIBUTING.md` for what pull requests need
to clear right now.

For a gate bypass, report it privately rather than in a public issue. See
`SECURITY.md`.

## Licence

MIT. See `LICENSE`.
