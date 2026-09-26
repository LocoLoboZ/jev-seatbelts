# Gate 3 residual risks

A denylist cannot be made sound. This page is the honest version of that
statement: the known ways past this gate, each with an example and a stated
posture. It is modelled on the fifteen-item register `kenryu42/cc-safety-net`
publishes for the same reason.

**A hook is a speed bump. The OS sandbox is the boundary.** If a command
running would be unrecoverable, the control that has to hold is the sandbox
or the permissions of the account, not this file.

Three independent confirmations that the enumeration approach cannot close:

- The ShellSieve study found 69.0 to 98.6 per cent of 1,709 real-world
  denylists fragile.
- `openai/codex` ships tests that deliberately **assert no detection** for
  variable indirection, base64, `xargs` and `find -exec`.
- `cc-safety-net` publishes fifteen numbered residual risks of its own.

Posture values used below:

| Posture | Meaning |
| --- | --- |
| `escalates` | The gate cannot resolve it, so it asks. Not a bypass, a cost. |
| `accepted` | Known gap. Not closed, and not planned to be. |
| `open` | Known gap that should be closed, with the shape of the fix noted. |

## The register

**RR-1. A dangerous command reached by an interpreter body. `escalates`.**
`python -c "import os; os.system('rm -rf /')"`. The gate cannot read the
body, so it asks rather than deciding. Every interpreter with `-c`, `-e` or
`--eval` lands here, which makes the ask rate higher than a guesser's would
be. That is the trade, taken deliberately.

**RR-2. A verb that arrives from stdin. `escalates`.**
`ls | xargs rm -rf`, `parallel rm -rf ::: a b`, `find . -exec rm {} +`. The
command in front of us is not the command that runs, so `xargs`, `parallel`
and `find -exec` are treated as unresolvable rather than parsed through.

**RR-3. A decoder feeding a shell. `escalates`.**
`echo <base64> | base64 -d | sh`. Note that `cc-safety-net` actively
classifies `base64` as a benign display command. That behaviour is not
inherited here. Adjacency inside the pipeline is **not** checked: a decode
anywhere in a command that also names a shell escalates the whole command,
which is conservative in the noisy direction on purpose.

**RR-4. A script file whose contents we never see. `accepted`.**
`bash ./deploy.sh` resolves to `bash` with a file argument and is allowed.
Reading the file would mean judging content that can change between the hook
firing and the command running, which is a time-of-check to time-of-use
race, not a check.

**RR-5. An alias, a function, or a `PATH` shadow. `accepted`.**
A user-defined `rm` that is really something else, or a `./rm` earlier in
`PATH`. The gate reasons about the text, and the shell resolves names at run
time. Nothing readable from a hook closes this.

**RR-6. A dangerous command with a target this gate does not know is
precious. `accepted`.**
`rm -rf /srv/customer-data` is an ask, not a deny, because the gate has no
model of which directories matter. The catastrophic list is machine-scope
paths only. Sizing that list from the actual filesystem was rejected: a gate
whose verdicts depend on the machine it runs on cannot be evaluated.

**RR-7. SQL arriving from a file or from stdin. `open`.**
`psql -f drop.sql` and `mysql < drop.sql` are not detected. The SQL family
reads the parsed words and is scoped to a known database client. Closing
this means reading the file, which RR-4 already rules out for the same
reason.

**RR-8. Windows-shell syntax. `escalates`.**
PowerShell tool calls are read as PowerShell since 2026-09-26. See RR-18
for what that reader covers and what still escapes it. `cmd.exe` syntax is
not read: `cmd /c ...` goes to Jev as a command this gate cannot read.

**RR-9. Anything past a cap. `escalates`.**
Over 8,000 characters, 400 words, 60 segments or 6 levels of nesting, the
parser raises and the gate asks. The caps fail closed, which is the opposite
of Claude Code's own deny evaluator, which reportedly stops evaluating past
50 subcommands and falls back to a prompt.

**RR-10. The self-protection list is a list of names. `accepted`.**
It knows `~/.claude/settings*.json`, `.claude/hooks/`, `.git/hooks/`,
`~/.jev-gates/` and the common shell rc files. A symlink pointing at one of
them, or a settings file somewhere unusual, is not covered. Anyone who can
place that symlink can already run code as this user.

**RR-11. The gate only sees what the tool call says. `accepted`.**
A command that writes a script now and runs it in a later, innocuous tool
call is two allowed steps. Per-call gating cannot see a plan.

