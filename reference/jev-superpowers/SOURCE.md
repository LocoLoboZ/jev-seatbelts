# Source

Evaluated 2026-09-20:
<https://github.com/AkashPriyadarshii/jev-superpowers> (MIT licensed).
Not adopted.

## Why it was not adopted

Judged on observable properties of the artefact, not on its author:

- It bundles 7 separate third-party CLI tools drawn from 5 unrelated GitHub
  accounts, each granted shell, git-commit, or task-completion authority.
- It was published 2 days before it was evaluated, with no track record.
- Its own tool table pointed at `KSym04/limpet` as the completion gate. That
  repository is an unrelated Rust project. A checkable broken reference
  at the time. As read on 2026-09-26, the upstream table has since been
  corrected to `noplan-inc/limpet`.

None of those points require an opinion about anyone. They are each
verifiable by opening the repository.

The concept was sound and is the reason this project exists. The
implementation carried more third-party surface area than the idea needed,
given that a TypeSafe API key and Claude Code's native hooks are sufficient
on their own.

## What used to be here, and why it is gone

Five `.SKILL.md` files were previously copied into this directory verbatim as
design reference. They were removed before this repository was prepared for
publication. Redistributing them would have required carrying the upstream
MIT copyright notice, and they had already served their purpose: everything
learned from their structure is recorded in `../DESIGN-BASIS.md`, in this
project's own words.

To read the originals, go to the upstream repository. Do not install or run
them.
