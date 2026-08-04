---
name: ppt-degrade-recipe
description: Turn a PPT task proposal into an executable degradation recipe — pick the shape paths, choose the ops, run it, look at the render, fix what is wrong. Use after ppt-task-proposal, when a proposal has to become a real broken file.
---

# Turning a proposal into an executable recipe

The proposal is written in plain English: "knock the six member cards apart",
"delete the chart on slide 4, keep the title". Your job is to turn that into a
**recipe that runs**, and then to **actually look** at the result and check it
is right.

This step and the proposal are **two layers, and they must not be mixed**. The
proposal layer does not consider implementation difficulty — deliberately, so
that good ideas are not cut down to the tooling's size. Here at your layer,
implementation difficulty becomes a problem for the first time.

---

## Inputs

| file | what it is |
|---|---|
| `proposal.json` | what is to be implemented. Look at `tasks[0].degradations`; each entry's `what_breaks` is the specification |
| `digest.json` | the shape list for each slide; **`path` is the address used in the recipe** |
| `renders/p-NN.png` | the renders |
| `source.pptx` | the original deck, and also the ground truth. **Never write it** |

## Output

`recipe.json`, plus **your own eyes' confirmation** that the degradation matches
the proposal.

---

## Discipline number one: look at the render before choosing a path

What `path 3` is in the digest **can only be recognised against the render**.
Choosing a path from the JSON alone has a high chance of being wrong, and being
wrong raises no error — it simply deletes something else.

The recommended order:

1. Read each `what_breaks` in `proposal.json` so you know what has to break
2. Open the render of the relevant slide and **look**
3. Match shapes to it from the digest by position, size, text and image hash
4. Write the recipe, run it, **and render again to see the result**
5. If it is wrong, change the paths and go again

Step 4 is not optional.

---

## Recipe format

```json
{"name": "<the task name from the proposal>", "seed": 41,
 "slides": {
   "7":  [{"op": "delete", "deg": "d1", "paths": ["3", "13"],
           "_why": "the photographs of the three milestones"}],
   "12": [{"op": "scatter", "deg": "d2", "paths": ["4", "6"],
           "amplitude_in": 1.2, "_why": "..."}]
 },
 "smartart": [{"slide": 19, "deg": "d4", "drop_text": ["Ingest"], "_why": "drop only the third column"}],
 "chart":    [{"slide": 5, "deg": "d1", "drop_name": ["Data Size"], "_why": "one series short"}],
 "reorder_slides": {"deg": "d3", "swap": [[3, 4]]},
 "clear_notes": [{"deg": "d3", "slides": [7, 8]}],
 "layout": [{"deg": "d2", "layout": "Title and Content", "delete_paths": ["1"]}]}
```

- the keys of `slides` are **1-based slide numbers**, consistent with the
  renders and with the `shapes` command
- **every step must carry `deg`**, filled in with the `id` of that degradation in
  `proposal.json` (`d1`/`d2`…)
- **every step must carry `_why`**, saying on what basis you chose those paths
  (which render you compared, which hash)
- `seed` makes `scatter` reproducible

---

## `deg`: which degradation a change belongs to

`deg` is **the only machine-verifiable link between the instruction and the
file**. The executor stamps it onto **every single** delta record this step
produces, so any change in `delta.json` can say "this is what which sentence of
the instruction asked for". The reward stage asks in both directions, and
neither may be missing:

- **every scored change must correspond to a degradation the instruction really
  asked for** — otherwise the scorer is judging a job nobody was asked to do
- **every degradation must produce at least one scorable change** — otherwise
  the instruction asked for a job that earns nothing

`check_recipe` enforces exactly these two: a missing `deg`, or one filled in
with an id that is not in the proposal, is a straight `failed`; a degradation in
the proposal **that no step claims** is likewise `failed`, and the error names
the id.

**Writing "this is d1" in `_why` does not substitute for the field.** The first
ten decks' recipes all happened to put the id at the start of `_why`, but that
is a habit, not a criterion: the next person writing "seal up the leaks from
d2/d3 together" matches two, and writing "put that row back" matches none — both
are guesswork.

**One `deg` per step.** A step serving two degradations at once (typically one
`strip_animation` sealing the animation leaks from two separate deletions)
splits into two steps, each claiming one — do not stuff two ids into one field.

---

## Operator table

### Shape level (written inside `slides`)

