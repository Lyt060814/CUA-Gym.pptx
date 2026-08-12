# Round-trip re-measurement against the tightened comparator

Re-ran both renderers over all ten decks in `work/deck00NN/source.pptx` after the
three changes to `pptxgym/roundtrip.py`:

1. `rotated` and `line_changed` are now actually compared, not merely collected.
2. The text exemption for date / slide-number / footer / header placeholders is
   now conditional on `has_fld` (the original really held an `a:fld`), not on the
   placeholder's role alone. Role still drives *matching*; only *comparison* was
   narrowed.
3. `line_changed` fires only when both sides carry a line width, so soffice's
   habit of writing `0` for "nothing said" and resolving inherited defaults into
   explicit values does not read as a border appearing.

Results written to `work/deck00NN/roundtrip.json` (LibreOffice) and
`work/deck00NN/roundtrip-wps.json` (WPS), in the shape `check()` returns.
LibreOffice was run five at a time (each `soffice` gets its own
`UserInstallation` profile); WPS was run strictly one at a time on Xvfb `:99`,
~40 s a deck.

---

## Job 1 — the two tables

### LibreOffice (`pptxgym.roundtrip.check`)

| deck | shapes | `changed_frac` | verdict | `counts` |
|---|---|---|---|---|
| deck0001 | 92 | 7.61 % | fragile | `{missing: 6, added: 6, text_changed: 1}` |
| deck0002 | 257 | 46.30 % | fragile | `{missing: 10, added: 10, moved: 33, resized: 48, fill_changed: 8, geom_changed: 18, text_changed: 2}` |
| deck0003 | 140 | 60.71 % | fragile | `{kind_changed: 1, moved: 16, resized: 57, fill_changed: 7, geom_changed: 4}` |
| deck0004 | 130 | 28.46 % | fragile | `{missing: 12, added: 12, kind_changed: 1, moved: 4, resized: 8, fill_changed: 6, geom_changed: 5, text_changed: 1}` |
| deck0005 | 210 | 38.10 % | fragile | `{missing: 9, added: 9, moved: 9, resized: 39, fill_changed: 10, geom_changed: 12, text_changed: 1}` |
| deck0006 | 743 | 42.40 % | fragile | `{missing: 11, added: 11, geom_changed: 301, text_changed: 3}` |
| deck0007 | 119 | 8.40 % | fragile | `{missing: 8, added: 8, text_changed: 2}` |
| deck0008 | 93 | 11.83 % | fragile | `{missing: 7, added: 7, moved: 1, resized: 1, geom_changed: 1, text_changed: 1}` |
| deck0009 | 182 | 61.54 % | fragile | `{missing: 15, added: 15, moved: 19, resized: 63, fill_changed: 5, geom_changed: 3, text_changed: 7}` |
| deck0010 | 76 | 7.89 % | fragile | `{missing: 2, added: 2, resized: 1, fill_changed: 1, geom_changed: 2}` |

Median 33.3 %, range 7.61 % – 61.54 %. Every deck is `fragile`, because
`structural` counts `missing + kind_changed + text_changed` and every deck has at
least one of those. Position drift where it exists: median 0.13 – 0.33 in,
worst `max_in` 0.845 in (deck0005).

**`rotated` and `line_changed` are empty on all ten decks** — and that is a
measurement, not an omission. The populations are there to be measured: across
the corpus 17 shapes carry a non-zero rotation (decks 4, 5, 9, 10) and 452 carry
an explicit line width (every deck but 0007). LibreOffice preserved all of them.
The `line_changed` guard is doing work: on deck0005 alone, all 47 line widths
come back blanked or defaulted on the soffice side, and the old unguarded
comparison would have reported 47 phantom border changes.

### WPS (`pptxgym.wps_roundtrip.check`)

| deck | shapes | `changed_frac` | verdict | `counts` |
|---|---|---|---|---|
| deck0001 | 92 | 0.00 % | stable | `{}` |
| deck0002 | 257 | 0.00 % | stable | `{}` |
| deck0003 | 140 | 0.00 % | stable | `{}` |
| deck0004 | 130 | 0.00 % | stable | `{}` |
| deck0005 | 210 | 0.00 % | stable | `{}` |
| deck0006 | 743 | 0.00 % | stable | `{}` |
| deck0007 | 119 | 0.00 % | stable | `{}` |
| deck0008 | 93 | 0.00 % | stable | `{}` |
| deck0009 | 182 | 0.00 % | stable | `{}` |
| deck0010 | 76 | 0.00 % | stable | `{}` |

