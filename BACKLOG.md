# Deferred, deliberately

Things we know about and have decided not to do yet. Each says why, and what
would have to be true before it is worth doing — a backlog entry without that
is just a wish.

---

# Decided against, 2026-08-05

Not deferred — looked at, and settled. Recorded so they are not raised again
as though they were open.

## A second implementation of what gets scored — **deleted, 2026-08-05**

`tools/` — `build_rollout.py`, `rollout_lib.py`, `rollout_eval.py`, 1268
lines — was the predecessor of `emit.py`, `inventory.py` and `comparators.py`.
It generated the thirteen tasks first published to the rollout repo, each of
which still carries `Regenerate with tools/build_rollout.py`. Nothing outside
it referenced it.

Deleted rather than maintained. Not because it was bad: it was carefully
written, and its own claim that generating every task from one source is what
keeps the copies identical is correct. Because by the time it was found it had
none of `share_forfeited`, `legitimate_variant`, `components_unscoreable`,
`est_steps_measured`, `thumbnail_leak`, `stripped_parts`, `position_slip` or
`_identity_facets` — **every defect this session's audits found was still live
in it**, including the hollow group worth 40% of a deck and reward apportioned
by a step count measured to be wrong.

The general rule this is an instance of: **two implementations of "what gets
scored" drift, and drift silently.** A plausible-looking obsolete evaluator is
more dangerous than an obviously broken one, because it can be run by someone
with no reason to doubt it. Porting the fixes across was the alternative and
was rejected — a maintained copy is still a copy, and the drift restarts.

In git history at the commit that removed it.

## Disclosing a position the reward grades — **acted on, 2026-08-05 (second pass)**

`coherence.position_slip` measures what a deck scores when every restored
shape sits two tolerances out: deck0003 **0.1988**, deck0004 0.5586,
deck0009 0.5512. It is corpus-wide, not one deck's quirk, and deck0009 puts
**39.3%** of its reward on a table centre the bundle never discloses.

The refusal this entry recorded stands and is not reversed: **the comparator
is not loosened.** Fourteen components of weight ≥ 0.10 are scored
`content × position` and thirteen of them *do* disclose the coordinate, so
grading centres by distance would loosen all fourteen to fix one — and pay
`wrong_params` for the privilege.

What this entry also said was that the fix "would have to be upstream, in
`materialise` shipping a masked reference render where nothing else pins the
position". **That has now been built** (`assets.anchor_pass`), at the user's
decision, and it is recorded here rather than replacing the entry because two
details came out differently from the sketch:

* **What is scored is measured, not declared.** The producer used to decide
  what to disclose from `proposal.json` — an agent's judgement, and a looser
  source than the one the scorer uses. It now moves the ground-truth shape
  four tolerances and asks the real comparator whether the score falls. A
  second table of "which operators grade position" would drift from
  `comparators.py` in silence; a measurement cannot.
* **What is anchored is three readings a solver can actually make**: a
  reference render of that slide, a surviving shape occupying the identical
  box, or every one of the four coordinates individually reproduced by some
  survivor. Checked against four independent solvability probes, it reproduces
  every one of their determinate / not-determinate calls on deck0001, deck0003
  and deck0009 — with no agent.
* **It ships numbers, not the masked render.** The mask is padded 0.06in and
  drawn at 130 dpi, so a hatch box read off one is good to roughly 0.06in
  against a `POS_TOL` of 0.01in — six times too coarse to earn the mark it
  exists to make earnable. `pNN-frames.csv` is exact, costs no render time
  (the objection this entry raised), and discloses strictly less than a render
  does: where the element goes, and nothing about what it is.

**The open half, which is the part to act on.** Three of the four decks
already through `packaged` grade coordinates their bundle never discloses:
deck0002 (18 of 23 position-graded components unanchored), deck0006 (7 of 49),
deck0005 (3 of 29). deck0004 and deck0007 are clean. They have not been
touched — they carry no code fingerprint, so nothing marked them stale — and
re-materialising a packaged deck costs a round of the gates, which is a
decision rather than a chore.

**And one this rule does not reach.** A reference render counts as an anchor
here because the pipeline and its probes have always treated it as one, and
the packaged decks are built on that. But the same arithmetic that rules out
the masked render applies to every render: 130 dpi is 0.0077in per pixel, so a
box measured off one is good to about `POS_TOL` at best and to ~0.06in when
the box is a padded hatch. Thirteen of deck0009's twenty-two position-graded
components are anchored by nothing else. Deciding whether a render is an
anchor at 0.01in needs its own evidence, and would change far more than three
decks.

## A picture facet that is not byte-exact

deck0008 is unearnable: the two withheld blobs are exactly what `c001`,
`c009` and `c002` score, `_facet_picture` is byte-exact, and the instruction
tells the solver to cut the image out of a reference screenshot — which
produces different bytes. **58.3% of that deck cannot be earned by the route
its own instruction prescribes**, and the only way to earn it is to obtain
bytes the cheat gate correctly refuses.

Not fixed, at the user's decision: **a deck like this is abandoned.** A
perceptual comparison needs a decoder, and `inventory.py` / `comparators.py`
are stdlib-only by contract because `emit` pastes them into a task file that
runs where `python-pptx` may not exist — 49 of the corpus's 318 slide
pictures are vector metafiles with no pixels until something renders them.
Adding a dependency to the evaluator would trade a whole design property for
one deck.

## Cutting the tail below `--timeout`

`--deck-deadline` bounds *spend*, not wall clock: it is enforced between
stages, so a deck already inside a forty-minute agent stage finishes it.
Actually cutting the tail would mean tightening `--timeout` as well, which
kills legitimate long stages.

Not done, at the user's decision. Revisit only with a real distribution of
stage durations from a cold corpus run — the pilot's stages are not that
distribution.

---

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
