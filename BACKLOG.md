# Deferred, deliberately

Things we know about and have decided not to do yet. Each says why, and what
would have to be true before it is worth doing — a backlog entry without that
is just a wish.

## A pre-filter before `proposed`

**Refuse a deck before spending an agent on it.** `inspected` is CPU-only and
nearly free; `proposed` is the first stage that costs API capacity, and a deck
rejected after it has already paid for propose, recipe, degrade, materialise,
reconcile and solvable. Throwing a deck away *before* proposing saves the whole
amount rather than one repair round, which makes it the cheapest cut available.

Candidate rules, none of them calibrated: no shape worth damaging; every
candidate target flagged `hard_target`; too few slides; a deck whose structure
duplicates one already processed.

**Blocked on the corpus, not on the code.** The ten pilot decks were hand-picked
from GitHub mining, so any threshold fitted to them will be fitted to the wrong
distribution — the recorded warning is that raw Zenodo decks "will propose worse
and be rejected more". Calibrate on a real batch, then implement.

Related: the yield measured across repair budgets (0 → 20%, 1 → 30%, 2 → 60%,
3 → 80%) says the repair loop converts well and should not be cut. The waste is
in *which* decks enter, not in how many chances they get.

## Per-stage model and effort assignment

The knob exists — `--model propose=opus,recipe=sonnet`, `--effort`,
`--fallback-model` — and every stage records what actually ran, read from the
log rather than from the flag. **Defaults are unset and stay unset:** the user
has decided to run everything on the session default for now.

The one hypothesis worth testing when it is worth the money: `recipe` is the
only agent stage that implements a decision already made ("implement every one
of them"), where the other four each render a judgement of their own. It still
has to identify shapes against renders, so it is a hypothesis, not a saving.

A clean experiment is ten decks with only `recipe` moved, comparing yield and
cost — roughly $90 at this run's rates.

## Independent votes on the solvability probe

The probe returned four distinct verdicts in ten runs against an unchanged
bundle. Voting — three probes, majority wins — is the reliable fix and is
deferred because `solvable` is already the most expensive stage in the pipeline
and this would triple it. Tightening the rubric is being tried first; if that
does not converge, this comes back.

## Decoupling the test suite from `work/`

Nine tests fail today because decks moved underneath them while agents ran. The
suite reads live pipeline data as fixtures, so a green run means "no deck
changed recently" as much as it means "the code is correct". Needs frozen
fixture decks before the next scale-up, or "the tests pass" stops being
evidence.
