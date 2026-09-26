---
name: commit-screening
description: >
  Gate 4. MUST run before ANY git commit, including a commit chained
  after git add in the same shell invocation, `git commit -a`, and
  `git commit --amend`. Screens the staged diff for a hardcoded secret -
  an AWS access key, a GitHub/GitLab token, a Slack token, a Stripe live
  key, a Google API key, an npm token, a TypeSafe, Anthropic or OpenAI
  project API key, or a PEM private-key block - and
  denies the commit outright if one appears on an ADDED line, no model
  call needed. When that fixed floor finds nothing and a Jev key is
  configured, one further Jev call judges what a fixed pattern cannot: a
  secret or backdoor in a shape the floor cannot phrase, and whether the
  diff touches risk-sensitive ground that earns a slower two-axis review.
  Do NOT skip it because the diff looks small: a one-line leak is exactly
  the case this gate exists for.
---

# Gate 4: commit screening

## What this is for

Stopping a hardcoded secret from being committed, before the commit runs.
Phase 1 (pattern matching - no model call, no API key, no network,
answers in milliseconds) is unconditional and runs first. Phase 2 (one
Jev call, only reached when phase 1 finds nothing) adds a semantic
secret/backdoor check and a risk-tier flag that recommends a slower
two-axis (Standards, Spec) review, per `reference/DESIGN-BASIS.md`, "Gate
4 absorbs the risk-sizing decision".

It runs as a Claude Code `PreToolUse` hook on the `Bash` and `PowerShell`
tools. A PowerShell command reaches it only when its text names `git` and
then `commit`. It only has an opinion about a command that contains
`git commit`, with one exception: a Bash command it cannot parse at all is
asked about (rule `commit-unresolved`), because it cannot tell whether
that command commits. Every other command passes through untouched.

## The three outcomes

| Outcome | When | What the harness does |
| --- | --- | --- |
| **deny** | An added line matches a named secret shape (phase 1), or Jev judges a secret/backdoor present (phase 2, rule `jev-judged-secret`) | The commit does not run. `python lib/bypass.py --grant` gives a one-time ticket that lifts a `jev-judged-secret` deny only (rule `gate-bypass-used`), never a phase 1 deny |
| **ask** | The command cannot be parsed (`commit-unresolved`), the diff could not be read (`diff-unreadable`: not a repo, `git` missing, timeout), or the gate crashed (`internal-error`) | The human is asked |
| **allow** | Not a commit, a commit whose diff has no match, or Jev cleared it (rule `jev-judged-clean`) | The gate stays silent |

Allow is silent by design, same reasoning as Gate 3: an explicit `allow`
decision would short-circuit the user's own permission rules and every
other `PreToolUse` hook. **Two exceptions**: a commit Jev flags as
touching risk-sensitive ground (rule `jev-risk-tier`), and a commit Jev
could not judge in a session where Gate 3 already flagged a command (rule
`gate3-caution-seen`). Both are still allowed - the commit is local and
reversible, so this never blocks it - but the flag is printed to the
agent as `additionalContext` and written to the shared finding store at
SARIF `note` level, so a later gate or a human reading it can still see
the recommendation. Without this carve-out the flag would vanish with the
silent allow and nothing would ever route anywhere.

## Distinguishing a real Jev judgment from Jev being unreachable

An allow from Jev actually clearing a diff and an allow from Jev never
being asked at all (no key, insufficient time budget, a failed or
malformed call) render identically on stdout - allow is silent either
way. The rule name in the decision log is the only way to tell them
apart. `jev-judged-clean` means Jev was asked and said fine.
`resolved-safe` means Jev was never consulted, phase 1's own verdict
stands unchanged. The same distinction Gate 3's `JUDGED_RULE` makes for
its own Jev-judged tier, reproduced here for the identical reason -
`eval_gate4.py`'s `judged` cases check this rule name, not just the
outcome, so an unreachable Jev can never masquerade as a real judgment
call and still pass the eval.

## The secret shapes on the floor, and why only these

Every pattern is a format a **provider itself defines** - an AWS key id
always starts `AKIA`, a GitHub token always starts `ghp_`/`gho_`/etc, a
PEM block always opens `-----BEGIN ... PRIVATE KEY-----`. That keeps the
false-positive rate near zero, the same reasoning Gate 3's own fixed floor
uses (see `command_safety.py`'s docstring, citing the ShellSieve study on
fragile denylists).

A generic `password = "..."` or `api_key = "..."` assignment is
deliberately **not** here. Whether that is a real secret or a test
fixture is context-dependent, which is exactly the kind of judgment call
this floor does not make - that belongs to Jev, in phase 2, the same
split Gate 3 draws between its own unconditional floor and the
Jev-judged tier above it.