**RR-12. A deny is advice, not enforcement. `accepted`.**
The decision is a JSON permission decision returned to Claude Code. If the
hook is not registered, is switched off, or the harness changes its
contract, nothing is enforced. This is why the enable state is printed in
the gate's own log and why the README says plainly that installing this
gives no protection until it is switched on.

**RR-13. Semantic judgement only where the floor flags. `open`.**
Our own probe scored a recursive delete of a real documents directory, with
the stated intent "clean up build artefacts", at 0.95 destructive and 0.03
of 2 on intent match. No regex reaches that. Jev now judges every command
the floor flags or cannot read, so that delete reaches Jev as
`delete-recursive`. A command the floor resolves as safe is never sent to
Jev, so one that is syntactically ordinary and semantically wrong, with no
family matching it, is still allowed.

**RR-14. The allow answer is deliberately silent. `accepted`.**
When no family matches, the gate exits 0 and prints nothing rather than
emitting an `allow` decision. An explicit allow would short-circuit the
user's own permission rules and any other PreToolUse hook. The cost is that
a resolved-safe verdict is invisible in the harness UI.

**RR-16. Compound-command grammar is recognised, not understood.
`accepted`.**
Control words (`if`, `then`, `do`, `done` and the rest) are dropped so the
real verb is seen. The gate does not build a syntax tree, so it does not
know which branch runs. `for f in *` leaves `f` looking like a verb, which
matches nothing and is allowed. Every command inside the construct is still
classified, which is the part that matters.

**RR-17. Heredoc handling assumes one body per operator, in order.
`accepted`.**
Bodies are stripped before parsing, and a quoted delimiter makes the body
inert. Two heredocs opened on a single line are handled sequentially rather
than by bash's exact rules. An unterminated heredoc raises and becomes ask.

**RR-18. PowerShell is read by a reader of its own. `escalates`.**
Found 2026-09-26: this gate was registered on the Bash tool only, so
`Remove-Item -Recurse -Force C:\` through the PowerShell tool ran with no
check. Closed the same day. `lib/psparse.py` reads PowerShell into the same
segments bashparse makes: the backtick escape and line continuation,
single and double quotes and their doubling, curly quotes, here-strings,
`#` and `<# #>` comments, the call operator, redirections, and assignments.
`$(...)`, `@(...)`, `(...)` and `{...}` bodies are lifted out and checked,
so a script block cannot hide a command. `ps_translate()` then binds the
parameters of the cmdlets that delete or write (`Remove-Item`, `Copy-Item`,
`Move-Item`, `Rename-Item`, `Set-Content`, `Add-Content`, `Clear-Content`,
`Out-File`, `Tee-Object`, `New-Item` and their aliases) the way PowerShell
does, by name, prefix, `-Name:value` and position, and hands them to the
existing families. Native commands (`git`, `kubectl`, `terraform`) meet the
same families as in Bash. Disk wipes (`Format-Volume`, `Clear-Disk`,
`format D:`) and shadow-copy or backup deletion are denied outright.

What still escapes the fixed floor and goes to Jev: a .NET method call
(`[IO.Directory]::Delete(...)`), `Invoke-Expression`, `Start-Process`,
`Invoke-Command`, `cmd /c`, `wsl`, a parameter that cannot be bound, and
any word built at run time. What is not seen at all, and stays `open`:

- An alias or function the user's profile redefines. The reader knows the
  default aliases only. A function defined in the same command is seen,
  because its body is lifted.
- Destructive cmdlets outside the mapped set, for example
  `Remove-ItemProperty`, `Remove-CimInstance`, `Stop-Computer` or
  `Set-MpPreference -DisableRealtimeMonitoring`. They are allowed unread,
  the same way an unknown Bash verb is.
- `Move-Item` away from a protected file is not a write to it, the same gap
  `mv` has in Bash.

**RR-15. Every one of these is written from our own reading. `open`.**
No adversary has tested this gate. The register records what was
anticipated, not what survived contact. Counter-examples are wanted: see
`SECURITY.md` at the repository root for how to report one privately.

**What one review already found, so this is not theoretical.** An
independent Codex review of the first build reproduced twelve defects,
including a critical fail-open on import (exit 1, which means the command
runs), seven ways to hide `rm -rf /` from the classifier, and four false
blocks on ordinary work. All twelve were reproduced locally, fixed, and
added to the eval as named cases. The eval passed 49 of 49 while every one
of them was live. Treat this register as the floor of what is wrong with
the gate, not the ceiling.
