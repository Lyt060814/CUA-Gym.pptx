# Corpus assessment: `Forceless/Zenodo10K`

Investigated 2026-08-04. Source: <https://huggingface.co/datasets/Forceless/Zenodo10K>.
All numbers below were measured directly against the HF hub API, the datasets-server API,
the metadata parquet (2 MB), and HTTP Range probes of 120 real `.pptx` files. No bulk
download was performed; exactly one full `.pptx` (1.0 MB) was fetched to `/tmp`.

**Headline:** the dataset is a *metadata index* + a *licence-partitioned LFS file tree*.
Per-record licences exist and are trustworthy, so per-item filtering **is** possible from
metadata alone. The corpus is 242 GB of original, byte-identical Zenodo uploads.

---

## 1. What is a row?

**A row is one presentation file (one `.pptx`), not a slide.** 10,448 rows, 9 columns,
all `string` except `size` (`int64`). There are **no binary columns** — the parquet holds
*only metadata*. The actual `.pptx` bytes live in the repo tree as Git-LFS files.

Evidence — `https://datasets-server.huggingface.co/info?dataset=Forceless/Zenodo10K`:

```json
"features":{"filename":{"dtype":"string"},"size":{"dtype":"int64"},"url":{"dtype":"string"},
 "license":{"dtype":"string"},"title":{"dtype":"string"},"created":{"dtype":"string"},
 "updated":{"dtype":"string"},"doi":{"dtype":"string"},"checksum":{"dtype":"string"}},
"builder_name":"parquet",
"splits":{"pptx":{"name":"pptx","num_bytes":3865833,"num_examples":10448}},
"download_size":2028492
```

One config (`default`), one split (`pptx`). `download_size` = **2,028,492 bytes** — that is
the *metadata parquet only* (`data/pptx-00000-of-00001.parquet`), which is why the number
looks implausibly small for "10K presentations".

Sample row (`/first-rows`):

```json
{"filename":"UF DSI Symposium Talk.pptx","size":1078460,
 "url":"https://zenodo.org/api/records/1207215/files/UF DSI Symposium Talk.pptx/content",
 "license":"cc-by-4.0",
 "title":"Data Science and Informatics: Creating Student Led Data Science Curricula",
 "created":"2018-03-25T17:21:14.668816+00:00","updated":"2020-01-20T14:42:22.076245+00:00",
 "doi":"10.5281/zenodo.1207215","checksum":"md5:49e3b6a509f6689aee174b2b780fa3f3"}
```

No nulls in any column (verified across all 10,448 rows).

### Storage layout of the actual files

Repo root has 4 entries: `.gitattributes`, `README.md`, `data/` (the parquet),
and `pptx/` — which contains **36 licence-named subdirectories**, then a year directory:

```
pptx/<license-id>/<created-year>/<md5-without-"md5:">-<original-filename>.pptx
```

Confirmed by `GET /api/datasets/Forceless/Zenodo10K/tree/main/pptx` → 36 directories:
`apache2.0, bsd-3-clause, cc-by-2.0, cc-by-3.0, cc-by-3.0-at, cc-by-3.0-us, cc-by-4.0,
cc-by, cc-by-nc-1.0, cc-by-nc-3.0, cc-by-nc-4.0, cc-by-nc-nd-1.0, cc-by-nc-nd-3.0-igo,
cc-by-nc-nd-4.0, cc-by-nc-sa-1.0, cc-by-nc-sa-2.0, cc-by-nc-sa-4.0, cc-by-nd-1.0,
cc-by-nd-2.5, cc-by-nd-4.0, cc-by-sa, cc-by-sa-2.0, cc-by-sa-3.0, cc-by-sa-4.0, cc-nc,
cc-pddc, cc-zero, etalab-2.0, mit-license, nlpl, notspecified, ogl-uk-3.0, other-at,
other-closed, other-open, other-pd`.

The README gives the exact path-construction rule (this is the whole card on the subject):

