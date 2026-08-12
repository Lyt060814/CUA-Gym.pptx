# Adversarial verification: "WPS changes 0.0% of shapes"

Method: build copies of real `work/` decks with deliberate, known damage, push them
through the real `wps_roundtrip.roundtrip_wps()` GUI round trip (serially, on the
private Xvfb display), and check that `roundtrip.compare()` reports the injected
damage and nothing else. 23 injections, 20 of them through a real WPS save.
Six WPS round trips were run; nothing under `pptxgym/`, `tests/`, `.claude/` or
`work/` was modified — all work is in `/tmp/wpsverif/`.

**Verdict: CONFIRMED.** WPS really does change 0.0% of the shape facts the
comparator models, and the 0.0% is not an artefact of the `APP_FILLED` change.
The comparator did not go blind to placeholder *geometry* — that specific worry
is refuted below by direct experiment. It did acquire one genuine new blind
spot, in placeholder *text*, affecting 40 shapes (2.0%) of the 10-deck corpus.

---

## 1. Injection results

Category is what `compare()` reported. "Spurious" counts anything reported that
was not injected. All rows marked *(WPS)* went through a real open-and-save.

### deck0010, one damaged copy, one WPS round trip *(WPS)*

| # | Injection | Detected? | Category | Spurious |
|---|---|---|---|---|
| INJ-1 | slide 2 `Oval 31` (no text) moved +0.50 in x | yes | `moved` `[0.5, 0.0]` | none |
| INJ-2a | slide 2 `Oval 25` (no text) moved +0.02 in x | yes | `moved` `[0.02, 0.0]` | none |
| INJ-2b | slide 2 title placeholder moved +0.02 in x | **no — by design** | — (0.02 < `TEXT_TOL` 0.05) | none |
| INJ-3 | slide 6 title (has text) widened +0.50 in | yes | `resized` **+ `moved [0.25,0]`** | see note A |
| INJ-3b | slide 4 `Rectangle 72` (no text) widened +0.50 in | yes, **wrong category** | `missing` + `added` | see note B |
| INJ-4 | slide 7 textbox (YouTube URL) deleted | yes | `missing` | none |
| INJ-5a | slide 5 title text rewritten (change at char 0) | yes, **wrong category** | `missing` + `added` | see note C |
| INJ-5b | slide 6 body run rewritten at char ~113 | yes | `text_changed` | none |
| INJ-6 | slide 4 solid fill `594182` → `FF0000` | **NO — MISSED** | — | see note D |

`compare(source, wps(damaged))` was byte-for-byte the same report as
`compare(source, damaged)`: **every injection survived WPS unaltered, in the same
category, with zero spurious entries.**
`compare(damaged, wps(damaged))` = **0/75 changed** — WPS noise alone is nil.

### deck0007, geometry of app-filled placeholders — the case under suspicion *(WPS)*

| # | Injection | Detected? | Category | Spurious |
|---|---|---|---|---|
| INJ-7a | slide 3 **`dt` (date) placeholder moved +0.50 in x** | **yes** | `moved [0.5, 0.0]` | none |
| INJ-7b | slide 3 **`sldNum` placeholder moved −0.50 in y** | **yes** | `moved [0.0, -0.5]` | none |
| INJ-7c | slide 5 **`ftr` placeholder moved +0.50 in x** | **yes** | `moved [0.5, 0.0]` | none |
| INJ-7d | slide 7 **`dt` placeholder widened +0.80 in** | **yes** | `resized` 3.0→3.8 in (+ `moved 0.4`, note A) | none |
| INJ-7e | slide 9 **`dt` placeholder deleted** | **yes** | `missing` | none |
| INJ-7f | slide 3 title moved +0.50 in x, full `a:off`+`a:ext` | yes | `moved [0.5, 0.0]` | none |
| INJ-7g | slide 11 title moved +0.50 in x via **`a:off` only** | **NO — MISSED** | — | see note E |

`compare(damaged, wps(damaged))` = **0/118 changed**.

**This is the direct refutation of the suspicion in the brief.** Only the *text*
of a `dt`/`sldnum`/`ftr` placeholder is exempt. Its position, size and existence
are compared exactly as for any other shape, and all five geometry injections on
field placeholders were caught through a real WPS save.

### deck0007, text of app-filled placeholders — where the change did blind it *(WPS)*

| # | Injection | Detected? | Category | Spurious |
|---|---|---|---|---|
| INJ-8 | slide 3 **`ftr` footer text** rewritten to `COMPLETELY DIFFERENT FOOTER TEXT…` | **NO — MISSED** | — | none |
| INJ-9 | slide 4 **`dt` placeholder**: `a:fld` replaced by author-typed literal `01.01.2000` | **NO — MISSED** | — | none |
| INJ-10 | slide 5 `ftr` placeholder deleted | yes | `missing` | none |
| INJ-11 | slide 3 body placeholder text rewritten (control) | yes | `missing` + `added` (note C) | none |

WPS preserved both missed edits verbatim — read back out of the saved file:
`'COMPLETELY DIFFERENT FOOTER TEXT - Forschungsdatenmanagement in Kooperation'`
and `'01.01.2000'`. The miss is the comparator's, not the renderer's.

