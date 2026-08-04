# What you have to know before writing the reward

The reward-function stage has not been built. This file records **the
constraints you will run into while building it**, and the numbers that have
**already been measured** — so that nobody has to discover them again, or pick
a tolerance out of the air.

---

## 1. Underneath "tolerance" there are two different things

Mixing them up is where a reward starts getting hacked.

| | what it is | how to handle it |
|---|---|---|
| **noise tolerance** | the file changed, but nobody did it (the renderer changed it by itself on open-and-save) | **subtract it, do not tolerate it** — compare against `roundtrip(gt)` as the baseline |
| **equivalence tolerance** | the agent got it right but wrote it differently (a theme colour resolved to explicit sRGB, a rebuilt shape with different XML structure, a cropped picture turned into a blipFill) | **this is what genuinely needs a tolerance** |

Handling noise by "widening the band" too is holding a door open for everyone.
Only subtracting the baseline is immune to noise.

---

## 2. The numbers already measured

### 2.1 WPS: 10 decks, 0.0%

`pptxgym/wps_roundtrip.py`. WPS has no usable headless conversion on Linux
(`wpp --headless` spins silently, and `--convert-to` is simply not implemented
in this Linux build), so it takes the same route the solver takes: on a virtual
display (Xvfb + xdotool), open the file, type two characters into the notes and
delete them again to light up the dirty flag, and click save.

**On open-and-save, WPS moved 0.0% of shapes. 10 / 10. No displacement, no
resizing, no losses.**

| deck | shapes | **WPS** | LibreOffice | LO p90 | kinds LO moves |
|---|---|---|---|---|---|
| deck0001 | 92 | **0.0%** | 7.6% | — | — |
| deck0010 | 76 | **0.0%** | 7.9% | — | — |
| deck0007 | 119 | **0.0%** | 8.4% | — | — |
| deck0008 | 93 | **0.0%** | 11.8% | 0.146in | textbox |
| deck0004 | 130 | **0.0%** | 28.5% | 0.130in | textbox |
| deck0005 | 210 | **0.0%** | 38.1% | 0.845in | table, textbox |
| deck0006 | 743 | **0.0%** | 42.4% | — | — |
| deck0002 | 257 | **0.0%** | 46.3% | 0.262in | table, textbox |
| deck0003 | 140 | **0.0%** | 60.7% | 0.313in | textbox |
| deck0009 | 182 | **0.0%** | 61.5% | 0.570in | table, textbox |

In the WPS column, `counts` / `by_kind` / `drift` are all empty and `verdict`
is `stable` throughout. The per-deck numbers are in
`work/deck00NN/roundtrip-wps.json`, LibreOffice's in `roundtrip.json` in the
same directory.

**This is not "nothing changed because nothing was saved".** deck0001's package
went 4.04MB → 3.81MB, 81 parts have different bytes, and `customXml/` was
dropped entirely. WPS re-serialised the whole package — and then moved not one
shape. (Incidentally: WPS, like PowerPoint, makes Ctrl+S a no-op when the file
has not been modified, which is why those two "type and delete" keystrokes in
the script are mandatory.)

### 2.2 LibreOffice: that 33% is the proxy's own reflow

This section used to say "on open-and-save alone, a median of 38% of shapes get
moved". Two things changed: after fixing the comparator's placeholder key
(section 7, first item), the recomputed median is **33.3%**, range 7.6% –
61.5%; and more importantly, **this entire column does not describe the
evaluation environment**. Tasks are solved and scored in WPS, and WPS moves
0.0%. LibreOffice's 33% is LibreOffice reflowing text against its own font
metrics — the behaviour of the proxy, not of the environment.

So the old sentence "once we have WPS numbers, this worst-case set of
constraints can probably be relaxed" was backwards. **Having them, we tighten.**
Setting the position tolerance from LO's `p90_in` would give a band of
**0.13 – 0.85 inches**. Tolerance is exactly the attack surface for reward
hacking (section 4), and the settled standard is anti-hacking over covering
equivalent solutions — building a 0.85-inch band on a renderer that takes no
part in scoring at all is a pure giveaway.

### 2.3 The LO numbers stay, but for a different purpose

They are not waste; they are now a **corpus-fragility signal**:

- displacement and resizing happen only to text boxes and tables, in all 10
  decks without exception — they are the only things that reflow against font
  metrics;
- `missing` and `added` are equal and paired in every deck, and the details are
  mostly the same kind on the same slide: largely key churn the comparator
  failed to match up, not anything actually lost;
- exactly one genuine type change: deck0008 slide 15, where 6 pictures were
  written out by LO as autoshapes.

**How to read it: the harder LO mangles a deck, the likelier that deck is built
on fragile constructions** (deeply nested auto-wrapping text boxes, tables that
size to their content, unusual picture wrapping), and it is easier to route
tasks around those places. **It is no longer the source of any tolerance.**

