---
name: command-safety
description: >
  Gate 3. MUST run before ANY shell command that deletes, overwrites,
  force-pushes, resets, prunes, drops, truncates, formats, or in any other
  way destroys something that is not trivially recreatable, and before ANY
  command whose real verb cannot be read from the text, such as one built
  from a variable, decoded from base64, handed to an interpreter with -c, or
  fed through xargs or find -exec. Use it for rm, git push --force, git
  reset --hard, git clean, kubectl delete, docker prune, DROP TABLE, dd,
  mkfs, and anything writing to a hook or shell configuration file. Do NOT
  skip it because the path looks obviously safe or the command is short: a
  command that turns out to be destructive is exactly the case this gate
  exists for, and an unread verb is not a safe verb.
---

# Gate 3: command safety

## What this is for

Stopping a destructive shell command before it runs, without becoming the
kind of gate people delete.

It runs as a Claude Code `PreToolUse` hook. It makes **no model call**, needs
no API key and no network, and answers in microseconds.

## The three outcomes

The gate parses the command, then classifies it:

| Outcome | When | What the harness does |
| --- | --- | --- |
| **deny** | Resolved, and on the denylist | The command does not run, and the agent is given a remediation intent code |
| **ask** | Unresolvable | The human is asked |
| **allow** | Resolved, and no family matched | The gate stays silent and the normal permission flow continues |

"Resolved" means every verb was read from a **literal** word. That is the
central idea, lifted from `cc-safety-net`: a command name is returned only
when its provenance is literal. `$CMD -rf /` does not name `rm`, so the gate
does not pretend to know what it is. It asks.

The allow answer is deliberately silent rather than an explicit `allow`
decision. An explicit allow would short-circuit the user's own permission
rules and every other PreToolUse hook, so this gate only ever says "deny",
"ask", or nothing.

## Unresolvable, in full

Any of these, and the gate asks:

- the text cannot be parsed, including unbalanced quoting and an
  unterminated heredoc
- **any word anywhere in the command** carries an expansion or substitution
  marker (`$`, backtick, `${`), whatever the verb is. `git commit -m "$MSG"`
  asks. This is the noisiest rule in the gate and it is deliberate: a word
  built at run time is not a word this gate has read. The first build ran
  this check only when the verb itself could not be named, which independent
  review reproduced as a bypass
- the verb is `eval`, `source`, `.`, or `exec`, `command`, `builtin` with a
  non-literal body
- the verb is an interpreter given code on the command line (`-c`, `-e`,
  `--eval`)
- the verb is an argument feeder (`xargs`, `parallel`, `find -exec`,
  `find -delete`), because the real verb can arrive from stdin
- a decoder (`base64 -d`, `xxd -r`, `openssl enc -d`, `uudecode`) appears
  alongside a shell or interpreter
- a cap is exceeded: 8,000 characters, 400 words, 60 segments, 6 levels of
  nesting, 4 wrapper layers

Wrappers are seen through rather than trusted: `sudo -u root`, `timeout 5`,
`env /bin/rm`, `nice -n 10`, and the `command`, `exec` and `builtin`
built-ins all resolve to the real verb, with their option values consumed.
Shell control words (`if`, `then`, `do`) are not commands. Heredoc bodies
are stdin, not shell. Every one of those is here because independent review
reproduced it as a bypass or a false block first.

Caps fail **closed**. Claude Code's own deny evaluator reportedly stops
evaluating past 50 subcommands and falls back to a prompt, and reportedly
inspects only the first token of a compound. This does the opposite.

## Why it parses at all

Not for bypass resistance. Parsing does not make a denylist sound and
nothing does.

It parses to cut **noise**. The earlier design dropped anything containing
`;`, `&&`, `||`, `|` or a newline straight to ask. That is safe and almost
useless: nearly every real command contains a pipe or a chain, so nearly
everything became an ask, and users delete gates that ask too often.
Parsing lets a chained command be resolved instead of escalated, which
lowers the ask rate without lowering the floor.

`git commit -m "wip" && pytest -q` is two resolved segments and is allowed.

## What it is not

**A hook is a speed bump. The OS sandbox is the boundary.** The
enumeration approach cannot be completed: the ShellSieve study found 69.0 to
98.6 per cent of 1,709 real-world denylists fragile, `openai/codex` ships
tests that deliberately assert no detection for variable indirection,
base64, `xargs` and `find -exec`, and `cc-safety-net` publishes fifteen
residual risks of its own. Ours are in `references/RESIDUAL-RISKS.md`,
RR-numbered, each with an example and a stated posture.

## The exit-code contract