### Offline-only (comparator properties, no WPS needed)

| # | Injection | Detected? | Category |
|---|---|---|---|
| INJ-12 | shape rotated 0° → 30° | **NO — MISSED** | `rot` is collected in `_facts` and never compared |
| INJ-13 | connector line width ×4 | **NO — MISSED** | `line_w` is collected and never compared |
| INJ-14 | inherited (no explicit) fill → solid green | yes | `fill_changed` `None → solid` |

INJ-14 vs INJ-6 pins the rule down: `fill` in `_facts` is only the fill **type**
(`(style["fill"] or {}).get("type")`). A *type transition* is caught; a
**colour change within the same type is invisible**.

---

## 2. Notes on the miscategorised and missed rows

- **A — a pure resize also fires `moved`.** `_facts` stores the shape *centre*
  (`cx`,`cy`). Growing width by 0.5 in with `left` fixed shifts the centre 0.25 in,
  so one resize is counted twice (once as `resized`, once as `moved`) and inflates
  the drift statistics. Pre-existing; unrelated to the change under test.
- **B — a text-less shape's key contains its size.** `stable_key_of` falls back to
  `geo:<kind>:<w/0.1in>x<h/0.1in>`. Any resize ≥ ~0.05 in of a shape with no text
  and no image changes the key, so it is reported as `missing` + `added`, never as
  `resized`. The bucket fix in the commit history only removed size from the
  *dict payload*, not from `stable_key`.
- **C — a text change in the first 60 characters is reported as delete+add.**
  The key is `txt:<first 60 chars>`; changing them changes the key. `text_changed`
  can therefore only ever fire for edits past char 60 of a shape's text (INJ-5b).
  Both categories feed `structural`, so nothing is lost from the headline number —
  but the category label is wrong and the shape count is doubled.
- **D — fill colour is not compared at all** (see INJ-14 above).
- **E — a slide-level `<a:xfrm>` with `<a:off>` but no `<a:ext>` is discarded.**
  `census.py:645`: `if (xf is None or not xf["off"] or not xf["ext"]) and ph is not None:`
  falls back to the *inherited* geometry, throwing the explicit offset away. This is
  legal OOXML (off overrides, ext inherits) and is what `python-pptx`'s `.left`
  setter writes on a placeholder that had no xfrm. A placeholder moved that way is
  invisible to the census. Pre-existing; unrelated to the change under test.

---

## 3. Is the WPS save real, or a no-op touch?

Part list and part bytes, directory entries excluded:

| pair | parts A | parts B | common | identical bytes | **differing bytes** | new in B | gone from A | slide XML identical |
|---|---|---|---|---|---|---|---|---|
| deck0003 clean | 202 | 189 | 176 | 50 | **126** | 13 | 26 | **0 / 20** |
| deck0006 clean | 123 | 134 | 123 | 46 | **77** | 11 | 0 | **0 / 15** |
| deck0010 clean | 116 | 122 | 115 | 40 | **75** | 7 | 1 | **0 / 24** |
| deck0010 damaged | 116 | 122 | 115 | 24 | **91** | 7 | 1 | **0 / 24** |
| deck0007 damaged | 168 | 169 | 164 | 14 | **150** | 5 | 4 | **0 / 22** |
| deck0007 text | 168 | 169 | 164 | 14 | **150** | 5 | 4 | **0 / 22** |

**Not a touch.** Every `ppt/slides/slideN.xml` in every pair differs byte-wise —
121 slide parts across six decks, zero identical. WPS also rewrites the package
structure: it drops all 26 `customXml/*` parts from deck0003, adds
`docProps/custom.xml` and a `notesMaster`/`handoutMaster`, re-encodes media
(`image1.tiff` → `image1.jpeg`, `image3.png` → `image3.jpeg`,
`image2.gif` → `image2.GIF`), and re-serialises every `ppt/diagrams/*` and
`ppt/embeddings/oleObject*.bin`. File size moves by up to −0.6 MB (deck0003).

So WPS parses and re-serialises the whole document and still lands on identical
shape geometry — that is a strong result about WPS, not an absent write.

---

## 4. Is 0.0% an artefact of the `APP_FILLED` change?

Two independent arguments say no.

**(a) On 6 of the 10 decks the change is provably a no-op.** Count of shapes whose
placeholder role is in `APP_FILLED`, per deck:

| deck | shapes | app-filled | of which `a:fld` | of which literal text | LibreOffice |
|---|---|---|---|---|---|
| deck0001 | 92 | **0** | 0 | 0 | 7.6% |
| deck0002 | 257 | 34 | 17 | **17** (`dt`) | 46.3% |
| deck0003 | 140 | **0** | 0 | 0 | 60.7% |
| deck0004 | 130 | 20 | 20 | 0 | 28.5% |
| deck0005 | 210 | **0** | 0 | 0 | 38.1% |
| deck0006 | 743 | **0** | 0 | 0 | 42.4% |
| deck0007 | 119 | 66 | 43 | **23** (22 `ftr`, 1 `sldnum`) | 8.4% |
| deck0008 | 93 | **0** | 0 | 0 | 11.8% |
| deck0009 | 182 | 1 | 1 | 0 | 61.5% |
| deck0010 | 76 | **0** | 0 | 0 | 7.9% |
| **total** | **2042** | **121** | **81** | **40** | |