```python
dirname = f"zenodo-pptx/pptx/{task['license']}/{task['created'][:4]}/"
basename = f"{task['checksum'][4:]}-{task['filename']}"
filepath = dirname + basename
try:
    open('/tmp/'+basename,'wb').close()
except:
    filepath = dirname + basename[:240] + ".pptx"     # long-filename fallback
```

**So: parquet row → deterministic repo path.** I built 25 paths this way from random rows
and HEAD-probed them: **25/25 returned 302/307 (LFS redirect = present)**; a deliberately
bogus path returned **404**. Range probes then confirmed the byte counts match exactly:

```
meta_size=5748454   content-range: bytes 0-0/5748454
meta_size=1642070   content-range: bytes 0-0/1642070
meta_size=23060165  content-range: bytes 0-0/23060165
meta_size=6332492   content-range: bytes 0-0/6332492
```

*Caveat, marked:* `.gitattributes` lists only **8,798** explicit `pptx/...` LFS lines,
against 10,448 rows. I could not enumerate the full tree to reconcile this — the recursive
tree API rate-limited me repeatedly (see §4). But 25/25 random samples resolved, including
rows whose paths are not in `.gitattributes`, so the shortfall is almost certainly a
`.gitattributes` bookkeeping artefact, not missing files. **Not fully verified.**

---

## 2. Licensing — the blocking question

### Is there a per-record licence? **Yes.**

Three independent signals, all agreeing:

1. A `license` column on every row, populated for all 10,448 (0 nulls).
2. A `doi` column on every row (0 nulls) — a resolvable link back to the source record.
3. The directory layout itself partitions by licence.

### Is the licence field trustworthy? **Yes — it mirrors Zenodo verbatim.**

I cross-checked four records against the live Zenodo API:

| DOI | HF `license` | Zenodo `metadata.license.id` | match |
|---|---|---|---|
| `10.5281/zenodo.1207215` | `cc-by-4.0` | `cc-by-4.0` | ✔ |
| `10.5281/zenodo.1210187` | `cc-by-nc-4.0` | `cc-by-nc-4.0` | ✔ |
| `10.5281/zenodo.8376131` | `other-closed` | `other-closed` | ✔ |
| `10.5281/zenodo.8391135` | `notspecified` | `notspecified` | ✔ |

**Verdict: per-item filtering is possible from the metadata alone. No per-record Zenodo
fetch is required for licensing.** (A fetch is only needed for *language* — see §3.)

### Is there a dataset-level licence? **No.**

`datasets-server /info` returns `"license":""`. The hub API `tags` array contains no
`license:` tag at all (`["size_categories:10K<n<100K","format:parquet","modality:text",
"modality:document","library:datasets",...,"ppt","powerpoint","document","pdf"]`).
The README has no `license:` YAML key.

This is arguably the *correct* outcome: the compiler cannot relicense the underlying
documents, and each document's licence travels with it in the `license` column. There is
no compilation licence to rely on, and none is needed — you inherit each file's own terms.

### Full licence distribution (all 10,448 rows)

| Bucket | Rows | % | GB |
|---|---:|---:|---:|
| **PERMISSIVE** (BY / CC0 / PD / OSS) | 10,046 | 96.15% | 229.3 |
| **SHARE-ALIKE** (BY-SA — copyleft) | 226 | 2.16% | 8.6 |
| **NON-COMMERCIAL** (any `-nc-`) | 135 | 1.29% | 3.9 |
| **CLOSED** (`other-closed`) | 19 | 0.18% | 0.1 |
| **OTHER-OPEN** (`other-open`, `other-at` — unverified terms) | 10 | 0.10% | 0.2 |
| **NO-DERIVATIVES** (`-nd-`, non-NC) | 8 | 0.08% | 0.1 |
| **UNSPECIFIED** (`notspecified`) | 4 | 0.04% | 0.0 |