`digest.json → deck_summary.renderer_drift` still stores LO's numbers and the
fields do not change (the conservative floor in the degradation recipe,
`amplitude_in ≥ max(0.8, 4 × p90_in)`, still holds — it only makes changes
larger), but its meaning becomes "how fragile this deck is", not "the software
will move things this far by itself".

### 2.4 The opening: 0.0% only holds for this machine's font set

WPS reported this itself at startup:
`Some formula symbols might not be displayed correctly due to missing fonts Symbol`.

Font substitution changes text metrics, and **text reflow is the entire source
of LO's 33%**. So 0.0% is a result for "this set of fonts on this machine", not
a universal property of WPS.

**This has now been measured, it is the same mechanism, and it is not small.**
Two sets of measurements, both using `roundtrip.py`'s `_facts`, on the same
English text in the same 4-inch-wide, 18pt, autofit-enabled text box, put
through LibreOffice:

| changing only the font name on the run | present on this box | height after saving |
|---|---|---|
| DejaVu Sans / DejaVu Serif | yes | 1.898 in |
| Garamond / SimSun / Meiryo UI / MS PGothic / Batang | no | 1.898 in (all land on the same fallback) |
| Lato / Liberation Serif | yes | 1.598 in |
| **Arial Narrow** | **no** | **1.298 in** |

**Not one character of text changed, and the shape's height differs by 0.600
inches.** The last row is the key one: Arial Narrow is not on this box, but the
face it substitutes to has different metrics from the generic fallback — which
is to say, **what determines the geometry is not "is the font file present", it
is "what face does this font name resolve to"**. The moment the evaluation VM
has Arial Narrow installed (or is missing Lato), the same deck saves out at a
different height.

Missing glyphs do the same thing, and more directly. Same box, same 40
characters, 18pt:

| | glyphs present on this box | height after saving | centre |
|---|---|---|---|
| `中` × 40 | yes | 0.998 in | 1.499 in |
| `あ` × 40 | yes | 0.998 in | 1.499 in |
| `한` × 40 | **no** | **0.698 in** | **1.349 in** |

`.notdef`'s advance width is the fallback font's own default and has nothing to
do with the width of the glyph it stands in for, so the line count changed, the
box shrank, and the centre moved 0.150 inches.

Against `POS_TOL = 0.01in`: 0.600 inches is 60× that, 0.150 inches is 15×; and
0.600 inches falls squarely inside the LO p90 band (0.13 – 0.85in) that section
2.2 already refused to use as a tolerance. **The conclusion: font differences
are not noise that "widening it a bit more" can cover. They are the same
magnitude and the same mechanism as the renderer drift we measured. The only
safe move is to make the fonts identical at both ends, not to widen the
tolerance** (section 3① still holds; this only puts a price tag on it).

Incidentally, section 3②'s "a different set of fonts gives a different value"
now has a number: it is the 0.600 inches above. The size of an autofit text box
**cannot be a scored component**, and that is not conservatism, it is supported
by measurement.

Things that must be done before "zero tolerance" is written into any
comparator:

0. First confirm the renders themselves are trustworthy —
   `python -m pptxgym.fonts <deck>` (`pptxgym/fonts.py`). This box is missing
   Hangul syllables, Thai and Devanagari; slides with missing glyphs render as
   hollow boxes, and **the proposal, the reference image and the solvability
   probe will all make confident judgements against those boxes**. Store this
   report in the repo alongside the font list below;
1. dump a font list from this machine with `fc-list : family` and store it in
   the repo together with WPS's missing-font warning above;
2. dump the same on the evaluation VM and take the difference. **Comparing file
   lists is not enough** — you also have to run `fc-match <name>` on every font
   name that actually occurs in the corpus, because what matters is **what it
   resolves to**; Arial Narrow in the table above is exactly the case where the
   file lists can be identical at both ends and the resolution differs;
3. if the difference is non-empty, run `wps_roundtrip` on these same 10 decks
   on the VM and re-confirm the 0.0%;
4. only once the difference is empty, or the VM also measures 0.0%, may
   positional comparisons use a floating-point-noise tolerance.

**Until step 3 is done, "the evaluation VM is also 0.0%" is an assumption, not
a measurement.**

---

## 3. Three design rules

**① The default tolerance is 0, not some measured band.**
WPS moves nothing on open-and-save, so the default tolerance on position /
size comparisons is the floating-point-noise band (`POS_TOL = 0.01in` in
`roundtrip.py` is that band), not "this deck's p90". **Any tolerance wider than
that needs measured evidence from WPS behind it** — LO's p90 does not count as
evidence (section 2). What genuinely needs a band is equivalence tolerance (the
right-hand column of section 1), and that band is set by semantics, not by a
renderer.

**② What the application decides is simply not scored.**
The size of an autofit text box is computed by the application and is not the
agent's business. This is not "widen the tolerance", it is **remove it from the
scored components**. Scoring something it cannot control makes the score noise.
`digest`'s `type_style.autofit` already records this.
WPS did not change the size of any autofit text box in this round, but that is
no reason to say it can be scored — it is still a value the application
computed from the current fonts, and a different set of fonts gives a different
value (see 2.4).

