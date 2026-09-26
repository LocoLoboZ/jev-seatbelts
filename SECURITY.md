# Security

This project's whole purpose is to block unsafe agent actions. A way to get
past a gate is therefore the most valuable bug report this repo can receive,
and the one that should not be posted publicly first.

## Reporting a bypass

Use GitHub's private vulnerability reporting on this repository
(Security tab, then "Report a vulnerability"). Do not open a public issue.

Please include the command, transcript, or diff that got through, and which
gate should have caught it. A working bypass with a reproduction is more
useful than a described one.

Expect an acknowledgement, a fix or an explicit decision not to fix, and
credit if you want it. This is a single-maintainer project, so there is no
response-time commitment. If a report goes unanswered for 30 days, treat
yourself as free to disclose publicly.

## What counts as a bypass

- A destructive command that reaches the shell without hitting Gate 3's deny
  or ask tiers, particularly through shell obfuscation or by dropping a
  compound command into a path that is not parsed
- A secret that reaches a commit or a push without Gate 4 flagging it
- A completion claim accepted by Gate 7 without genuine test evidence,
  including a transcript crafted to look like evidence
- Anything that makes Gate 3 allow a command it could not resolve. Gate 3
  fails closed: when Jev cannot answer, it denies
- Anything that makes a gate fail with no trace in its log. Gates 1, 5, 6
  and 7 fail open by design, so a logged fail-open there is expected, not a
  bypass

## Known weaknesses, already public

These are stated so nobody wastes time reporting them as discoveries.

- **Gate 7's score cannot yet tell a right block from a wrong one.** The
  0.75 block and 0.50 warn thresholds were checked once against real
  labelled stops on 2026-09-26 and kept. On held-out data the main
  blocking rule scored an AUROC of 0.50. They are checked defaults, not
  fitted values.
- **`install.py` switches the gates on.** It sets each gate's enable
  variable to 1 when it is absent. A plugin install through
  `hooks/hooks.json` leaves every gate off. See `README.md`.
- **No push-time screening.** It was decided in the design but never
  built. Gate 4 screens commits only. Gate 3 still checks the push command
  itself, such as a force push to the default branch.
- **Subagent coverage is established, and partial.** Hooks from settings and
  from plugins also run inside subagents, so a PreToolUse gate covers a
  subagent's tool calls. A Stop fired inside a subagent is converted to
  SubagentStop at runtime, so Gate 7 is registered on both. Before that
  registration, a subagent could claim it was finished without Gate 7 ever
  running.
- **Pipeline-level gaps are documented.** The pipeline-level gaps section
  of `reference/DESIGN-BASIS.md` recorded twelve structural gaps on
  2026-09-20. Several are closed since, including the missing end-to-end
  test (`evals/eval_pipeline.py`). Check that file for what is still open.

## Scope and expectations

This is a personal project published so others can read and reuse the design
reasoning. It carries no warranty, as stated in `LICENSE`. Do not rely on it
as the only control preventing a destructive action in an environment you
care about. It is a second line of defence, not a first one.

## Handling of credentials

No API key is stored in this repository. The Jev key is read from an
environment variable, or on Windows from the user registry when a hook does
not inherit user variables. Check before you publish a fork of this: the key
lookup is in `lib/jevgate.py`, and the hook error log redacts secrets before
writing.
