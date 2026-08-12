# Operator Reference

Not API documentation. `--help` on each command is always in sync with the
code; this file only covers **the order to call things in, what a failure
means, and which traps not to walk into**.

---

## What a deck directory looks like

```
work/deck0001/
  meta.json          provenance, slide count, slide size
  source.pptx        the original deck — also the ground truth, no stage may write it
  digest.json        structural digest (for humans, indented)
  digest.min.json    same content, compact, for the agent to read (about half the tokens)
  renders/p-NN.png   one per slide
  proposal.json      what task this deck should yield         ← agent
  recipe.json        exactly how to break it                  ← agent
  input.pptx         the file after breaking
  delta.json         every change, together with the value it had before
  state.json         the state of each stage
```

**Stages hand off only through files.** Nothing lives in a conversation, so any
step can be re-run on its own, and a human can take one step over and hand it
back to the pipeline.

---

## Stages

```
ingested → inspected → proposed → recipe → degraded → materialised
           script       agent      agent    script      script
         → reconciled → solvable → scored → hardened → packaged
             agent       agent      script    script      script
```

```bash
pptxgym ingest corpus/            # register source decks
pptxgym inspect                   # digest + renders
pptxgym propose --deck deck0001   # start a headless agent
pptxgym recipe  --deck deck0001   # start a headless agent
pptxgym degrade --deck deck0001   # run the recipe + integrity gate
pptxgym run --mode fast --count 10 # managed end-to-end batch
python3 -m pptxgym.foreman         # low-level orchestrator entry point
pptxgym status                    # stage table (>24 decks switches to a summary; --all for the full table)
```

`--workers` = how many **agent** stages at once (this eats API); `--cpu-workers`
= how many **render** stages at once (this eats soffice, default cores/4). The
two pools are separate; a slot is claimed per stage and returned immediately.
The end of `status` reports who is running now, who carries a gate's `no`, who
is parked, and how much disk `work/` is using.

Use `pptxgym run`, `resume`, `run-status`, `logs`, `publish`, and `verify` for
normal operation. Individual stage commands and `python -m` entries remain for
diagnosis and targeted recovery; they are not a second setup path.

---

## Inspection tools

You will need these while writing a recipe:

```bash
python -m pptxgym.tools shapes   work/deck0001 7 12 19   # shape table, this is where paths come from
python -m pptxgym.tools smartart work/deck0001 --slide 19
python -m pptxgym.tools chart    work/deck0001 --slide 5
python -m pptxgym.tools pair     work/deck0001 7 12      # original vs broken, slide by slide
```

Each `shapes` row is: `path / kind / normalised position / size in inches /
z-order / font / image hash / flags / text`. A leading `·` means decorative
(small icons, connectors), `>>` a composite object, `~~` connector topology,
`##` a repeated group. **The `path` is the address used in the recipe.**

---

## Three traps you have to know about

**1. You cannot pick the right path from the JSON alone.**
What `path 3` is can only be recognised against the render. Picking wrong
raises no error; it just deletes something else. **Look at the render first.**

**2. Paths are positional indices, and a deletion shifts everything after it.**
The executor indexes once **before any of a slide's steps begin**, so every
path in the recipe is written against **the numbering in the original digest**.
Do not try to work out what the numbering becomes after a deletion.

**3. Deleting a shape must delete its relationships too, or the answer is still
in the package.**
The executor already does this, but if you are hand-writing a tool you need to
know: pulling the element out of the spTree leaves the picture's bitmap and
SmartArt's `data*.xml` (which holds the text of every node) alive, and `unzip`
reads them. `degrade`'s gate catches this and reports `ANSWER LEAK` or
`DEAD RELS`.

---

## Fonts: when the render stops being evidence

Trap 1 above says "look at the render first". **This section is about when the
render is lying to you.**

When the renderer meets a codepoint no font on the machine covers, it draws
`.notdef` — a hollow box. It raises no error and writes no log, and nothing
further down the chain can read a glyph: **the proposal is written against
boxes, the reference image is boxes, the solvability probe judges "can this be
done" against boxes.** Every gate passes and the output is a batch of
confidently empty tasks. The corpus `Forceless/Zenodo10K` is ten thousand
international conference submissions — CJK, Arabic, Cyrillic and Thai are all
in there, so this is not a rare event.

```bash
python -m pptxgym.fonts work/deck0001/source.pptx      # one-line verdict
python -m pptxgym.fonts work/deck00*/source.pptx --json
```

| verdict | meaning |
|---|---|
| `ok` | every character has a glyph |
| `incidental` | under 1% missing, and no slide collapsed (two Greek letters in a formula land here) |
| `degraded` / `unrenderable` | whole slides are boxes — see `unusable_slides`, **the renders of those slides are not evidence** |
| `unknown` | no fc-list and no fontTools, and **what cannot be asked cannot pass** (same principle as `undetermined`) |