Raw counts: `cc-by-4.0` 9758, `cc-by-sa-4.0` 222, `cc-zero` 185, `cc-by-nc-4.0` 59,
`cc-by` 36, `cc-by-nc-nd-4.0` 35, `cc-by-nc-sa-4.0` 26, `other-closed` 19,
`cc-by-3.0-us` 16, `cc-by-2.0` 9, `other-open` 8, `cc-by-3.0` 8, `cc-pddc` 8,
`cc-by-nd-4.0` 6, `mit-license` 5, `other-pd` 5, `etalab-2.0` 5, `cc-by-3.0-at` 4,
`notspecified` 4, `cc-by-nc-sa-1.0` 3, `cc-by-nc-3.0` 3, `cc-nc` 3, `ogl-uk-3.0` 3,
`cc-by-nc-1.0` 2, `other-at` 2, `apache2.0` 2, `cc-by-sa-3.0` 2, `cc-by-nc-nd-3.0-igo` 2,
`cc-by-nd-1.0` 1, `cc-by-nc-nd-1.0` 1, `bsd-3-clause` 1, `cc-by-nd-2.5` 1, `cc-by-sa` 1,
`nlpl` 1, `cc-by-sa-2.0` 1, `cc-by-nc-sa-2.0` 1.

### The card's licence claim is overstated — verified false

README: *"comprising over 10,000 PowerPoint (.pptx) files, **all distributed under a clear
and compliant license**."*
PPTAgent paper (arXiv:2501.03936), ethics section: *"artifacts that were not permitted for
modification or commercial use under their respective licenses were filtered out."*

**That filter was not applied to the published artefact.** 166 rows (1.59%) are NC, ND,
`other-closed`, or `notspecified` — i.e. explicitly *not* permitted for modification and/or
commercial use. `notspecified` on Zenodo means no licence was declared → all rights reserved
by default; `other-closed` likewise. We must apply the filter ourselves; we cannot rely on
the card's blanket assurance.

---

## 3. Size and shape

### Total download size

| Quantity | Bytes | Note |
|---|---:|---|
| Metadata parquet (what `download_size` reports) | 2,028,492 | 1.9 MB — this is *not* the corpus |
| **Full `.pptx` corpus** | **242,123,486,039** | **242.1 GB** (sum of `size` over 10,448 rows) |
| Permissive subset only | 229.3 GB | 10,046 files |

The 242 GB figure is the sum of the metadata `size` column, and that column was verified
byte-exact against the hosted files by four independent `Content-Range` probes and one full
download (md5 matched). *Marked as not independently re-derived:* I could not enumerate the
repo tree to sum LFS sizes directly — the recursive tree endpoint rate-limited me (§4).

### Per-file size (all 10,448)

```
mean    23.17 MB     5%    0.22 MB
std     61.68 MB    25%    1.89 MB
min      0.00016 MB 50%    5.82 MB   <- median
max   1180.99 MB    75%   17.01 MB
                    95%   97.11 MB
```

Heavy right tail: 260 files >200 MB, 34 files >500 MB, one 1.18 GB. And a left tail:
255 files <100 KB, 8 files <20 KB (likely near-empty or stub decks).

### Slides per deck — **not in the metadata**; measured by Range probe

There is no slide-count column. I recovered it cheaply without downloading files: a `.pptx`
is a zip, so I HTTP-Range-fetched the tail of each file, parsed the zip central directory,
and counted `ppt/slides/slideN.xml` entries. **n = 120 random rows, 120/120 parsed cleanly,
0 errors, 0 decks with zero slides.**

```
mean    23.4      5%     1
median  19       10%     5
std     18.2     25%    11
min      1       75%    33
max    117       90%    46
                 95%    59
```

Histogram:

| slides | decks (of 120) |
|---|---:|
| 1–4 | 11 |
| 5–10 | 18 |
| 11–25 | 46 |
| 26–50 | 35 |
| 51–100 | 9 |
| >100 | 1 |

**Decks in our useful 5–25 band: 64/120 = 53.3%** (95% CI roughly 44–62%). Extrapolated to
the permissive subset: **≈ 4,600–6,500 usable decks**, point estimate ~5,350. That is a
comfortable margin over any plausible task target.