Exit 1 from a PreToolUse hook is **neither allow nor block, so the command
runs**. Every path in this gate reaches a deliberate 0, a deliberate 2, or
an explicit JSON decision. An unhandled exception is turned into `ask`,
never into silence, and is recorded in the hook error log. The self-check
asserts this by feeding the real hook invalid stdin as a subprocess.

This differs from Gate 7, which fails open on purpose. Gate 7 is not the
last line of defence against an irreversible action. This gate is closer to
being one.

## Two-tier failure policy

A **security** check that raises escalates to ask. An **ergonomic** check
that raises is skipped, which leaves the stricter answer standing. The only
ergonomic check here is the build-directory exception that downgrades
`rm -rf node_modules` to allow, and if it fails the command is escalated
rather than waved through.

## Self-protection

Writes to the files that decide whether hooks run at all are denied:
`~/.claude/settings*.json`, `.claude/hooks/`, `.git/hooks/`, `~/.jev-gates/`
and the common shell rc files. This covers output redirection targets, the
written operands of file-writing verbs, and the `file_path` of `Write`,
`Edit`, `MultiEdit` and `NotebookEdit`.

Only actual writes count. `cat < ~/.bashrc`, `cp ~/.bashrc /tmp/backup` and
`sed -n 1p ~/.bashrc` are reads and are allowed, while `sed -i` on the same
file is not. Path forms are normalised first, so `$HOME/.claude/settings.json`,
`~/.claude/./settings.json` and `~/.claude//settings.json` all name the
protected file.

File **content** is never inspected. Writing a JSON file that contains a
dangerous string is text handling, and blocking it was one of the six false
positives this machine's own string-match guard produced in a single
session against zero true catches.

## What it writes

Every deny and every ask is recorded twice:

1. A JSONL decision line, for calibration later, at `~/.jev-gates/gate3.jsonl`.
2. A SARIF finding in the shared store, so later gates can see it.

The second one is the point. Gate 7 already reads that store, so a command
this gate refused is visible to the gate that rules on "done" rather than
being forgotten the moment the prompt is answered. That is the write side of
pipeline gap P2, and Gate 3 is the first gate to supply it.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root. To check it
by hand:

```console
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' | python skills/command-safety/scripts/command_safety.py
```

Offline checks, no key and no network:

```console
python lib/bashparse.py
python skills/command-safety/scripts/command_safety.py --selfcheck
python skills/command-safety/scripts/eval_gate3.py
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE3_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. Anything else, including a typo, leaves it off. |
| `GATE3_EXTRA_ASK` | Newline-separated regular expressions. Each one that matches **adds** an ask. Config can never remove a family or downgrade a verdict. An invalid pattern is skipped, not fatal, so one typo cannot disable the gate. |
| `GATE3_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate3.jsonl`. |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. |
| `GATE3_DEBUG` | Set to 1 to print tracebacks. The verdict is still ask. |

`GATE3_ENABLED` is an **install** switch, not a rule toggle. Once the gate
is on, no configuration variable reaches the catastrophic set or the
self-protection set. There is deliberately no per-family off switch: one
variable that turns off the boundary is not a boundary.

## Status: deterministic v1, no model call

Gate 3 v1 ships the deterministic half only, and it carries almost all the
weight: `garrytan/gstack` gates the same commands in production with zero
model calls.

**Deferred, not dropped:** the tier-4 semantic question. Our own probe
scored a recursive delete of a real documents directory, with the stated
intent "clean up build artefacts", at 0.95 destructive, block at 0.99 with
confidence 0.98, and intent match 0.03 of 2. No regex reaches that. It is
RR-13 in the register, and it is the one judgement in this gate worth paying
for. It is not shipped because the deterministic half had to be correct
first, and because the standing project rule forbids shipping a gate with an
uncalibrated borrowed threshold.

The eval is 69 cases, offline and deterministic, with a stored baseline at
`evals/baseline/gate3.json`. It passes 69 of 69. Twenty of those cases exist
because an independent Codex review found them: one critical fail-open on
import, seven bypasses of the classification, and four false blocks. The
first eval passed 49 of 49 while every one of those was live, which is the
most useful thing this gate has learned so far. An eval written by the
author of the gate measures intent, not resistance.

Read `references/RESIDUAL-RISKS.md` before believing anything about
coverage.

## References

- `references/RESIDUAL-RISKS.md` - the RR-numbered register of known gaps
- `references/PROVENANCE.md` - every lifted idea, its source, and its licence
- `references/EVAL.md` - the eval cases and what the run showed
- `../../lib/bashparse.py` - the parser, its provenance model, and its caps