## Phase 2: the Jev-judged tier

Reached only when the fixed floor above found nothing. One request, two
parallel questions (no extra latency, per `reference/DESIGN-BASIS.md`'s
own measurement) over the diff's added lines plus deterministic scope
facts gathered with no API call - risk-category path hints detected from
changed filenames only (auth, secrets, crypto, access control, billing,
migration, deploy/infra, logging/monitoring - the categories in
`RISK_PATH_HINTS` in `commit_screening.py`), and the diff's size:

- **Secret/backdoor question.** Does this diff still carry a credential or
  a backdoor a fixed pattern cannot phrase - an unusual auth bypass, a
  hardcoded debug credential, disabled verification, a covert network
  call? At or above threshold: denied, rule `jev-judged-secret`.
- **Risk-tier question.** Does this diff touch ground where a mistake
  would be hard to reverse or hard to notice? At or above threshold:
  allowed (never blocked - see "the one exception" above), rule
  `jev-risk-tier`, and the flag reaches the shared finding store.
- **Neither fires.** Allowed, rule `jev-judged-clean` - a real judgment,
  distinct from the never-asked fallback, see above.

Thresholds (`SECRET_THRESHOLD` = 0.6, `RISK_THRESHOLD` = 0.5 in
`commit_screening.py`) are a reasoned starting point, **not yet
calibrated** against this project's own logged outcomes. Gate 3's
`JEV_THRESHOLDS` carry the same caveat: they came from one Jev run on
base rates and are not fitted to logged outcomes either. Recalibrate once
`gatelog.py` has real verdicts to fit against.

Fail policy, unchanged from what phase 1 already was: no key, not enough
time budget to survive `call_jev`'s own worst case, the session's Jev
call budget spent, or the call itself failing or coming back malformed -
all fall back to the phase 1 verdict (rule `resolved-safe`), never a
guess, never an ask. If Gate 3 flagged a command earlier in the same
session, that fallback is `gate3-caution-seen` instead: still an allow,
but flagged, as above. This is the fail-
open-to-the-regex-scan behaviour `reference/DESIGN-BASIS.md` names for
Gate 4 specifically: a commit is local and reversible, so an outage does
not warrant blocking every commit while it lasts.

## What actually gets scanned

Only **added** lines (`+`, never a context or removed line) of the diff
this commit is about to record:

- Ordinary commit: `git diff --cached` - whatever is already staged.
  `git commit --am` is in this group: git reads `--am` as an
  abbreviation of `--amend`, not as `-am`, so nothing extra is staged.
- `git commit -a`/`--all`/`-am`, or a `git commit` preceded by a `git add`
  in the **same** shell invocation (`git add x && git commit -m y`):
  `git diff HEAD` for tracked files, plus a direct read of every
  currently untracked file, since `git diff` never shows an untracked
  file's content against any comparison target. PreToolUse fires before
  any part of the command has run, so at scan time nothing `add` or `-a`
  would stage is staged yet - the wider scan is the only way to see it
  before it lands in the commit.
- The commit **message** is not scanned, even in phase 2. `git commit -m
  'ghp_...'` is allowed by this gate - the leak that matters is in the
  tree, not the message. Message/diff cross-checking (does the message
  claim something the diff does not do) remains unbuilt, named in
  `reference/DESIGN-BASIS.md`, "Gate 4 - commit screening", as a further
  upgrade this phase does not cover.

## Known gaps, named rather than hidden

- **No wrapper unwrapping.** Gate 3's `unwrap()` sees through `sudo`,
  `env` and `timeout`, and Gate 3 sends `bash -c` to Jev as code it cannot
  read. This gate does neither - `sudo git commit` or
  `bash -c "git commit -m x"` is not detected. A residual gap, not a
  promise kept.
- **`--amend` scans only the newly staged delta**, the same as an
  ordinary commit, not a diff against the commit being amended's own
  parent. A secret already present in the commit being amended, and left
  untouched by this amend, is not caught.
- **Binary content is read as text** (`errors="replace"`) when scanning
  an untracked file. A binary secret blob would not match a text-shaped
  pattern anyway, so this is inert rather than dangerous, but it is not a
  deliberate binary-detection path.

## The exit-code contract

Unchanged from Gate 3: exit 1 from a `PreToolUse` hook is **neither
allow nor block**, so the command runs. Every path here reaches a
deliberate 0 or an explicit JSON decision, including a failure to import
this gate's own dependencies.

## Failure policy