Six decks — including deck0003 (60.7% under LibreOffice) and deck0006 (42.4%,
743 shapes) — contain **zero** `APP_FILLED` placeholders, so the change cannot
touch their result, and they read 0.0% too. The 81 `a:fld` shapes match the
"81 date and slide-number placeholders across four decks" in the commit message.

**(b) Running the old comparator on the same before/after pairs isolates exactly
what the change suppressed.** I reimplemented the pre-`55d9aad` `_facts`
(field detection via `a:fld` only) and ran both over the same files:

| pair | new comparator | old comparator |
|---|---|---|
| deck0003 clean | 0/140 (0.0%) | **0/140 (0.0%)** |
| deck0006 clean | 0/743 (0.0%) | **0/743 (0.0%)** |
| deck0010 clean | 0/76 (0.0%) | **0/76 (0.0%)** |
| deck0010 damaged | 0/75 (0.0%) | **0/75 (0.0%)** |
| deck0007 damaged | 0/118 (0.0%) | 42/118 (35.6%) — `missing: 42, added: 42` |

The old comparator's 35.6% is **entirely** the 42 `dt`/`sldNum` placeholders on
deck0007, and the cause is visible in the XML: WPS keeps the `<a:fld>` element and
its `type`, but **strips the cached `<a:t>` literal**.

```xml
before: <a:fld id="{9093…}" type="slidenum"><a:rPr lang="de-DE"/><a:pPr/><a:t>2</a:t></a:fld>
after : <a:fld id="{9093…}" type="slidenum"><a:rPr lang="de-DE"/></a:fld>
```

With no runs left, the old `any(run["field"])` test returned False, the key fell
from `fld:sldnum` to the geometry bucket, and one untouched shape became one
`missing` plus one `added`. Nothing moved, resized, changed fill or vanished.
The change is correct and the 42 were false positives.

**(c) A deeper diff finds nothing the comparator is hiding.** I also ran a diff
over the *entire* census record (kind, cx/cy/w/h, rot, flip, text, image_sha,
style, text_style, table, diagram, crop, link, placeholder, semantic), matched by
document order, with only field-placeholder text exempt:

- deck0003 clean, 140 shapes: **no differences at all, in any field.**
- deck0010 clean/damaged, 76/75 shapes: **no differences at all.**
- deck0007 damaged, 118 shapes: 42 `text_style` differences, all of them the
  field placeholders whose `<a:t>` WPS dropped (`text_style` → `None`).
- deck0006 clean, 743 shapes: 35 `text_style` differences, all cosmetic —
  24 East-Asian font-name normalisations (`맑은 고딕` → `Malgun Gothic`, the
  same font under its English name), 20 font sizes rounded by 0.01 pt
  (13.06 → 13.05), 1 paragraph space-before by 0.02 pt.

---

## 5. What the comparator cannot see

Introduced by the change under test:

1. **The text of a `ftr` / `hdr` placeholder**, which is author-written content,
   not application-generated. 22 shapes in deck0007.
2. **The text of a `dt` / `sldnum` placeholder that holds a literal run rather
   than an `a:fld`.** 17 shapes in deck0002 — where the author typed
   `"Peter Charlton"` into the date placeholder. 1 more in deck0007.

Total exposure: **40 shapes, 2.0% of the 2042-shape corpus**, carrying author text
that is now never compared. The fix is one line of extra evidence: exempt a
placeholder's text only when the paragraph actually contains an `<a:fld>` **in the
before file** (`is_field = ph_type in APP_FILLED and before-side had a field`, or
simply take `is_field` from the `a` side for both), rather than from its role alone.
Keying on the role is right; skipping the text comparison on the role alone is not.

Pre-existing, not caused by the change, but relevant to reading "0.0%":

3. Rotation (`rot`) — collected, never compared.
4. Line width (`line_w`) — collected, never compared.
5. Fill **colour** — only the fill *type* is compared.
6. Font, font size, run colour, bold/italic, paragraph spacing — `text_style` is
   never compared at all (this is what hides WPS's 0.01 pt size rounding).
7. A resize of a text-less, image-less shape ≥ ~0.05 in — reported as
   `missing` + `added`, not `resized` (`stable_key` carries the size bucket).
8. A text edit within the first 60 characters — reported as `missing` + `added`,
   not `text_changed`.
9. A move written as `<a:off>` with no `<a:ext>` on a placeholder — discarded
   entirely by `census.py:645`.

"0.0% of shapes changed" should therefore be read as: *WPS changed none of
{existence, kind, centre, size, effect list, dash, fill type, preset geometry,
non-field text} on 2042 shapes across 10 decks.* Within that scope the result
survived every positive control, with zero spurious reports across six WPS pairs.