| op | parameters | purpose |
|---|---|---|
| `delete` | `paths` | delete shapes; the parts and relationships of pictures/charts are cleared too |
| `scatter` | `paths`, `amplitude_in` (default 0.9) | push away from the original position at random |
| `move` | `paths`, `dx_in`, `dy_in` | directed translation |
| `resize` | `paths`, `factor`, `factor_y`, `keep_center` | scale |
| `swap` | `pairs`: `[["3","4"]]` | swap the positions of two shapes |
| `rotate` | `paths`, `angle` (degrees, **not** `deg` — that is the id of the degradation this step belongs to) | rotate |
| `zorder` | `paths`, `to`: `"back"`/`"front"` | change stacking order |
| `ungroup` | `paths` | break up a group, leaving the children at their absolute positions |
| `clear_text` | `paths`, `keep_first_paragraph` | empty the text, keeping the shape |
| `set_text` | `paths`, `text` | replace the text |
| `set_font` | `paths`, `font`, `size_pt`, `bold`, `italic`, `underline`, `color` | every run in **the whole shape** |
| `text_runs` | `paths`, `paragraphs`:[indices] or `match`:[substrings], `delete`:true or the same style parameters | **some** paragraphs |
| `strip_effects` | `paths`, `flatten_gradient` | remove shadow/glow/3-D/gradient |
| `recolor` | `paths`, `to`: `"#RRGGBB"` | change the fill colour |
| `outline` | `paths`, `mode`:`"remove"`/`"set"`, `color`, `width_pt` | change/remove the outline |
| `crop` | `paths`, `mode`:`"reset"` or `l`/`t`/`r`/`b` (percentages) | picture crop |
| `table_drop_rows` / `table_drop_cols` | `paths`, `rows`/`cols`:[indices] | **delete** rows/columns |
| `clear_table_cells` | `paths`, `rows`, `cols` | **empty** cells (without deleting) |
| `detach_connector` | `paths`, `nudge_in` | knock a connector off its target |
| `anim_drop_steps` | `steps`: [1-based step numbers] | drop some build steps |
| `strip_animation` / `strip_transition` | — | remove the whole slide's animation/transition |
| `blank_slide` | `keep_paths` | empty the whole slide, keeping the named shapes |

### Inside composite objects (written at the top level)

| key | parameters | purpose |
|---|---|---|
| `smartart` | `slide`, `drop_text`:[substrings] or `drop_id`, `graphic` | **delete certain nodes inside a SmartArt**, leaving the rest in place |
| `chart` | `slide`, `drop_name`/`drop_index`, `strip`:[`legend`/`title`/`data_labels`/`gridlines`/`axis_titles`] | **delete a series or a chart accessory** |

### Whole deck

| key | parameters |
|---|---|
| `reorder_slides` | `{"swap": [[3,4],[7,9]]}` |
| `delete_slides` | `[12, 15]` |
| `clear_notes` | `[{"slides":[7,8]}]` |
| `layout` | `[{"layout":"layout name","delete_paths":["1"]}]` — affects every slide using that layout |

**When the proposal asks for a "local edit", first check whether the two tables
above have a key for it, and only then consider deleting the whole thing.**
`smartart` and `chart` exist precisely for this: deleting the whole thing
destroys the surviving elements, which were the anchor, and turns "complete the
pattern" into "rebuild from nothing" — the difficulty and the point of the task
both change.

---

## Commands

```bash
# the shape table for certain slides (path / position / size / z-order / font / image hash / text)
python -m pptxgym.tools shapes <deck-dir> 7 12 19

# what nodes/series are inside a SmartArt or a chart
python -m pptxgym.tools smartart <deck-dir> --slide 19
python -m pptxgym.tools chart    <deck-dir> --slide 5

# trial-run the recipe + integrity gate (writes into trial/, does not commit, does not change pipeline state)
python -m pptxgym.tools trial <deck-dir>

# render the affected slides side by side (original vs broken)
python -m pptxgym.tools pair <deck-dir> 7 12
```

`trial` must print `gate=ok`. ANSWER LEAK or DEAD RELS means the deletion was
not clean.

**Do not run `pptxgym degrade`.** That is the orchestration layer's command for
committing artefacts; your job is to get the recipe right, and whether it is
committed is for the pipeline to decide. If you run it, the deck lock will
refuse you anyway.

---

## The amplitude floor for positional degradations

`digest.json`'s `deck_summary.renderer_drift` records **how far this deck drifts
purely from being opened and saved by the software**. It is now recorded **per
renderer**, with `governs` marking which one has the say:

