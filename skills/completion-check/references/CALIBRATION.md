# Calibrating Gate 7's thresholds

Standing project rule: no gate ships with a confidence number copied from
someone else's README. The 0.75 block and 0.50 warn defaults were checked
once against real operator-labelled stops (2026-09-26) and kept. They are
checked defaults, not fitted per-rule values. Held-out AUROC for the main
blocking rule was 0.50, so the score cannot tell right blocks from wrong
ones. This page is the procedure for fitting per-rule numbers to this
machine's own history, and the record of that check.

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
   a mistake), `question`, `new_request`, `ack`. The self-tune now uses a
   different option set, see "Self-tuning" below.

   Put the human's reply inside the **question**, never in the shared state.
   Rule questions asked over the same state must not be able to see the
   answer, or the labels leak into the scores and every rule looks perfect.

3. **Score each stop against every rule**, using the same state the live
   hook would build, including the hard evidence fields.

4. **Split.** `bad` = reaction was `push` or `correction`. `good` =
   everything else. In the self-tune, an allowed stop is `bad` when the
   reply was `premature` or `false_claim`. Blocked stops are labelled as
   described below.

5. **Per rule, compute:**
   - **AUROC** over (bad scores, good scores). Below 0.60, the floor the
     self-tune enforces, treat the rule as not separating anything. Do not
     ship it with a threshold. Leave it in shadow mode and reword it.
   - **Threshold** = the score at the 95th percentile of `good`, so roughly
     5% of fine stops are blocked. Tighten `--fp` if that is still too noisy.
   - **Catch rate** = share of `bad` stops at or above that threshold. This
     is what the rule actually buys you.

6. **Hold out.** Fit on one split, report catch and block rates on the other.
   A threshold fitted and reported on the same data is a number about that
   data, not about future stops.

7. **Write the result** into `~/.jev-gates/gate7-thresholds.json`. The
   self-tune does this for you. Setting `GATE7_BLOCK` as
   `substring=0.62,other substring=0.71` instead overrides every tuned
   value. A key matches any rule whose text contains it and the first match
   wins, so make each key unique to one rule.

## Two tiers, and which rules get which

Since the evidence-class change, a rule is either `[observed]` or `[stated]`
(see `rules.md`). Fit them differently, because they can do different things.

- An `[observed]` rule can block. Fit a block threshold for it by the
  procedure above, at the 95th percentile of `good`.
- A `[stated]` rule can only warn. Fitting a block threshold for it is
  meaningless. Fit a warn threshold instead, and there is no need to be
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
gate is for. The agent's last message is passed too, marked untrusted and
as context only. The label is the human's reaction, not a judgement of the
agent's claim, so this is the operator labelling the gate, not the gate
grading itself. Labelling is a separate call from rule scoring, which
reuses the scores the live hook logged, so the reply cannot leak into a
score.

Blocked stops, which have no human reply of their own, are labelled from
what followed: the block led to a new test run (right), or the agent did
nothing after it and the human carried on calmly (wrong). A human verdict
from `gatelog.py --mark` always overrides an auto label.

It runs once a day from SessionStart, in the background, when
`GATE7_SELFTUNE_ENABLED` is set. `install.py` sets it. A plugin install
through `hooks/hooks.json` does not. A threshold moves only when a rule has
enough labelled data on a 70/30 split, held-out AUROC is at least 0.60,
the held-out error does not get worse, the step is at most 0.05, and the
value stays inside 0.55-0.95 (block) or 0.30-0.90 (warn). Values are
written to `~/.jev-gates/gate7-thresholds.json`, every change is logged to
`~/.jev-gates/gate7-tuning.jsonl`, and `--reset` sets them aside. A set
`GATE7_BLOCK` or `GATE7_WARN` replaces every tuned value of that kind, not
only the rules it names.

First full run, 2026-09-26. At the time of the run there were 262 labelled
decisions (237 fine stops, 4 misses, 14 right blocks, 7 wrong blocks). The
label store has grown since, so these figures will not reproduce exactly.
No threshold moved, correctly:

- Right and wrong blocks score the same (0.75-0.85 against 0.75-0.89), so
  raising the block threshold drops good blocks as often as bad ones.
  Held-out AUROC for the test-claim rule was 0.50.
- The 4 misses scored 0.34-0.61 at the time. Blocking at 0.60 would have
  caught three and also stopped about 30 fine turns.

So 0.75 was kept as a checked default. It is not a fitted value, and with
held-out AUROC at 0.50 the score cannot tell right blocks from wrong ones. The wrong blocks
are an evidence problem, not a threshold problem. One cause was found and
fixed the same day: test runs made through the PowerShell tool were not
counted as test runs, so a turn that ran the whole suite was blocked as
"no tests run".

## Status before self-tuning

The fitting mechanism (`jevcal_calibrate_gate7.py`) is built and has been run
live against this machine's real gatelog verdicts. As of 2026-09-23 there
were only 5 real verdicts, below the 20-sample trust floor, so the script
correctly reported its own output as NOT TRUSTWORTHY and no threshold moved.
The self-tune run of 2026-09-26 above superseded that state.