A command this gate has already identified as a commit, but whose diff
it then fails to read (not a git repo, `git` not on PATH, the read times
out), answers **ask** - never silent allow. This gate has no read on
what is about to be committed in that case, and gstack's rule (carried
into Gate 3 already) is never allow by default on failure. A Bash
command that cannot be parsed at all is asked about too (rule
`commit-unresolved`), since the gate cannot tell whether it commits. A
command that parses and is not a commit is never touched.

This is phase 1's whole failure story, and phase 2's fail-open-to-this-
scan behaviour named in `reference/DESIGN-BASIS.md` for when Jev itself
is unreachable is exactly a fallback to it - see the "Phase 2" section
above.

## What it writes

Every decision this gate makes writes a JSONL decision line at
`~/.jev-gates/gate4.jsonl`, allows included. Every deny and every ask also
writes a SARIF finding in the shared store so a later gate can see it,
same shape as Gate 3, when the hook payload carries a session id. The
`jev-risk-tier` and `gate3-caution-seen` allows are the exceptions to
"an allow writes no finding" - each still writes a `note`-level finding,
since the flag is the whole point and a silent allow would erase it.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root. To check
it by hand, from inside a real git repo with something staged, with the
gate switched on for that one run:

```console
echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"},"cwd":"'$(pwd)'"}' \
    | GATE4_ENABLED=1 python skills/commit-screening/scripts/commit_screening.py
```

`--selfcheck` is fully offline - every assertion that could reach the
Jev-judged tier mocks `jevgate.api_key`/`call_jev`, the same discipline
Gate 3's own selfcheck uses, so it never depends on whether this machine
actually has a key configured. `eval_gate4.py` is NOT offline once phase
2 is reached: any case whose diff clears the fixed floor calls the real
hook as a real subprocess, and on a machine with a real key configured
(env var or the Windows registry fallback in `lib/jevgate.py:api_key`,
which clearing this subprocess's own environment cannot suppress) that is
a live call. `expected="judged"` cases exist for exactly that - see
"Distinguishing a real Jev judgment from Jev being unreachable" above.

```console
python skills/commit-screening/scripts/commit_screening.py --selfcheck
python skills/commit-screening/scripts/eval_gate4.py
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE4_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. An install switch, not a rule toggle - see Gate 3's `SKILL.md` for why. `install.py` sets it to `1` when it is absent. A plugin install from `hooks/hooks.json` sets nothing. |
| `GATE4_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate4.jsonl`. |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. Same store Gate 3 and Gate 7 already use. |
| `GATE4_DEBUG` | Set to 1 to print tracebacks. The verdict is still ask. |

## Status: both phases built

Phase 1 (2026-09-22): the local regex scan, unconditional, no model call.
Phase 2 (2026-09-22, same day): one Jev call, reached only when phase 1
finds nothing, for backdoor/secret confirmation and the risk-tier
question that flags a commit for a slower two-axis review, per
`reference/DESIGN-BASIS.md`, "Gate 4 absorbs the risk-sizing decision".

**What phase 2 does not do**: actually launch the two-subagent
Standards/Spec review. Gate 4 is a synchronous shell hook - it can decide
allow/ask/deny and write a finding, and nothing more. Sizing and
dispatching that review from the flag this gate writes is
`agentic-orchestration-pattern-selector-dbs`'s job, downstream of this
gate, not built here. This gate's job ends at "flagged, and the flag is
recorded where a later gate or a human will see it."

The eval is 9 cases, with a stored baseline at `evals/baseline/gate4.json`.
5 are settled with no Jev call: the four the fixed floor denies and the
non-commit. The other 4 clear the floor, so on a machine with a key they
make a real Jev call. Three of those expect allow. The fourth is the
`judged` case added for phase 2, checked by rule name so an unreachable
Jev cannot masquerade as a real judgment and still pass (see
"Distinguishing a real Jev judgment" above). The stored baseline passed 9
of 9 on 2026-09-22. It predates the one-time bypass, the read of Gate 3's
earlier findings and the TypeSafe, Anthropic and OpenAI key shapes.
Phase 1's own eval already caught one real bug its offline selfcheck
missed: a chained `git add x && git commit` correctly widened its diff
target, but the newly-added file was untracked, and `git diff` never
shows an untracked file's content against any target - the fix is
`_new_file_diff()`.

## References

- `reference/DESIGN-BASIS.md`, "Gate 4 - commit screening" and "Gate 4
  absorbs the risk-sizing decision" - the full design, phase 2 included
- `../command-safety/SKILL.md` - Gate 3, the sibling gate this one's
  conventions (exit-code contract, shared finding store, enable-variable
  pattern) are deliberately copied from