Media richness is good for the image-extraction plan: **median 27 `ppt/media/` parts per
deck, mean 36.7**. The one deck I fully downloaded had 15 slides, 5 media images, 103 zip
parts.

### Language distribution — **not in the HF metadata**

No language column. `title` text is the only in-metadata proxy, and it is unreliable.
However, **Zenodo itself carries a `metadata.language` field** — e.g. record 1207215
returns `language: eng`, record 8376131 `eng`, record 1210187 `None`. So language *is*
recoverable, but only by fetching each Zenodo record (10,448 API calls) and it is not
always populated. Marked: **undetermined from the dataset; recoverable at a cost.**

### Subject / domain — **not in the HF metadata**

No subject column. The PPTAgent paper's Table 1 groups its 50-deck experimental subset into
five domains (Culture, Education, Science, Society, Tech), but **that labelling is not
shipped in this dataset.** Zenodo records carry `keywords`/`subjects`, again fetch-only.

### Temporal spread (`created` year)

2018: 383 · 2019: 869 · 2020: 1,567 · 2021: 1,472 · 2022: 1,824 · 2023: 2,472 · 2024: 1,861.
Corpus ends in 2024; repo last modified 2025-10-31.

---

## 4. Access mechanics

### Auth / gating

**Not gated, not private, no access agreement.** Hub API: `"private":false, "gated":false,
"disabled":false`. Every probe in this investigation was made anonymously and succeeded.

### Rate limiting — real, and I hit it repeatedly

The HF **API** endpoints (`/api/.../tree`, `/whoami-v2`, `/api/.../revision`) rate-limited
this machine three separate times:

```
429 Too Many Requests: you have reached your 'api' rate limit.
Retry after 140 seconds (0/10000 requests remaining in current 300s window).
We had to rate limit your IP (54.179.137.28). ... make sure you pass a HF_TOKEN
```

That is a *shared egress IP* budget (10,000 req / 300 s), and this sandbox shares it.
**Any bulk enumeration of 10,448 file paths must send an `HF_TOKEN`.** A token file exists
at `~/.cache/huggingface/token` but I could not validate it (the validation call itself was
rate-limited). By contrast, the **`resolve/` CDN** path was never rate-limited — 25 HEAD
probes + 120 Range reads + 1 full download all went through anonymously and without
throttling. Downloading files is fine; enumerating metadata via the API is what needs a token.

### Streaming — **works, verified**

```
$ python -c "from datasets import load_dataset; ...
STREAMING OK, type IterableDataset
{'filename': 'UF DSI Symposium Talk.pptx', 'size': 1078460, ...}
```

```python
from datasets import load_dataset
ds = load_dataset("Forceless/Zenodo10K", split="pptx", streaming=True)
```

**But understand what it streams: the metadata index, not the decks.** Streaming buys
nothing here — the whole index is 1.9 MB, so just download the parquet and use pandas.
Triage plan: pull the parquet once (1.9 MB), filter on `license` + `size`, construct paths,
then fetch only the chosen decks. `datasets` is *not* installed system-wide
(`ModuleNotFoundError: No module named 'datasets'`); `huggingface_hub` 1.9.0 *is*. Neither
`pyarrow` nor `fastparquet` is installed system-wide, so reading the parquet needs a venv.

### Smallest command that pulls one sample `.pptx`

Plain `curl`, no auth, no Python (verified — produced a valid 1,078,460-byte
`Microsoft PowerPoint 2007+` file whose md5 matched the metadata):

```bash
curl -sL -o sample.pptx \
 "https://huggingface.co/datasets/Forceless/Zenodo10K/resolve/main/pptx/cc-by-4.0/2018/49e3b6a509f6689aee174b2b780fa3f3-UF%20DSI%20Symposium%20Talk.pptx"
```

Note the path must be percent-encoded — filenames contain spaces, commas, accents, and
non-Latin characters. Equivalent via `huggingface_hub` (handles encoding for you):

