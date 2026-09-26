# Changelog

What changed, in plain words. Newest first.

## 2026-09-26

The Windows update. If your agent lives in PowerShell, the seatbelt now
clicks in there too.

### New

- **Gate 3 reads PowerShell.** Commands run through Claude Code's
  PowerShell tool now get the same checks as Bash. `Remove-Item -Recurse
  -Force C:\` is treated like `rm -rf /`, aliases such as `ri` and `rd`
  and shortened parameter names included. Wiping a disk or deleting
  backup copies is blocked outright. What still gets through is listed
  under RR-18 in `skills/command-safety/references/RESIDUAL-RISKS.md`.
- **Gates 2, 4 and 6 check PowerShell commits and package installs.**
  Before this, an agent using PowerShell skipped them entirely.
- **Gate 5 stall check.** Warns when the same test fails three times in a
  row with edits in between. That's not debugging, that's pokies. Off
  until `GATE5_STALL_ENABLED` is set.
- **Gate 7 can tune itself from your replies.** It reads how you answered
  after each stop and moves a threshold only when the evidence is strong.
  Off until `GATE7_SELFTUNE_ENABLED` is set. Your own `GATE7_BLOCK` and
  `GATE7_WARN` always win.
- **Gate 4 spots more keys.** TypeSafe, Anthropic and OpenAI project key
  shapes.

### Fixed

- **Gate 3 missed Windows spellings through the Bash tool.** `rm.exe`,
  `RM` and `git.exe` were not recognised, so a recursive delete of the
  root or a force push to `main` written that way was allowed. Git Bash
  runs all of them. Now caught.
- **Gate 3 checked only the first target of a recursive delete.** A root
  hiding in second place, like `rm -rf /tmp/x /`, went to Jev instead of
  being blocked. Every target is checked now.
- **Gate 3 blocked `bash -e script.sh`.** For bash, `-e` just means "stop
  on error". Only `pwsh` and `powershell` treat `-e` as code now.
- **Gate 3 could allow a risky command on a broken Jev answer.** A
  not-a-number reply slipped past the threshold. Every Jev answer is now
  checked before use.
- **No double billing.** A Jev call is never sent a second time after a
  timeout.
- **`git -C repo commit` is a commit.** It was read as a command called
  `repo`.
- **Gate 7 missed PowerShell test runs.** A turn that ran the whole suite
  through PowerShell could be blocked for "no tests run".
- **The installer handles upgrades.** It adds new gates, refreshes changed
  ones, never turns back on a gate you set to 0, and reports only what it
  really changed.
- **Evals no longer trip over themselves.** Each run gets its own session,
  so the session call limit no longer fills up across runs. The Gate 7
  eval runs in its own throwaway git repo, so its result no longer
  depends on whether you have uncommitted work.

### Tests and docs

- The Gate 3 eval grew from 115 to 128 cases, 13 of them PowerShell. All
  128 pass and the stored baseline matches. All seven gates: 176 of 176.
- Gate 3's `SKILL.md`, `EVAL.md` and `RESIDUAL-RISKS.md`, and the README,
  now match the code. The README lists every offline check and says which
  evals need a key.

## 2026-09-24

First public release. Seven gates, each off until you switch it on.