**`fc-list :lang=zh` returning something ≠ Chinese renders.** Coverage is
counted per codepoint, not per language. On the machine this was written on,
`:lang=zh` and `:lang=ja` each matched one font, and it really could draw them
(DroidSansFallbackFull carries Han and kana); the same font's Hangul has only
the letters, not the syllables, so Korean is all boxes — and `:lang=ko` matched
nothing at all. The language probe got two of three answers right, purely by
luck. Measured as missing on this machine: **Hangul syllables, Thai,
Devanagari, emoji outside the BMP**; deck0001 also contains two U+F0E0, the
Wingdings envelope icon, which cannot exist on Linux (that WPS startup warning
about `missing fonts Symbol` is about exactly this).

### Fonts the image has to install

Inside the Docker image running on HF Jobs, these are the **Debian/Ubuntu
package names**, and not one of them can be dropped:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
      fontconfig \
      fonts-noto-core fonts-noto-extra fonts-noto-ui-core \
      fonts-noto-cjk fonts-noto-cjk-extra \
      fonts-noto-color-emoji fonts-noto-mono \
      fonts-dejavu-core fonts-dejavu-extra \
      fonts-liberation fonts-liberation-sans-narrow \
      fonts-crosextra-carlito fonts-crosextra-caladea \
 && fc-cache -f && rm -rf /var/lib/apt/lists/*
```

- `fonts-noto-core` / `-extra` cover Greek, Cyrillic, Arabic, Hebrew, Thai,
  Devanagari and most of the remaining scripts; **CJK is not in them** and has
  to be installed separately as `fonts-noto-cjk` (`-extra` is the full set of
  weights, and SmartArt and titles routinely use unusual weights, so install it
  too).
- `fonts-liberation` (Arial / Times / Courier),
  `fonts-liberation-sans-narrow` (Arial Narrow), `carlito` (Calibri) and
  `caladea` (Cambria) do not install "more scripts", they install
  **metric-compatible** substitutes. The font names written in decks are
  basically all of these, and if the substitute's metrics do not match, text
  reflows — which is exactly the 0.6 inches measured in
  [reward design section 2.4](../design/reward.md) (see
  that section).
- `fc-cache -f` cannot be skipped; without refreshing the cache after
  installation fontconfig does not see them.
- PUA icon fonts like Wingdings / Symbol have no free substitute and nothing
  you install will fill them in, so such decks will keep being flagged — that
  is the correct behaviour, do not turn it off.

**Both ends need them, and they need the same set:** LibreOffice uses them **at
render time** (the images `inspect` produces are the whole of the proposal's
evidence), WPS uses them **at measurement time** (`wps_roundtrip`'s 0.0% is
computed against the current fonts, and a different set gives a different
number). If the image and the evaluation VM do not have the same font set, the
numbers measured on one do not transfer to the other.

---

## What the gate said

`pptxgym degrade` only counts as passing when it prints `gate=ok`. What it
rejects:

| report | meaning |
|---|---|
| `r:id ... -> missing part` | dangling reference; the file behaves differently in different software |
| `no content type for part` | same |
| `shape id N used 2x` | a duplicate shape id on one slide |
| `ANSWER LEAK` | the data of a deleted thing is still in the package |
| `DEAD RELS` | a slide still points at a part it does not draw |

**Run the gate on the original deck before drawing conclusions.** Some
"problems" come with the source file — an OLE `mc:Fallback` image id that is
always 0, SmartArt's `diagramDrawing` not being referenced through `r:id` —
and both of those are already excluded in the gate, but a new false positive
should be verified against the ground truth first.

---

## hard_target

Shapes carrying `hard_target` in the digest **cannot be produced in the GUI**:

- `ole` — an embedded Excel / Origin / Prism / equation object
- `custom_geometry` — hand-drawn Bézier paths (especially when `redrawable` is
  false)

They are **context, not targets**. Delete one and the task has no solution. The
proposal usually says so outright; the recipe stage must not overstep.

---

## Composite objects need partial edits, not wholesale deletion

| object | entry point for a partial edit |
|---|---|
| SmartArt | top-level `smartart` in the recipe: `{"slide":19,"drop_text":["Ingest"]}` |
| chart | top-level `chart` in the recipe: `{"slide":5,"drop_name":["Data Size"]}` or `strip` |
| table | `table_drop_rows` / `table_drop_cols` (delete) vs `clear_table_cells` (empty) |
| text | `text_runs` (some paragraphs) vs `set_font` (the whole shape) |
| animation | `anim_drop_steps` (some build steps) vs `strip_animation` (whole slide) |

Deleting the whole thing destroys **the surviving elements, which were the
anchor**, and turns "fill it back in from the pattern" into "rebuild it from
nothing" — the difficulty and the point of the task both change. If the
proposal asks for a partial edit, use the partial entry point.

---

## Reporting discipline

Whatever you could not do, **write it into `_why` honestly**, and say what it
cost.

> "deleted an entire SmartArt that should have been edited in place — the
> surviving column, which was the style anchor, is gone, the difficulty goes
> from filling in to rebuilding, and the instruction needs rewriting to match"

**Under-reporting is far worse than not being able to do it.** Not being able
to do it is a known tooling gap and can be filled; under-reporting puts a
mislabelled sample into the data, and nobody downstream can detect it.
