---
name: package-check
description: >
  Gate 2. Runs before ANY npm/yarn/pnpm install or pip/pip3/poetry
  install. A registry lookup (npm registry or PyPI, deterministic, no
  model call) confirms the package name actually exists before anything
  else happens - a name that does not exist is denied outright, the
  exact failure class a hallucinated or mistyped dependency is. A
  package that does exist is then handed to Jev for two residual
  judgments a lookup cannot make: does the name read as a deliberate
  typosquat of a well-known package, and does the package read as
  abandoned given its own last-publish date. Do NOT skip it because the
  package "obviously" exists - the whole point is that a name can look
  plausible and still not be real.
---

# Gate 2: package check

## What this is for

Stopping an install of a package that does not exist, or that exists
but looks like a supply-chain trap, before the install runs. Per
`reference/DESIGN-BASIS.md`, "Decision - Gate 2 is NOT a Jev-first
gate": Jev was tested directly against npm registry ground truth and
returned 0.71 for `left-pad` (real) and 0.38-0.41 for two fabricated
names - there is no threshold that separates them, because package
existence is a fact lookup, not a calibrated judgment. So the registry
lookup **is** the gate. Jev runs only after the facts are fetched, on
the judgments a lookup genuinely cannot make.

It runs as a Claude Code `PreToolUse` hook on the `Bash` tool, the same
matcher Gates 3 and 4 use. It only ever has an opinion about a command
that resolves to a registry install; every other Bash command passes
through untouched.

## The three outcomes

| Outcome | When | What the harness does |
| --- | --- | --- |
| **deny** | The registry says the name does not exist (`package-not-found`), or Jev judges it a likely typosquat (`jev-judged-typosquat`) | The install does not run |
| **ask** | The command names an install, but the registry lookup itself failed (network/DNS down, a non-404 HTTP error) | The human is asked |
| **allow** | Not an install, or the name exists and Jev found nothing wrong (`jev-judged-clean`/`resolved-safe`), or Jev judged it likely abandoned (`jev-judged-abandoned` - allowed, but flagged) | The gate stays silent, except the abandoned flag below |

Allow is silent by design, same reasoning as Gates 3 and 4: an explicit
`allow` decision would short-circuit the user's own permission rules and
every other `PreToolUse` hook. **One exception**: `jev-judged-abandoned`
is still allowed - an abandoned package is not itself dangerous to
install, and blocking it outright would be a nuisance for legitimate
maintenance work - but the flag reaches the agent through
`additionalContext` and is written to the shared finding store, the
same carve-out shape as Gate 4's `jev-risk-tier`.

## Ecosystems covered, and why only these

npm (via `npm`, `yarn`, or `pnpm` - all three install from the same npm
registry, so the registry is what is checked, not the CLI) and PyPI (via
`pip`, `pip3`, `python -m pip`, or `poetry`). cargo, go modules, and gem
are not covered - a deliberate first slice, the same "deliberately not
further" reasoning Gate 3's own build-order notes use for cloud CLIs.

## Distinguishing a real Jev judgment from Jev being unreachable

Same discipline as Gates 3 and 4: `jev-judged-clean` means Jev was
actually asked and found nothing; `resolved-safe` means Jev was never
consulted (no key, insufficient time budget, session call budget spent,
or a failed call) and only the registry fact stands. `eval_gate2.py`'s
`judged` cases check the rule name, not just the outcome, so an
unreachable Jev can never masquerade as a real judgment and still pass.

## What the fixed floor actually checks

A registry HTTP lookup - `registry.npmjs.org/<name>` or
`pypi.org/pypi/<name>/json` - with no local heuristic at all. A 404
means the name does not exist; any other failure (timeout, DNS, a 5xx)
is a lookup failure, not a fact, and answers **ask**, never a guessed
deny. This is a deliberate reframe of what "the fixed floor" means
compared to Gates 3/4: there, the floor is a local pattern match; here,
it is an external, authoritative fact source, because the question
itself ("does this exist") has no local answer.

## The Jev-judged tier, reached only for a confirmed-real package

One request, two parallel questions over the package name plus its own
last-publish date (a registry fact, not an invented one):

- **Typosquat question.** Does the name closely resemble a well-known
  package in a way that reads as deliberate (swapped/doubled/missing
  character, a hyphen/underscore swap, a common misspelling)? At or
  above `TYPOSQUAT_THRESHOLD` (0.7): denied.
- **Abandonment question.** Given the last-publish date, does this read
  as stale enough to be a real reliance risk? At or above
  `ABANDON_THRESHOLD` (0.6): allowed, flagged (see "one exception"
  above).
- **Neither fires.** Allowed, rule `jev-judged-clean`.