**The claim holds.** WPS still changes 0.0 % of shapes on all ten decks, 2 042
shapes in total, with every category empty. No deck reports non-zero, so there is
no `detail` to open.

### Is the stricter instrument actually stricter?

The LibreOffice numbers came back **byte-identical to the pre-change files** —
same `changed_frac`, same `counts`, deck for deck. That is the sort of result
that usually means the new code never ran, so it was checked rather than
assumed:

- The tightened module is what was imported (`pptxgym/roundtrip.py` from the
  working tree, `has_fld` present in its source), and all twenty JSON files were
  rewritten.
- Change 2 has a real population. 121 shapes across the corpus sit in an
  `APP_FILLED` role, and only 81 of them hold an actual `a:fld`. So **40
  placeholders that used to be exempt from text comparison are now compared** —
  17 on deck0002, 23 on deck0007, and they are exactly the author-typed kind the
  change was aimed at (deck0007's are footers reading
  "FoDaKo - Forschungsdatenmanagement in Kooperation"). Neither renderer touched
  any of them, which is why the totals did not move.
- Positive control on the comparator, by doctoring one fact and re-running
  `compare` (harness in `/tmp/poscontrol2.py`, nothing in the repo touched):
  - rotate one shape → `{'rotated': 1}`, `from: 36.15, to: 66.15`
  - triple one line width → `{'line_changed': 1}`, `from: 9525, to: 28575`
  - blank all 47 line widths, soffice-style → `{}`, correctly silent
  - rewrite the 23 author-typed footers on deck0007 → `{'text_changed': 23}`

So all three detectors fire when given something to find. The corpus simply had
nothing for them, under either renderer.

The honest caveat on WPS: 0.0 % means *nothing this comparator looks at* changed.
It compares shape kind, position, size, rotation, effects, fill, dash, line width
(when both sides have one), preset geometry and flattened text. Character-level
formatting, theme, slide-master and animation are outside its field of view, and
inherited line defaults are explicitly left unresolved.

---

## Job 2 — the deck0001 `text_changed` on slide 5

`detail.text_changed` reports one entry whose `was` and `now` are indeed
identical for the first 50 characters. The full strings:

```
was: '4 factors needed to form a mineral deposit of economic value: Source must provide metals; Minerals must dissolve influid ; Fluids must transport metals as a solution; Mineral must precipitate and concentrate to form a deposit,'
now: '4 factors needed to form a mineral deposit of economic value: Source must provide metals; Minerals must dissolve influid; Fluids must transport metals as a solution; Mineral must precipitate and concentrate to form a deposit,'
```

226 vs 225 characters. Codepoint diff, after `_norm_text`:

```
delete: was[120:121] = ' '   →   now[120:120] = ''
        U+0020  Zs  SPACE
   context was: 'Minerals must dissolve influid ; Fluids must transport metals'
   context now: 'Minerals must dissolve influid; Fluids must transport metals'
```

One ordinary ASCII space, before a semicolon. Not a soft hyphen, not a
zero-width space, not an NBSP, not a ligature — `NFC`, `NFKC` and `NFKD` all
leave the two strings unequal, so no Unicode normalisation form touches it.

### It is an artefact, and the source is `census.element_text`

```python
def element_text(el):
    parts = [t.text for t in el.iter(q("a:t")) if t.text]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()
```

Runs are joined with a **space**. OOXML run boundaries carry no implicit
whitespace — whitespace is only ever the literal content of an `a:t` — so every
run boundary that falls mid-token has a space injected that is not in the deck.

Slide 5, shape id 5 "Rectangle 3", paragraph 2. Before:

```xml
<a:r><a:rPr lang="fr-FR" sz="2000" b="0" kern="0" dirty="0" err="1" .../><a:t>Minerals</a:t></a:r>
<a:r><a:rPr lang="fr-FR" sz="2000" b="0" kern="0" dirty="0"         .../><a:t> must dissolve </a:t></a:r>
<a:r><a:rPr lang="fr-FR" sz="2000" b="0" kern="0" dirty="0" err="1" .../><a:t>influid</a:t></a:r>
<a:r><a:rPr lang="fr-FR" sz="2000" b="0" kern="0" dirty="0"         .../><a:t>;</a:t></a:r>
```

After the soffice round trip, one run:

