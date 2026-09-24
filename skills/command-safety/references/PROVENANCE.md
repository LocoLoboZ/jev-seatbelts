# Gate 3 provenance and licence check

Standing project rule: never adopt a third-party tool without independently
checking its licence, popularity, author track record and actual file
contents first. Do not trust GitHub's licence classifier, which has reported
NOASSERTION for verbatim MIT and hidden a real Commons Clause. Read the
LICENSE file.

Nothing in this gate is copied code. What was taken is design, and every
borrowed idea is named below with its source.

## Licence bar, operator decision 2026-09-20

**Allowed:** MIT. Apache-2.0 with NOTICE and attribution.

**Excluded without exception:** Commons Clause and any other
source-available rider, GPL, LGPL, AGPL, any vendor-specific or
field-of-use restriction, and anything with **no licence at all**. No
licence means all rights reserved, which is stricter than GPL, not looser.

The private repository will be forked to a public one, and the only thing
that must not happen is inheriting a licence that constrains that release.

## What was lifted, and from where

| Idea | Source | Licence |
| --- | --- | --- |
| Word provenance: every word carries `literal`, `variable`, `command-substitution`, `arithmetic`, `glob` or `unknown`, and a command name is returned only when provenance is literal | `kenryu42/cc-safety-net` | MIT |
| Dual layer: a structured parse for precision, plus an independent quote-aware raw-text scan that runs whatever the parse did | same | MIT |
| Caps that fail closed on length, word count, segment count and depth | same | MIT |
| Remediation intent codes returned with a block, so the agent corrects rather than retrying with an obfuscation | same | MIT |
| A catastrophic set that no configuration can disable, separate from the rest | same | MIT |
| The RR-numbered residual-risk register format, each item with an example and a stated posture | same | MIT |
| Tokeniser shape, and lifting `$(...)` and backtick bodies out as their own segments leaving a sentinel | `MikahNiehaus/ClaudeBoost` `scripts/bash-guard.py` | MIT |
| The feeder rule: `xargs` and `parallel` are unresolvable rather than parseable through, because the verb can arrive from stdin | same | MIT |
| Two-tier failure policy: a security check that raises blocks, an ergonomic check that raises is skipped | same | MIT |
| Recursive wrapper unwrapping with a fail-closed depth cap | `openai/codex` | Apache-2.0 |
| Deterministic tiers, the build-artefact safe-exception anchored at the final path component, config that may only add rules, and the hook error log | `garrytan/gstack` | MIT |

`cc-safety-net` is TypeScript on Node, so adopting it was never on the
table. Its design ports and its code cannot, which is why this is a lift
rather than a dependency.

No code from `openai/codex` was copied, only the unwrap-with-a-cap idea, so
no NOTICE file is required. If code is ever copied from it, Apache-2.0
attribution and a NOTICE become obligations, not options.

## Named blocked sources, not read into this codebase

| Project | Licence | Why excluded |
| --- | --- | --- |
| `Dicklesworthstone/destructive_command_guard` | MIT plus an OpenAI/Anthropic rider | The rider denies all rights to Anthropic and anyone acting for its benefit, and forbids incorporation into any ML pipeline or evaluation harness. Technically the strongest design in the space. Black-box comparison only. |
| `bashlex` | GPL-3.0, abandoned 2024-04-08 | Contaminates an MIT release whether vendored or depended on. Removes most Python prior art as a code source. |
| `eyaltoledano/claude-task-master` | MIT plus Commons Clause | Source-available, not OSI. Shows as NOASSERTION on GitHub. |
| `sheeki03/tirith` | AGPL-3.0 | |
| `aryanbhosale/sh-guard` | GPL-3.0 | AST classifier mapped to MITRE ATT&CK. Interesting, untouchable. |
| `doorstop-dev/doorstop` | LGPL-3.0 | |
| `disler/claude-code-hooks-mastery` | **No licence at all** | About 3.9k stars and the most-copied hook in the ecosystem. Also technically poor: it lowercases the whole command and matches greedily across `;` and `&&`. |
| `broven/claude-permissions-plugin` | **No licence at all** | Also vendors GPL `bashlex` into an unlicensed repository. |
| Amazon Kiro | Proprietary | |

## The dependency question

`tree-sitter-bash` is MIT and therefore admissible under the bar above. It
was **not** taken for v1.

The parser ships behind a backend seam (`bashparse.BACKENDS`) so a
tree-sitter backend can be added as an optional accuracy tier later. The
stdlib backend ships first so a public user with no compiler still gets a
working gate, and an unknown backend name raises rather than falling back
silently, because a silent fallback is how an accuracy claim in a README
becomes false.

Because no third-party package was taken, `README.md` keeps its "standard
library only" claim truthfully. If the tree-sitter backend is ever made the
default, that sentence must change in the same commit. A false dependency
claim in a public README is the same class of error as the "Gate 7 is off by
default" claim that independent review already caught here once.