```python
from huggingface_hub import hf_hub_download
hf_hub_download("Forceless/Zenodo10K", repo_type="dataset",
                filename="pptx/cc-by-4.0/2018/49e3b6a509f6689aee174b2b780fa3f3-UF DSI Symposium Talk.pptx")
```

### The cheap-triage trick worth keeping

Slide count, media count, and part list are all obtainable **without downloading the deck**,
via two Range requests (zip EOCD → central directory). This scales: triaging all 10k decks
by shape costs ~1 GB of Range traffic instead of 242 GB. Working implementation lives at
`/tmp/zenodo10k_probe/probe_slides.py` (temp — reimplement if wanted).

---

## 5. Provenance and quality flags

**Files are original uploads, not conversions — proven.** The `checksum` column is Zenodo's
own md5 of the uploaded file. I downloaded one deck and hashed it:

```
49e3b6a509f6689aee174b2b780fa3f3  sample1.pptx     <- computed
md5:49e3b6a509f6689aee174b2b780fa3f3               <- metadata / Zenodo
```

Byte-identical. Every row therefore carries a **self-verifying integrity check**, and any
deck can be independently re-fetched from `url` (the Zenodo content endpoint) to confirm
the HF copy was not modified. This is unusually strong provenance.

**Deduplication: none at the file level.** 10,393 unique md5s across 10,448 rows →
**55 rows are exact byte duplicates** of another row (108 rows share 53 duplicated hashes).
Examples: `FLIPPED LEARNING- FT 2020 - Made Sujana.pptx` and
`FLIPPED LEARNING- FT 2020 - Made Sujana (1).pptx`, both under DOI 10.5281/zenodo.10421641.
Near-duplicates (same talk, revised) are certainly more numerous — 8,827 unique DOIs means
**1,621 rows share a Zenodo record with another row** (multiple files per record).
The dedup described in the paper (image cosine >0.85, slide-text cosine >0.8) is a
*downstream preprocessing step in PPTAgent*, **not applied to this artefact**.

**Corruption: no flag, and no evidence of a problem.** The card says nothing about corrupt
files. Empirically, 120/120 Range-probed files had a parseable zip central directory and
≥1 slide part, and the one full download opened cleanly. The soft signals to screen on are
size-based: 255 rows <100 KB and 8 rows <20 KB are suspicious; 34 rows >500 MB are
impractical regardless.

**Conversion: none.** Files are native `.pptx` as uploaded by researchers. The paper
explicitly frames this as the point — prior corpora are "mostly stored in PDF or JSON
formats, which leads to a loss of semantic information."

**Content character.** Real academic/institutional material: conference talks, posters,
lecture decks, project overviews, in many languages (filenames include Spanish, Indonesian,
Hungarian, German, French). Authentic — which is what we want for computer-use realism —
but also means real author names, institutional branding, and occasionally personal
material (one filename is literally `cpascoe_CV_PIMMS_.pptx`).

---

## What would have to be true for us to redistribute derived files from this corpus

We plan to publish, on HF and/or GitHub, a **damaged `.pptx`** (a derivative work of the
original deck) plus **images extracted from it** (verbatim copies of embedded works).
Both acts require redistribution rights *and* derivative rights.

