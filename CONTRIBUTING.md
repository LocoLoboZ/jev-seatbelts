# Contributing and feedback

Short version: issues are open and welcome. Pull requests are not being
merged yet. Nothing here is accepted on trust, including your suggestion and
including the author's own.

## What this repo is, so you can judge whether to bother

jev-seatbelts: seven safety gates for a Claude Code development pipeline,
built against the TypeSafe Jev API directly rather than by installing
third-party binaries. All seven are implemented; Gate 7's threshold is
still uncalibrated (see `README.md`, "Status"). The design reasoning,
including the parts that turned out wrong, is kept in the project's
private working notes rather than tidied away and forgotten.

Read `README.md` for current status before filing anything. Several things
that look like bugs are known and already written down.

## Issues are the feedback channel

Use GitHub issues for all of the following:

- A gate that fires when it should not, or fails to fire when it should
- A threshold that is wrong, especially with a counter-example
- A design gap in the pipeline, particularly one not already listed in the
  pipeline-level gaps section of `reference/DESIGN-BASIS.md`
- Evidence that Jev is the wrong tool for a given gate, which is a genuinely
  useful finding here and has already changed the design once
- Anything that reads as overconfident or unevidenced

What helps most, in order: a reproducible case, the actual command or
transcript, and what you expected instead. A bare opinion about a threshold
is much less useful than one labelled example that the threshold gets wrong.

There is no issue template. Write plainly.

## Why pull requests are not open yet

The gates are still being designed and tested by the author. Merging
implementations while the design is still moving would mean reviewing code
against a specification that changes underneath it. That is a bad deal for a
contributor's time, not a judgement about contributors.

If you have written something that works, open an issue describing it and
link your fork. That costs you less and is genuinely wanted. This policy
will change once the gates stop moving - see `README.md`, "Status", for
what is still in flux.

## The bar anything here has to clear

These rules apply to the author's own work and are the reason the repo looks
the way it does.

1. **Deterministic checks stay deterministic.** A model call must earn its
   place. If a regex, a registry lookup, or a tier list answers the question,
   it answers the question. Gate 2 was redesigned after Jev failed a package
   existence test against registry ground truth, and Gate 3 was inverted to
   run deterministic tiers first after a 133k-star project was found gating
   the same commands with zero model calls.
2. **Thresholds are calibrated, not chosen.** Every confidence number in the
   original design was copied from someone else's README. Any number shipped
   here has to be fitted against real labelled examples and verified on a
   held-out split.
3. **Failure behaviour is chosen per gate, never left to chance.** It is
   written down in `reference/DESIGN-BASIS.md`. Gate 3 and any push fail
   closed to a confirmation prompt, because those guard irreversible
   actions. Gates 1, 5, 6 and 7 fail open and log, because none of them is
   the last line of defence. Gate 7 as implemented does exactly that: with
   no key, no rules, no transcript or no budget left, it records the failure
   and allows the stop.
4. **Evidence beats assertion.** A finding that arrives with a failing test
   is worth more than a paragraph explaining why something is wrong.

## Security issues

Do not open a public issue for a gate bypass. See `SECURITY.md`.

## Licence

Contributions are accepted under the MIT licence in `LICENSE`.