**③ Where you can judge the relation, do not judge the absolute value.**
Judge "alignment / order / relative spacing against the surviving elements of
the same kind", not EMU coordinates.
Six cards drifting 0.2in together leaves the relations unchanged and the score
unmoved.
**This means positional scoring barely needs a tolerance at all**, which is
steadier than any band.
WPS's 0.0% makes this rule cheaper, but do not use that as a reason to turn
around and judge absolute coordinates: judging relations is also immune to font
differences, and absolute coordinates are not.

---

## 4. A tolerance has to be proven safe

Tolerance is exactly the attack surface for reward hacking: every notch of
widening adds another region where you get paid for doing nothing. The settled
standard is **anti-hacking over covering equivalent solutions**, so:

> **No tolerance may let "do nothing" score.**

Two mechanisms guarantee it, both implemented in
`pptx-tasks/scaling/pipeline/`:

- **floor normalization** — subtract the broken file's own score;
  `score(input)` must be 0 after normalisation
- **adversarial battery** — `noop` ≈0, `wrong_params` low.
  **If widening some tolerance makes the `noop` score go up, that tolerance is
  wrong.**

---

## 5. Five probes: turning tolerance from a judgement call into a measurable constraint

| probe | assertion |
|---|---|
| `equivalent_repr` | an equivalent way of writing it → 1.0 |
| **`roundtrip_identity`** | **the ground truth through the application once → still 1.0** ← new, suggested as the first one to write |
| `input_floor` | the broken file → 0.0 |
| `scripted_restore` | a perfect restoration → 1.0 |
| `blind_solver` | no view of the reference → low (answer-leak detection) |

**Tune the tolerance to exactly the point where all five hold — no more, no
less. One notch more is a notch of room for hacking.**

`roundtrip_identity` is suggested first because it is the cheapest (it needs no
agent at all), and because it will point straight at **which components should
not exist in the comparator in the first place** — if the ground truth loses
points by going through the application once, the problem is not that the
tolerance is too narrow, it is that **the wrong thing is being compared**.

---

## 6. Where the existing code is

`/home/yitongli/XLANG/pptx-tasks/scaling/pipeline/`:

| file | what it is | status |
|---|---|---|
| `evaluator.py` | registry-driven scoring | validated on 4 tasks |
| `verify.py` | 4 probes + an 8-case adversarial battery | same, `accept` criteria |
| `ops.py` | 16 operators and their comparators | **not the same set** as this repo's `degrade_exec`; align before moving anything |

**Every one of them has to be validated separately before being brought in.**
"A stage that has never been run has no business in a pipeline other people are
meant to use" is a settled principle of this project.

---

## 7. Things that will still bite

These are the traps already stepped in along this chain, and the reward stage
will certainly meet them again:

- **Date / slide number / header / footer placeholders: judge the role, not the
  text inside** — the text in this kind of placeholder is generated by the
  application and nobody controls it. So it **can be neither a matching key nor
  a thing to compare**. We stepped in this one twice: first LibreOffice
  re-evaluated the date field, and 31 placeholders nobody had touched were
  reported as 31 deletions plus 31 additions; after switching to detecting
  `a:fld` the LO side was fine — and then WPS wrote that field without a cached
  literal, the detection silently stopped working, and 81 date / slide-number
  placeholders across 4 decks were again reported as deletions plus additions,
  **a renderer that changed nothing scoring a 36% change rate on one deck**.
  Keys are now grouped by the placeholder's **role** (`APP_FILLED` in
  `roundtrip.py`). Note the scope of the exemption: **its geometry is compared
  as usual; the only thing exempted is that generated text.**
- **`a:endParaRPr`** — the end-of-paragraph run properties, not `a:rPr`.
  Walking only `a:rPr` misses it. The solvability probe caught an answer leak
  through exactly this.
- **Answer leaks** — deleting the shape without deleting the relationship
  leaves the picture bitmap / SmartArt's `data*.xml` / the chart's embedded
  workbook in the package, and `unzip` reads them. `pkg_check` checks for this,
  but **the comparator should also assume the solver has unzipped it**.
- **For chart data read `numCache`, not the embedded workbook** — the cache is
  what gets rendered; the workbook may be stale.
- **`delta.json` is the shared foundation of the degrader and the scorer** —
  every change together with the value it had before. floor normalization
  depends on it. So **there can be no complete information barrier against the
  scorer**; our equivalent barrier is that the comparator is written against
  **operator semantics** and may not look at the specific recipe.
- **Equivalent representations will come and find you** — while writing the
  comparators in `roundtrip.py`, `"none"` fill vs `null` and implicit geometry
  vs explicit `rect` produced 87% false positives on the first try.
  **The comparator will step in the same hole again.**