```xml
<a:r><a:rPr b="0" lang="fr-FR" sz="2000" spc="-1" strike="noStrike" .../><a:t>Minerals must dissolve influid;</a:t></a:r>
```

The four runs exist only because PowerPoint's spell-checker tagged `Minerals`
and `influid` with `err="1"`. soffice drops `err`/`smtClean`, the four runs
become formatting-identical, and it merges them. The rendered text is
`Minerals must dissolve influid;` on both sides, character for character. Only
`element_text`'s join changed, because the `influid` | `;` boundary was
mid-token and the `metals as a ` | `solution;` boundary in the next paragraph
was not.

(The missing space in `influid` is a typo in the original deck, present in both
sides. It is not the diff.)

**The same artefact explains the 6 `missing` + 6 `added` on deck0001**, which
until now looked like shapes vanishing. The two sides have six unmatched keys
each, and they pair off exactly:

```
A (0,  'txt:Olivier POURRET @ olivier_pourret https://orcid.org/0000-000')
B (0,  'txt:Olivier POURRET @olivier_pourret https://orcid.org/0000-0001')
A (0,  'txt:UniLaSalle , Beauvais, France 24th October 2019')
B (0,  'txt:UniLaSalle, Beauvais, France 24th October 2019')
A (6,  'txt:Haxel et al . (2002 )')
B (6,  'txt:Haxel et al. (2002)')
A (6,  'txt:Why secondary deposits ?')
B (6,  'txt:Why secondary deposits?')
A (7,  'txt:Sensitivity : - to pH, - to redox condition, - to sorption r')
B (7,  'txt:Sensitivity: - to pH, - to redox condition, - to sorption re')
A (18, 'txt:Imming & Tennant . ( 2018) Sticker open science: just scienc')
B (18, 'txt:Imming & Tennant. (2018) Sticker open science: just science ')
```

Every one is a spurious space before punctuation. Nothing is missing and nothing
was added; the `txt:` match key changed under the same run-merge, so the shape
failed to match itself. Since `missing` and `added` are equal on eight of the ten
decks, this is likely a large share of the LibreOffice `changed_frac` corpus-wide
— which would mean the numbers above are an over-estimate of what soffice really
does. That is a separate investigation and nothing here was changed on the
strength of it.

### The fix (proposed, not applied)

In `pptxgym/census.py::element_text`, join runs with nothing:

```python
return re.sub(r"[^\S\n]+", " ", "".join(parts)).strip()
```

Run boundaries stop inventing whitespace; real whitespace, which lives inside
`a:t`, is untouched.

### What that normalisation would then hide

Joining with `""` is also what currently keeps text from *different paragraphs*
and across `a:br` from fusing — `element_text` walks every `a:t` under the shape,
paragraphs included, and the `" "` join is the only thing separating the last
word of one bullet from the first word of the next. Remove it and:

- **A deleted paragraph or line break becomes invisible.** Two bullets
  `metals;` / `Fluids must…` flatten to `metals;Fluids must…`. Merge them into
  one paragraph and the flattened text is byte-identical. Today that merge shows
  up as a `text_changed` (`'metals; Fluids'` → `'metals;Fluids'`); afterwards it
  would not. The mirror case — one bullet split into two — goes the same way.
  This is a *real* edit an agent can make and a reward would need to see, and it
  is precisely the granularity the bullet-level tasks operate at.
- **The `txt:` match key stops distinguishing them**, so a shape whose paragraph
  structure changed would still match itself — which is the desired effect for
  the six false `missing`/`added` above, and the undesired one for a genuine
  re-flow into a different number of bullets.

The version that fixes the artefact without paying that price is structure-aware
rather than a different join character: concatenate `a:t` **within** a paragraph
with `""`, and join paragraphs and `a:br` with `"\n"`, then collapse horizontal
whitespace only (as written above). That in turn requires `roundtrip._norm_text`
to stop using bare `str.split()`, which folds `\n` away again — it would have to
strip per line and rejoin on `\n`. And it has a cost of its own: paragraph
structure would then be compared, and any renderer that turns an `a:br` into a
paragraph (or the reverse) on import would start reporting `text_changed` on
shapes whose visible text is unchanged. Worth doing, worth measuring first, and
out of scope here.

Note that `element_text` also feeds `text_digest`, `stable_key` and therefore
every `digest.json` in `work/`; changing it invalidates decks that have already
passed the gates. Nothing was applied.
