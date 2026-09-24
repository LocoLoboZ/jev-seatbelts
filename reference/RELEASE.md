# Release procedure

How this project goes public. Written down because it runs once, long after
the reasoning behind it has left anyone's head.

## The rule

**Publish from a fresh baseline, never from this repository's history.**

The private repository keeps its full history. It is the audit record: what
was tried, what was rejected, and the evidence for each. That record has
value and does not get rewritten. The public repository is a separate
repository that starts at a single commit containing only the final tree.

No history rewrite. No force-push. No `filter-repo`. Nothing is destroyed.

## Why, concretely

Two things in this repository's history cannot go public, and deleting the
files in a later commit did not remove either of them.

1. **Third-party MIT files.** Five verbatim `.SKILL.md` copies were added in
   `64ed14c` and removed in `13dfcf7`. They remain fully retrievable from the
   earlier commit. Redistributing them requires carrying the upstream MIT
   copyright notice, which this repository does not have. A fresh baseline
   never contains them, so the obligation never arises.
2. **A local machine path.** Commit `a909f05` contains `__pycache__` blobs
   whose decoded `co_filename` holds the absolute working path of the
   machine this was built on. Untracking them in `dc233a8` left the blobs in
   place.

Both were found by an independent Codex review on 2026-09-20. Neither is
fixable by editing the current tree.

## Procedure

Run from the private working tree, on the commit being released.

```console
git archive HEAD | tar -x -C <empty-dir>
cd <empty-dir>
git init -b main
git add -A
git commit -m "initial public release"
```

Then create a new, empty GitHub repository and push `main` to it. Do not add
the private repository as a remote, and do not push the private branch.

## Checks before pushing the baseline

Run these in the new baseline directory. Each must return nothing.

```console
git rev-list --objects --all | grep -iE "jev-superpowers/.*SKILL|pycache|\.pyc"
git rev-list --objects --all | awk '{print $1}' \
  | while read o; do git cat-file -p "$o" | grep -aq "<your user path>" \
  && echo "HIT $o"; done
```

Also confirm by hand:

- `git log --oneline` shows exactly one commit.
- `git ls-files` contains `LICENSE`, `README.md`, `CONTRIBUTING.md` and
  `SECURITY.md`, and contains no file under `reference/jev-superpowers/`
  other than `SOURCE.md`.
- The three offline checks in `README.md` still pass in the new directory.
- Commit author and email are what you want on a public repository.

Dry run of this procedure on 2026-09-20 against `11c9326` produced one
commit, 36 objects, and no hits on any of the checks above.

## Repository settings on the new public repository

- Enable issues. `CONTRIBUTING.md` points at them as the feedback channel.
- Enable private vulnerability reporting. `SECURITY.md` points at it, and
  without it that file names a button that does not exist.
- Do not enable wikis, projects or discussions. Nothing in the docs refers
  to them.

## Before any of this

The repository is not ready to publish while these are open.

- Gate 7's threshold is uncalibrated. Run the calibration procedure in
  `skills/completion-check/references/CALIBRATION.md` first.
- Re-run an independent adversarial review against the exact commit being
  released, not against a moving working tree.
- `reference/DESIGN-BASIS.md` ships publicly: 28 other files cite specific
  sections of it as their rationale, so excluding it would leave 28 dangling
  references. Instead it has been scrubbed of the machine-specific paths
  and one personal environment-variable name it used to carry - confirm no
  new personal detail has crept back in since, with the same grep used
  during the "Check for a personal machine path" step below extended to
  personal usernames, machine paths, or private environment-variable
  prefixes as well.

## Updating the public repository later

Repeat the same procedure onto a second commit in the public repository, or
copy the changed files across by hand and commit them there. Never merge or
pull between the private and public repositories, in either direction. They
share no history by design, and keeping it that way is the whole point.