> **The task is solved and scored in WPS, so only `renderer_drift.wps` can
> constrain positional degradations. `renderer_drift.libreoffice` is a
> corpus-fragility signal, not a tolerance, and must never be used to set an
> amplitude.**

**Displacement caused by the software looks exactly like displacement caused by
the agent** — but it has to be displacement caused by **the software that does
the scoring**. Measured across 10 decks: **WPS opens and saves and does not move
a single shape**; LibreOffice moved 7.6%–61.5%, almost all of it text boxes and
tables reflowing against font metrics. Setting the amplitude from LibreOffice's
p90 would give a floor of 0.13–0.85 inches — **that is the proxy renderer's
noise, not a property of this deck**.

**① The amplitude floor for `scatter` / `move`:**

```
amplitude_in ≥ max(0.8, 4 × renderer_drift.wps.drift_in.p90_in)
```

- **`wps.changed_frac` is 0** (the case for all 10 decks so far): `drift_in` is
  empty, so **do not go looking for a `p90_in` that does not exist** — the floor
  degenerates to the constant 0.8in. On these decks positional degradations have
  no renderer noise; propose them.
- **`wps.changed_frac` is not 0**: this is the real constraint. On a deck whose
  p90 is 0.57in, the amplitude has to be ≥2.3in. **If that slide has no room for
  a 2.3in displacement, that slide should not carry a positional task** — pick a
  different target, or switch to a deletion/rebuild degradation.
- **`governs` is null** (this deck has never been measured on WPS): **do not
  substitute LibreOffice's numbers.** Make the displacement large and obvious
  (≥1.5in is a safe starting point) and write "WPS drift unmeasured" in `_why`.
  To measure it: `python3 -m pptxgym.wps_roundtrip <deck>/source.pptx`.

**② Prefer moving things that do not drift.**
`renderer_drift.wps.kinds_that_move` lists the kinds that drift on this deck
**under WPS**; empty means nothing drifts. Whatever is in
`libreoffice.kinds_that_move` is **not** a reason to avoid a shape kind.

Positional degradations should generally prefer **pictures, cards and diagrams**
as targets. Caption text that travels with a picture can move along with it, but
it should not be the object being scored — spell out in `_why` which ones are
the primary targets.

---

## Three hard rules

**1. Paths are positional indices, indexed once and once only.**
`delete` shifts every shape after it. The executor indexes once **before any of
a slide's steps begin**, so every path you write follows **the numbering in the
original digest**; do not try to work out "what number does it become after the
deletion".

**2. If you cannot do it, write that down honestly; do not pretend you did.**
Spell out in `_why` what was approximated and why. For instance, if you deleted
a whole SmartArt that should have been edited in place, say so: "the anchor is
gone, the difficulty goes from completing to rebuilding, and the instruction
needs rewriting to match".
**Under-reporting is far worse than not being able to do it** — not being able
to do it is a tooling gap; under-reporting puts a wrong label into the data.

**3. Do not touch `hard_target`.**
Shapes marked `hard_target` in the digest (OLE objects, custom Bézier geometry)
cannot be produced in the GUI, and the proposal usually says outright that they
are context, not targets. Deleting one makes the task unsolvable.

---

## Common correspondences

| what the proposal says | what you usually do |
|---|---|
| "some things have gone missing" | `delete` |
| "knocked apart / misaligned" | `scatter` (+ the odd `resize` to create inconsistent sizes) |
| "a whole block hollowed out" | `delete` a group, or `blank_slide` with `keep_paths` |
| "one column / one tier is gone" | `smartart` or `chart`, **not** `delete` |
| "the emphasis was wiped" | `text_runs` with `bold:false` **and** `underline:false` |
| "the style no longer matches the other slides" | `set_font` / `recolor` / `strip_effects` |
| "a few build steps are missing" | `anim_drop_steps` |
| "the order is scrambled" | `reorder_slides` |

When removing bold you **must** remove the underline with it: removing only the
bold leaves `u="sng"` behind, which marks out exactly the places that were
emphasised — handing the agent the answer.

---

## Self-check before delivery

1. Does every degradation in the proposal have at least one step claiming it
   (`deg`)? Is every step's `deg` an id that really exists in the proposal? Both
   directions are hard criteria in `check_recipe`; the only difference is
   whether you check them yourself first or get sent back.
2. Did `trial` print `gate=ok`?
3. **Have you looked at the renders?** Does the broken state match what
   `what_breaks` describes?
4. Are the anchors that should have survived still there? (sibling elements,
   reference slides, surrounding context)
5. Did you hit any `hard_target` by accident?