| # | Condition | Status | Basis |
|---|---|---|---|
| 1 | Each source deck carries a licence permitting **redistribution** | **SATISFIED, per-item** | `license` column, 0 nulls, verified against Zenodo on 4/4 spot checks |
| 2 | Each source deck carries a licence permitting **modification** (derivatives) | **SATISFIED once filtered** | 10,046 permissive rows (96.15%); must exclude 8 ND + 35 BY-NC-ND + 2 BY-NC-ND-3.0-IGO + 1 BY-NC-ND-1.0 |
| 3 | Each source deck permits **commercial use** (RL training data for a commercial lab is safest treated as commercial) | **SATISFIED once filtered** | must exclude all 135 NC rows |
| 4 | No `other-closed` / `notspecified` decks in the shipped set | **SATISFIED once filtered** | 19 + 4 = 23 rows to drop; both mean all-rights-reserved by default |
| 5 | Filtering is mechanically possible **without** per-record fetches | **SATISFIED** | licence is a metadata column, byte-verified against Zenodo |
| 6 | **Attribution** shipped with every derived file (CC-BY requires author + title + licence + link, and a statement that the work was modified) | **UNSATISFIED — action required** | metadata has `title`, `doi`, `url`, `filename`, but **no author/creator field**. Author must be pulled from the Zenodo record (`metadata.creators`) — one API call per selected deck. This is the one place a per-record fetch is genuinely unavoidable. |
| 7 | A "this file has been modified" notice accompanies the damaged deck (CC-BY 4.0 §3(a)(1)(B)) | **UNSATISFIED — action required** | we control this; must be added to the task manifest |
| 8 | BY-SA decks, if used, are redistributed under BY-SA (copyleft propagates to the damaged deck) | **UNKNOWN / decide** | 226 rows. Simplest is to **exclude BY-SA entirely**, costing 2.16%; otherwise the derived task assets inherit BY-SA, which may conflict with the licence of the wider task repo |
| 9 | `other-open` / `other-at` / `nlpl` / `etalab-2.0` / `ogl-uk-3.0` terms actually permit our use | **UNKNOWN** | 21 rows total, terms not machine-readable. Cheapest resolution: **exclude**. Cost is 0.2% |
| 10 | Embedded third-party images inside a CC-BY deck are themselves licensed for redistribution | **UNKNOWN — irreducible residual risk** | The uploader licensed *the deck*. A deck may embed a stock photo, a figure from a paywalled paper, or a corporate logo the uploader had no right to sublicense. Extracting media and republishing it standalone strips it from the context that made its inclusion arguable. **No metadata can resolve this.** |
| 11 | Personal data / PII in decks is handled | **UNKNOWN** | corpus contains CVs, author photos, institutional material; no PII screening exists upstream |
| 12 | Files are unmodified originals, so provenance claims hold | **SATISFIED** | md5 verified byte-identical to Zenodo |

### Bottom line

**Conditions 1–5 and 12 are satisfied today.** A single pandas filter on the `license`
column — keep only the permissive bucket, drop the 166 NC/ND/closed/unspecified rows and
(recommended) the 226 BY-SA and 21 unverifiable-other rows — yields **≈ 9,799 decks,
93.8% of the corpus**, all cleanly redistributable and modifiable. Intersected with the
5–25 slide band that is still **roughly 4,300–6,100 usable decks**. Licensing is not a
blocker for this corpus.

**Two conditions need work before first publication (6, 7)** and both are cheap: fetch
`metadata.creators` from the Zenodo API for the few hundred decks we actually select, and
emit an attribution + "modified" notice alongside each shipped task. Budget one Zenodo API
call per selected deck; the DOI is already in the metadata.

**One condition (8) is a decision, not a discovery** — excluding BY-SA costs 2.2% and
removes a copyleft-propagation question entirely. Recommend excluding.

**The single largest residual risk is #10: embedded third-party media.** Our plan
specifically ships *images pulled out of the original deck* as standalone reference
material. That is the operation most likely to redistribute something the uploader never
had the right to license, and **no amount of metadata analysis can clear it** — the
`license` field describes the deck, not every asset inside it. Mitigations to consider:
prefer decks whose media are charts/screenshots over photographs; keep extracted media at
low resolution and clearly framed as task reference rather than a standalone image corpus;
retain the DOI on every derived artefact so any complaint is traceable to a source record
and can be honoured by takedown.

Secondary risk: the dataset card asserts all files are "under a clear and compliant license"
and the paper claims non-permissive artefacts "were filtered out." **Both statements are
false as applied to the published files** (166 counterexamples). Do not inherit that
assurance — run our own filter and record which licence each shipped task derived from.
