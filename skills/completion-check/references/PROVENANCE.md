# Gate 7 provenance and adoption check

Standing project rule: never adopt a third-party tool without independently
checking its licence, popularity, author track record, and actual file
contents first. That rule has already caught one bundle and two unusable
repos on this project. This page is that check, carried out for Gate 7.

## What was checked

`noplan-inc/limpet`, via the GitHub API and the raw file contents, on
2026-09-20.

| Signal | Value | Read |
| --- | --- | --- |
| Licence | MIT | Fine. Permits adaptation with attribution. |
| Owner | `noplan-inc`, an organisation | Better than a personal throwaway account. |
| Stars | **1** | Weak. The design doc did not state a star count, so this is new information. |
| Created | 2026-09-17 | **Three days old.** Same age profile as the bundle this project already rejected. |
| Pushed | 2026-09-17 | A single day of work. No sustained maintenance to judge. |
| Size | 93 KB, one 605-line Python file plus a 13 KB test file | Small enough to read completely, and it was read completely. |
| CI | `.github/workflows/test.yml` present | Tests exist and run. |

## Verdict: port the pattern, do not install the tool

Adopting it as a dependency would mean trusting a three-day-old, one-star
repository to sit on the Stop hook of every session. That is the exact risk
shape already rejected once on this project, and a licence being MIT does
not change the age or the track record.

Reading it is a different matter, and the whole file was read. The
transcript-parsing, the loop guard, the fail-open discipline and the
threshold spec are good work and are reflected here with credit. No code
was copied verbatim.

## What our implementation deliberately drops

- **Codex CLI transcript support.** We only run Claude Code here.
- **The Vercel AI Gateway provider.** We call TypeSafe directly.
- **OS keychain storage and `LIMPET_KEY_CMD`.** The key is already a Windows
  user environment variable on this machine. `LIMPET_KEY_CMD` in particular
  runs `subprocess.run(cmd, shell=True)` on a config-supplied string, which
  is a shell-execution surface we have no need to carry.

Result: roughly 605 lines becomes roughly 300, with no feature we use lost.

## What our implementation adds

**Hard working-tree evidence**, which limpet does not collect. limpet passes
Jev the transcript and coarse tool outcomes. Ours also passes:

- which test, build, or lint commands ran this turn, and whether any errored
- an explicit flag for "no test command was run this turn"
- `git status --porcelain` and `git diff --stat HEAD`

This matters because the design brief for Gate 7 was to verify **real
evidence** before allowing exit, and to be a genuine upgrade over
`ralph-loop`'s plain string match. Judging only the wording of the final
message is a smaller upgrade than it first appears: a confident-sounding
"all tests pass" reads the same whether or not the tests ran. The evidence
fields are what make that rule decidable.

## Unexpected finding: `limpet calibrate` supersedes a planned dependency

The design doc logged `jevcal` (abhixhek, MIT, 8 stars) as the answer to
threshold calibration. Reading limpet showed it already ships the same
capability in its `calibrate` subcommand: it mines this machine's own
transcripts for past stops, has Jev classify how the human reacted to each,
then fits a per-rule threshold at a target false-positive rate and reports
AUROC so that rules which do not separate good stops from bad ones are
visible as noise.

That is the calibration method Gate 7 needs, and it is described in
`CALIBRATION.md` as our own procedure. `jevcal` is therefore **not needed as
a dependency**. One fewer third-party tool to vet.

## Residual risk accepted

Jev is sent the tail of the transcript: the last few messages, the tool
command lines of the turn, and the working-tree status. That is conversation
content leaving the machine for a third-party API on every stop. It is
inherent to the design, not a defect, but it is named here so the decision is
on the record rather than assumed.

## Automated security review of the Gate 7 script (2026-09-20)

An automated reviewer raised three findings on `completion_check.py`. Two
were acted on, one was rejected. Recorded here because a rejected finding
that is not written down looks identical to one that was never read.

**Accepted: unconstrained `transcript_path`.** The hook opened whatever path
the Stop event supplied and sent its tail to a third-party API. Only the
harness writes that field, and anyone who can forge it can already run code
as this user, so this was defence in depth rather than a live hole. It is
cheap, so `safe_transcript()` now resolves the path and accepts it only under
`~/.claude/projects` or the system temp directory.

**Rejected: the suggested fix for that finding.** Both suggested variants
proposed constraining the path to `cwd`. Claude Code stores transcripts under
`~/.claude/projects`, never under the working directory, so that check would
reject every real transcript. Because the gate fails open, the result would
have been a gate that always exits 0 while appearing to work. A security fix
that silently disables the control is worse than the finding. The `".." not
in tp` variant was also rejected: it is defeated by a symlink and by an
absolute path such as `~/.ssh/id_rsa`, which contains no `..` at all. The
self-check now asserts both directions, that hostile paths are refused and
that a real transcript path is still accepted.

**Accepted: log file permissions.** The decision log is now created with mode
`0o600` in a `0o700` directory.

**Rejected: stripping `assistant_text` and `tests_run` from the log.** Those
two fields are the labels the calibration procedure fits thresholds against.
Removing them would leave Gate 7 permanently stuck on a guessed threshold,
which is the specific weakness this gate is being built to remove. The file
holds the operator's own conversation on their own machine and is a strict
subset of what `~/.claude/projects` already stores unencrypted. Tightening
the mode addresses the exposure; deleting the data would trade a real
capability for no meaningful gain.

## Second automated security review (2026-09-20)

One MEDIUM finding: the new hook error log writes raw exception strings.

**Accepted in substance, rejected in method.** The suggested fix was to log
only a generic description with no exception details. That would have undone
the fix made minutes earlier: the error log exists precisely because a gate
failing open with no trace is indistinguishable from a gate switched off, and
a log saying only "something failed" restores that blindness. The exception
type and message are the entire diagnostic value.

**Checked the actual exposure first.** `str(urllib.error.HTTPError)` is
"HTTP Error 401: Unauthorized". urllib exceptions do not carry request
headers, so the `Authorization` header is not in the string, and the request
body is not echoed either. The realistic leak risk was low.

**Done anyway, narrowly.** `jevgate.redact()` strips the live key value by
direct comparison, plus `Bearer <token>`, `sk-`/`tsk-`/`key-` prefixed
tokens, and any 40+ character token-shaped run. Applied to every write to the
error log. Diagnostics survive; secrets do not.

Two bugs surfaced while proving it, both caught by the assertion rather than
by review:

1. The first attempt to wire `redact()` into `hook_error()` silently did not
   apply, so nothing was redacted at all. Without the assertion this would
   have shipped as a fix that did nothing - the exact failure mode Gate 7
   exists to catch, hit while building Gate 7's own library.
2. The assertion then failed a second time on stale lines left by the crashed
   first run. The self-check now truncates the log before writing to it.
