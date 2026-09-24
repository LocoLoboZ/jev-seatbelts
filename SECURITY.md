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
- Anything that makes a gate fail silently open rather than failing to a
  confirmation prompt

## Known weaknesses, already public

These are stated so nobody wastes time reporting them as discoveries.

- **Gate 7's threshold is not calibrated.** It is a placeholder. In the
  baseline run, "stops at a bug without fixing it" scored exactly on the
  threshold. Gate 7 is provisional for this reason. Every gate stays off
  until you set its own `GATEn_ENABLED` variable - see `README.md`.
- **Subagent coverage is established, and partial.** Hooks from settings and
  from plugins also run inside subagents, so a PreToolUse gate covers a
  subagent's tool calls. A Stop fired inside a subagent is converted to
  SubagentStop at runtime, so Gate 7 is registered on both. Before that
  registration, a subagent could claim it was finished without Gate 7 ever
  running.
- **Pipeline-level gaps are documented, not fixed.** See the pipeline-level
  gaps section of `reference/DESIGN-BASIS.md` for twelve known structural
  gaps, including that there is no end-to-end test of the gates working
  together.

## Scope and expectations

This is a personal project published so others can read and reuse the design
reasoning. It carries no warranty, as stated in `LICENSE`. Do not rely on it
as the only control preventing a destructive action in an environment you
care about. It is a second line of defence, not a first one, and today it is
mostly a design document.

## Handling of credentials

No API key is stored in this repository. The Jev key is read from an
environment variable, or on Windows from the user registry when a hook does
not inherit user variables. Check before you publish a fork of this: the key
lookup is in `lib/jevgate.py`, and the hook error log redacts secrets before
writing.
