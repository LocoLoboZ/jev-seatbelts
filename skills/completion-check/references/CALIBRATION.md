# Calibrating Gate 7's thresholds

Standing project rule: no gate ships with a confidence number copied from
someone else's README. The 0.75 in the script is a placeholder chosen to
under-fire. This page is the procedure that replaces it with numbers fitted
to this machine's own history.

Method adapted from `noplan-inc/limpet`'s `calibrate` subcommand, read
directly. See `PROVENANCE.md`.

## The idea

You cannot pick a good threshold by taste, because the cost of the two
mistakes is not symmetric. A gate that fires on a fine stop is an
interruption the operator learns to ignore, which silently kills the gate. A
gate that misses a bad stop does nothing at all. So fit the threshold to an
accepted false-positive rate, and measure whether the rule separates good
stops from bad ones at all.

## The procedure

1. **Harvest past stops.** Every point in `~/.claude/projects/*/*.jsonl`
   where an assistant message is followed by a human message. The human's
   next message is the label.

2. **Label each stop, without letting the labeller see the rules.** Ask Jev
   one Choice question: how did the human react? Options: `push` (told the
   agent to do what it should already have done), `correction` (pointed out
   a mistake), `question`, `new_request`, `ack`.

   Put the human's reply inside the **question**, never in the shared state.
   Rule questions asked over the same state must not be able to see the
   answer, or the labels leak into the scores and every rule looks perfect.

3. **Score each stop against every rule**, using the same state the live
   hook would build, including the hard evidence fields.

4. **Split.** `bad` = reaction was `push` or `correction`. `good` =
   everything else.

5. **Per rule, compute:**
   - **AUROC** over (bad scores, good scores). Below about 0.55 the rule does
     not separate anything. Do not ship it with a threshold; leave it in
     shadow mode and reword it.
   - **Threshold** = the score at the 95th percentile of `good`, so roughly
     5% of fine stops are blocked. Tighten `--fp` if that is still too noisy.
   - **Catch rate** = share of `bad` stops at or above that threshold. This
     is what the rule actually buys you.

6. **Hold out.** Fit on one split, report catch and block rates on the other.
   A threshold fitted and reported on the same data is a number about that
   data, not about future stops.

7. **Write the result** into `GATE7_BLOCK` as
   `substring=0.62,other substring=0.71`. The substring must be a unique
   prefix of the rule text.

## Two tiers, and which rules get which

Since the evidence-class change, a rule is either `[observed]` or `[stated]`
(see `rules.md`). Fit them differently, because they can do different things.

- An `[observed]` rule can block. Fit `GATE7_BLOCK` for it by the procedure
  above, at the 95th percentile of `good`.
- A `[stated]` rule can only warn. Fitting a block threshold for it is
  meaningless. Fit `GATE7_WARN` instead, and there is no need to be
  conservative, because a warn costs a log line and never halts anything.
  A looser warn threshold buys more signal for the feedback loop.

Fit per rule, never one number for the gate. The rules measure different
things, and a single threshold averaged across them destroys the finding.

## Do this before fitting anything

Fit and you have a number. You still have no way to tell whether the number
is good. Two rates decide that, and neither is computable from the decision
log alone.

    python lib/gatelog.py --list     # decisions awaiting a verdict
    python lib/gatelog.py --mark <id> <verdict> "why"
    python lib/gatelog.py --rates    # escape rate, false-block rate

Accumulate real verdicts first. Escape rate is what the gate is for, target
zero. False-block rate is what kills it, because an operator learns to
ignore a gate that nags. A threshold moved without both numbers is a
preference, not a calibration.

Note the deliberate asymmetry with the harvest above. The harvest labels
past stops automatically, which is how you get enough data to fit at all.
The verdicts are written by a human, which is how you find out whether the
fitted number was right. A gate must never adjudicate itself.

## Re-check on model change

`MODEL` in `lib/jevgate.py` is pinned to a version, not the `jev-latest`
alias, precisely so a release cannot move every score underneath a fitted
threshold. The response header still reports the served model, and that
value is logged on every call, which is how a server-side substitution
would show up.

Re-run this whole procedure whenever the pin is deliberately bumped, or
whenever the served version in `~/.jev-gates/gate7.jsonl` stops matching the
pin. Treat a large shift in the good-stop score distribution as a
calibration failure rather than a new baseline. Diff the vendor's jaggedness
page for the two versions first, since that is free and often explains the
shift before a single probe is spent.

## Self-tuning (2026-09-26)

`scripts/gate7_selftune.py` now does the harvest above for real, with one
change of question. The push/correction/question/new_request/ack labels
proved too coarse on a 41-stop trial: "push" also caught ordinary
go-aheads such as "fix all review items as recommended", so half the
"miss" labels were wrong. Jev is now asked whether the human's reply says
the stop was premature or a claim was false, which is the one thing this
gate is for. Jev reads the human's reply, not the agent's claim, so this
is the operator labelling the gate, not the gate grading itself.

Blocked stops, which have no human reply of their own, are labelled from
what followed: the block led to a new test run (right), or the agent did
nothing after it and the human carried on calmly (wrong). A human verdict
from `gatelog.py --mark` always overrides an auto label.

It runs once a day from SessionStart, in the background, when
`GATE7_SELFTUNE_ENABLED` is set. A threshold moves only when a rule has
enough labelled data on a 70/30 split, held-out AUROC is at least 0.60,
the held-out error does not get worse, the step is at most 0.05, and the
value stays inside 0.55-0.95 (block) or 0.30-0.90 (warn). Values are
written to `~/.jev-gates/gate7-thresholds.json`, every change is logged to
`~/.jev-gates/gate7-tuning.jsonl`, `--reset` sets them aside, and an
operator's `GATE7_BLOCK`/`GATE7_WARN` always wins.

First full run, 2026-09-26: 262 labelled decisions (237 fine stops, 4
misses, 14 right blocks, 7 wrong blocks). No threshold moved, correctly:

- Right and wrong blocks score the same (0.75-0.85 against 0.75-0.89), so
  raising the block threshold drops good blocks as often as bad ones.
  Held-out AUROC for the test-claim rule was 0.50.
- The 4 misses scored 0.34-0.61. Blocking at 0.60 would catch three and
  also stop about 30 fine turns.

So 0.75 is about right for what the score can separate. The wrong blocks
are an evidence problem, not a threshold problem. One cause was found and
fixed the same day: test runs made through the PowerShell tool were not
counted as test runs, so a turn that ran the whole suite was blocked as
"no tests run".

## Status before self-tuning

The fitting mechanism (`jevcal_calibrate_gate7.py`) is built and has been run
live against this machine's real gatelog verdicts. As of 2026-09-23 there are
only 5 real verdicts, below the 20-sample trust floor, so the script
correctly reports its own output as NOT TRUSTWORTHY. No threshold has moved.
The gate still operates on the placeholder 0.75 and should be treated as
provisional until more verdicts are marked (`python lib/gatelog.py --mark`)
and the script is re-run.