Thresholds are a reasoned starting point, **not yet calibrated** against
this project's own logged outcomes - the same caveat every other
uncalibrated threshold in this project carries.

Fail policy: no key, not enough time budget, session call budget spent,
or the call itself failing - all fall back to a silent allow on the
already-confirmed-real package (`resolved-safe`), never a guess.

## Known gaps, named rather than hidden

- **The third judgment named in the settled design - does the package
  match what the surrounding code actually needs - is not built.** It
  needs more context (the task, the file being edited) than one Bash
  command carries; a residual gap, not a promise kept, noted for
  whoever picks it up next.
- **No wrapper unwrapping.** Gate 3's `unwrap()` sees through `sudo`,
  `env`, `bash -c`. This gate does not - `sudo npm install x` is not
  detected. Same residual-gap shape Gate 4's own SKILL.md names for the
  identical reason.
- **A package name built from a shell variable is not read** - only
  literal text bashparse can resolve statically is checked. Named, not
  silently promised, same as every other gate's non-literal-word gap.
- **cargo/go/gem are out of scope entirely** - see "Ecosystems covered"
  above.
- **A private/internal package is denied as `package-not-found`, the
  same as a hallucinated one.** The registry lookup only ever queries
  the public `registry.npmjs.org`/`pypi.org` - there is no private-
  registry configuration, so a real internal package that only exists on
  a company registry looks identical to a fabricated one from this
  gate's point of view. If you use a private registry, this gate will
  block every install from it until it is taught to check that registry
  too.
- **Every package name checked is sent to a third-party service**
  (`registry.npmjs.org`/`pypi.org`), unauthenticated, with no way to opt
  out short of disabling the whole gate. Unlike Gates 3/4/7, which only
  ever call the operator's own TypeSafe API, this is the first gate that
  reaches an outside service by default - worth knowing before enabling
  it on a project whose dependency names themselves should not appear in
  a third party's access logs (independent review, 2026-09-24).

## The exit-code contract

Unchanged from Gates 3 and 4: exit 1 from a `PreToolUse` hook is
**neither allow nor block**, so the command runs. Every path here
reaches a deliberate 0 or an explicit JSON decision, including a
failure to import this gate's own dependencies.

## What it writes

Every deny, every ask, and a `jev-judged-abandoned` allow are recorded:
a JSONL decision line at `~/.jev-gates/gate2.jsonl`, and a SARIF finding
in the shared store so a later gate can see it - same shape as Gates 3
and 4.

## How to run it

The hook is wired in `hooks/hooks.json` at the repository root. To check
it by hand:

```console
echo '{"tool_name":"Bash","tool_input":{"command":"npm install left-pad"}}' \
    | python skills/package-check/scripts/package_check.py
```

`--selfcheck` is fully offline - the registry lookup itself is mocked
(no network call), and every assertion that could reach the jev-judged
tier mocks `jevgate.api_key`/`call_jev`, the same discipline every other
gate's own selfcheck uses. `eval_gate2.py` is **not** offline: both the
registry lookup and, for the `judged` cases, the Jev call itself are
real network calls.

```console
python skills/package-check/scripts/package_check.py --selfcheck
python skills/package-check/scripts/eval_gate2.py
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `GATE2_ENABLED` | **Off unless set.** `1`, `true`, `yes` or `on` switches the gate on. An install switch, not a rule toggle - see Gate 3's `SKILL.md` for why. |
| `GATE2_LOG` | Where the JSONL decision log goes. Defaults to `~/.jev-gates/gate2.jsonl`. |
| `JEV_FINDINGS_DIR` | Where the shared finding store lives. Defaults to `~/.jev-gates/findings`. Same store every other gate already uses. |

## Status: built (2026-09-24)

The eval is 9 cases, stored baseline at `evals/baseline/gate2.json`,
passing 9/9: two fixed-floor cases (a name that has never existed on
either registry, denied with no Jev call), two "not an install"
negatives, two path/URL/flag-value negatives, and two `judged` cases
that make a real Jev call on a real, well-known package. Live-verified
separately (not part of the eval's own record): the DENY path on a
fabricated name, an ALLOW on a real package with the package/ecosystem
fields correctly attributed in the decision log, and version-specifier
stripping (`left-pad@1.0.0` resolves to `left-pad`).

## References

- `reference/DESIGN-BASIS.md`, "Decision - Gate 2 is NOT a Jev-first
  gate" - the registry-vs-npm ground-truth test that settled this
  gate's whole shape
- `../command-safety/SKILL.md` and `../commit-screening/SKILL.md` - the
  sibling gates this one's conventions (exit-code contract, shared
  finding store, enable-variable pattern, the allow/judged distinction)
  are deliberately copied from
