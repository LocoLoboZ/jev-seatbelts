# Changelog

What changed, in plain words. Newest first.

## 2026-09-26

The Windows update. If your agent lives in PowerShell, the seatbelt now
clicks in there too. One exception: the Gate 5 stall check still watches
Bash only.

### Later the same day

A public content audit checked every doc against the code. These fixes
came out of it.

- **`install.py` switches the gates on, and now says so.** It sets
  `GATEn_ENABLED` for Gates 1, 2, 3, 4, 6 and 7, plus
  `GATE5_STALL_ENABLED`, `GATE7_SELFTUNE_ENABLED` and `DRIFTGUARD_ENABLED`,
  to 1 when they are absent. It never overwrites a value you set. A plugin
  install through `hooks/hooks.json` sets nothing, so every gate stays off
  there. Neither route sets `GATE5_ENABLED`.
- **The no-key warning covers Gate 3.** With no Jev key, Gate 3 is still
  on and fails closed. A command its parser cannot resolve is denied.
- **Gate 3 fails closed.** When Jev cannot answer, an unresolved command
  is denied. Gate 3 asks only when it crashes inside itself.
- **Gate 3 reads `cmd /c` through the Bash tool.** `cmd /c`, `cmd //c` and
  `cmd /k` are treated as unreadable and sent to Jev. With no key, denied.
- **Gates 1, 2 and 6 survive bad input.** An unhandled error, including
  stdin that is not JSON, is logged. Gates 1 and 2 then ask. Gate 6 lets
  the command through.
- **Gates 2, 3 and 4 get 30 seconds.** Their hook timeout was 10 seconds,
  shorter than a slow Jev call, so the harness could stop them before they
  decided.
- **`lib/gate_status.py` lists every switch.** It now shows
  `GATE5_STALL_ENABLED` and `GATE7_SELFTUNE_ENABLED` too.
- **Gate 7's thresholds were checked, not fitted.** 0.75 to block and 0.50
  to warn were checked once against real labelled stops on 2026-09-26 and
  kept. On held-out data the main blocking rule scored an AUROC of 0.50,
  so the score cannot yet tell a right block from a wrong one.
- **Push-time screening was never built.** It was decided in the design
  and is written down as not built.
- **The review chart is gone.** Its round counts were wrong: later rounds
  found real defects, not zero. The README no longer claims the reviews
  converged to zero.
- **Every gate's docs now match its code.** All seven `SKILL.md` files,
  their reference files, `SECURITY.md`, `CONTRIBUTING.md`, the design
  notes, and the `hooks.json` and `plugin.json` descriptions were checked
  line by line against the code and corrected. Stale code comments too.
- **A local file path was removed from the design notes.** It named the
  author's own user folder.

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
  row with edits in between. That's not debugging, that's pokies. It
  watches test runs through the Bash tool only, not PowerShell.
  `install.py` switches it on with `GATE5_STALL_ENABLED`. A plugin install
  leaves it off until you set that variable.
- **Gate 7 can tune itself from your replies.** It reads how you answered
  after each stop and moves a threshold only when the evidence is strong.
  `install.py` switches it on with `GATE7_SELFTUNE_ENABLED`. A plugin
  install leaves it off until you set that variable. Your own
  `GATE7_BLOCK` and `GATE7_WARN` always win.
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

First public release. Seven gates. `install.py` switched every gate on,
overwriting any value already set. A plugin install left them all off.
