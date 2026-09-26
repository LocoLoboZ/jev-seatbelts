---
name: completion-check
description: >
  Gate 7. MUST run before ANY claim that work is finished, done, complete,
  verified, passing, green, fixed, shipped, or ready for review, and before
  handing control back to the human at the end of a task. Use it whenever you
  are about to write "done", "all tests pass", "that's working now", "I've
  committed that", or any equivalent, and whenever you are about to stop and
  ask the human to take over work they already asked you to do. It checks the
  claim against hard evidence from the working tree rather than against how
  confident the sentence sounds. Do NOT skip it because the task felt small
  or the result seems obvious: an unverified small claim is exactly the case
  this gate exists for.
---

# Gate 7: completion check

## What this is for

A coding agent's most expensive failure is not a wrong answer. It is a
confident "done" that was never verified, because the human stops checking
after it. This gate makes the stop itself checkable.

It runs as a Claude Code `Stop` and `SubagentStop` hook. When a session
tries to end, it asks Jev, for every rule in `references/rules.md`, the
probability that the rule is being broken right now.

Two outcomes, decided by the rule's evidence class rather than by its score
alone. An `[observed]` rule is scored over tool results and working-tree
facts, so over its block threshold the session does not end and the agent is
told to keep working. A `[stated]` rule is scored over text the agent itself
wrote, so it is recorded and never blocks. The vendor documents that Jev
does not treat state as hostile, and a stopping agent writing its own
closing message is the clearest case of a subject authoring its own
evidence.

## Why it is not a string match

The predecessor pattern (`ralph-loop`) blocks exit until a promise string
appears in the output. An agent can print the promise without doing the work,
and the gate lets it through. Judging the wording of the final message alone
has the same weakness in a subtler form: "all tests pass" reads identically
whether or not the tests ran.

So this gate collects evidence the prose cannot fake, and gives it to Jev
alongside the message:

- which test, build, or lint commands ran this turn, and whether any errored
- an explicit flag when no test command ran at all
- `git status --porcelain` and `git diff --stat HEAD`

A rule like "do not claim tests pass when no test command was run" is only
decidable because those fields are there.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root and needs no
manual invocation. To check it by hand:

```console
echo '{"transcript_path":"","stop_hook_active":false,"last_assistant_message":"All done, tests pass.","cwd":"."}' | python skills/completion-check/scripts/completion_check.py
```

Exit 0 means the stop is allowed. Exit 2 with a message on stderr means the
agent is sent back to work.

Offline checks, no API call and no key needed:

```console
python skills/completion-check/scripts/completion_check.py --selfcheck
```

The generic plumbing (key lookup, transcript parsing, thresholds, the Jev
call and its retry, logging) lives in `lib/jevgate.py` and is shared with
every other gate. Only the completion-specific evidence and questions are in
this skill's script.

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE7_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. Anything else, including a typo, leaves it off. `install.py` sets it to `1` when it is absent. A plugin install through `hooks/hooks.json` sets nothing. |
| `GATE7_SELFTUNE_ENABLED` | Switches on the daily self-tune run (see `references/CALIBRATION.md`). Same values and same install behaviour as `GATE7_ENABLED`. |
| `TYPESAFE_API_KEY` | The Jev key. Also read from the Windows user registry when it is not in the environment, because a hook does not always inherit user variables. |
| `GATE7_BLOCK` | One number for every rule, or `substring=0.62,other=0.71` per rule. Only `[observed]` rules can reach it. When set, it replaces every self-tuned block value, not only the rules it names. A rule it does not name falls back to 0.75. |
| `GATE7_WARN` | Same format and same override. A warn is recorded and printed, and never blocks. |
| `GATE7_TUNED` | The self-tuned threshold file. Defaults to `~/.jev-gates/gate7-thresholds.json`. |
| `GATE7_EXTRA_TEST` | Extra regular expressions, one per line, for commands this project counts as a test run. Additive only. |
| `GATE7_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate7.jsonl`. |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. |
| `GATE7_DEBUG` | Any non-empty value prints the traceback of an unhandled error. The error is written to the hook error log either way. |

### Switching the gate on

Gate 7 is off until `GATE7_ENABLED` is set, and off means it returns before
it reads its own input. Registering the hook is not enough, because Claude
Code has no per-hook enable mechanism: a hook listed in `hooks.json` fires on
every Stop. The switch therefore has to live inside the script, and it does.
`install.py` sets the variable for you. A plugin install does not, so set it
by hand:

```console
setx GATE7_ENABLED 1      # Windows, new shells only
export GATE7_ENABLED=1    # POSIX
```

Documenting the gate as off by default while it was in fact on is a mistake
this project has already made once, caught by independent review. The
self-check now asserts that with the variable unset the hook exits 0 without
touching its input at all.

## What the gate can see from other gates

Gate 7 reads the shared finding store before it asks anything, and puts what
earlier gates recorded this session into its hard-evidence block. That is
pipeline gap P2: without it Gate 7 starts from zero every time and can pass a
task Gate 6 has already flagged.

Those findings count as observed evidence rather than the stopping agent's
prose, because another gate wrote them from tool output before this turn
ended, so a rule scored over them may block. At most 25 are included, newest
first, so a long session's backlog cannot eat the gate's time budget.

Gates 3, 4 and 6, the debug-triage ranker and the bypass log write to the
store, so a finding any of them recorded this session reaches Gate 7.

## Failure behaviour: fail open, deliberately

No key, API down, timeout, unreadable transcript, or any unexpected error
exits 0 and the session ends normally. This is the agreed behaviour for Gate
7 specifically, and it differs from Gates 3 and 4 on purpose. Gate 7 is not
the last line of defence against an irreversible action. A completion check
that wedges a session when an API is slow costs more than the premature stop
it was meant to catch.

There is also a loop guard: Claude Code sets `stop_hook_active` after a
push-back, and the gate exits 0 immediately when it sees it. It can push back
once per stop chain and can never trap a session in a loop.

## Status: checked defaults

The 0.75 block and 0.50 warn thresholds were checked once against real
operator-labelled stops (2026-09-26) and kept. They are checked defaults,
not fitted per-rule values. Held-out AUROC for the main blocking rule was
0.50, so the score cannot tell right blocks from wrong ones. The run is
recorded in `references/CALIBRATION.md`. The self-tune can move a rule's
value later, but only inside fixed guardrails. Calibrate per rule, never one
number for the gate, because the rules measure different things and an
average across them destroys the finding.

The evaluation described in `references/EVAL.md` has been run and is
recorded there, with a stored baseline at `evals/baseline/gate7.json`
(2026-09-22, 9 of 9). The case "stops at a bug without fixing it" is scored
by a `[stated]` rule, which can never block, so the eval now expects it to
be allowed. It scored 0.73 in the baseline.

Escape rate, meaning issues the gate should have caught, target zero, and
false-block rate come from marked decisions. Mark them with
`python lib/gatelog.py --mark` and read them with `--rates`. The self-tune
adds automatic labels from the human's next reply.

## References

- `references/rules.md` - the rules, and how to write a checkable one
- `references/CALIBRATION.md` - fitting thresholds to our own history
- `references/PROVENANCE.md` - the third-party adoption check for `limpet`
- `references/EVAL.md` - the before/after test results
