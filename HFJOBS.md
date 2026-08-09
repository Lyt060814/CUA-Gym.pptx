# Getting the pipeline onto HF Jobs

Written 2026-08-05, before any of it exists. The B run — ten fresh decks,
cold, all eleven stages — is meant to happen there rather than here, so that
what we measure is the thing we will actually scale.

## The plan, in order

1. **Re-run and submit the eight.** Local, ~30 min, mostly cache hits. They
   are published as `1100001`–`1100008` through the new submission path, which
   is also what proves that path. `1100001`–`1100013` are deleted from the
   rollout repo in the same change, so the two numberings never overlap.
2. **Build the image and prove WPS starts in it.** Locally if Docker can be
   made to run here, otherwise on HF.
3. **One deck, on HF Jobs.** ~$2. The point is not the task; it is that the
   image, the secrets, the corpus fetch and the result upload all work.
4. **Ten decks, cold.** The B run.

Step 3 exists because the expensive failure is not a wasted job — it is a
wasted feedback cycle. A ten-deck run that dies three minutes in costs an
hour of turnaround to diagnose blind.

## What has to be built

### The image — written, and most of WPS turned out to be known

`image/bootstrap.sh`, `image/Dockerfile`, `image/smoke.sh`. The work is in the
script rather than only the Dockerfile because the two routes into a container
differ: baked, it is a `RUN` line; on a stock `python:3.12` job it is the first
command, needing no registry and no local Docker daemon.

Three of the five WPS unknowns are now settled, from this machine:

- **The `.deb` fetches non-interactively.** `wps-office_11.1.0.11723.XA_amd64.deb`
  from the Kingsoft CDN, 319 MB, HTTP 200. The 12.x URL is 403, so 11.x is not
  merely the pinned choice but the available one.
- **The pin is the version we measured.** This machine runs
  `wps-office 11.1.0.11723.XA` — the same build every `wps_roundtrip.py` wait,
  the notes-pane trick and the ~2-in-80 startup segfault rate were measured
  against. A different build would silently invalidate those numbers.
- **The first-run dialog is one config key.** `~/.config/Kingsoft/Office.conf`
  on a working install carries `common\AcceptedEULA=true`, and nothing else in
  that directory is about consent. `bootstrap.sh` seeds that key *only*: the
  rest of the file is telemetry counters and machine identifiers
  (`infoGUID`, `deviceid`, `VLGDeviceKey`), and copying it wholesale would
  stamp one developer's device identity onto every container.

**WPS runs in a container.** Seven probe jobs on HF Jobs, a few cents each,
settled it: the package installs cleanly, `wpp` starts under Xvfb with no
desktop session, and it opens a deck — `probe.pptx - Presentation`.

What each run bought, since the pattern matters more than the list:

- Two runs on postinst commands the package needs and does not declare. After
  the second I stopped guessing and read all six out of the postinst on this
  machine, which has WPS installed. **The local install is the reference; ask
  it before asking a job.**
- One run lost to `--break-system-packages`, which arrived in pip 23 while
  Ubuntu 22.04 ships pip 22 — and to the probe swallowing pip's error and
  blaming WPS for the missing file. A diagnostic that can lie about which
  component failed is worse than none.
- One run to `libxslt.so.1`, which the container's own `dlopen` named. Worth
  noting what did *not* help: `ldd` over `office6` reports 70 unresolved
  libraries **on a working install**, so that list is noise. The dlopen
  message was the only signal.
- One run to find, by enumerating every window rather than only looking for
  the one it wanted, two first-run dialogs holding focus: `WPS Office` and
  `System Check`. `wps_roundtrip._settle_dialogs` already closes exactly
  these — it checks for `System Check|Tip|Prompt` by name. Seeding
  `common\SystemCheck\DoNotReport=true` alongside the EULA key and closing
  what remains clears the display.

Two assumptions recorded here were wrong, both measured on the real hardware:

- **`/dev/shm` is 486 GB, not 64 MB.** The worry about office applications
  needing more, and about having no `CAP_SYS_ADMIN` to mount a tmpfs, does not
  apply.
- **`nproc` reports 64 inside a `cpu-upgrade` container that has 8 vCPU.**
  Pool sizes must not be derived from it.

Still open: the probe never proved a *save*. That is not yet a container
finding — WPS treats saving an unmodified document as a no-op exactly as
PowerPoint does, and the probe pressed Ctrl+S on a file it had not managed to
dirty. `wps_roundtrip.py` knows this and dirties the notes pane first, so
**the real instrument is the pipeline's own code, not a toy** — which needs
the repository inside the container.

`smoke.sh` is the instrument for both: seven checks, in the order a failure
would stop the pipeline, ending with a real WPS round trip. **It runs 7/7 green
on this machine**, which is what makes a red line in a container mean the
container rather than the script. Two of its own bugs were found that way — it
asserted `pg-1.png` where `pdftoppm` zero-pads to `pg-01.png`, and it let a
stale PDF stand in for a working `soffice`.

### The WPS round trip does not work in a container, and we stopped looking

Everything else does. Six of the seven checks in `smoke.sh` pass on HF Jobs —
imports, `soffice`, `pdftoppm`, Xvfb, and `claude -p` authenticating on the
subscription token. The round trip is the seventh and it has failed every
time, always the same way: the document never goes modified, so WPS treats the
save as a no-op and nothing is written.

**Eight explanations were proposed and all eight were wrong**: a modal dialog
holding focus, a missing keymap, a deadline too short for a slow machine, a
window not filling the screen, the X input focus, the deck itself, launching
the binary instead of the wrapper, and no settling time between the click and
the type. Each cost a ten-minute container run. Three of the eight were fixed
anyway because they were real defects — the dialog pattern missed `WPS Office`,
the profile was cold on every run, `DIRTY_WAIT` was calibrated to one machine.

What is established, and it is precise:

* A standalone probe **dirties the document** in the same container, with the
  same deck, the same 1920x1200 screen, the same `(500, 1143)`, and the same
  plain `xdotool type`.
* The pipeline, doing what looks like the identical thing, does not.
* So there is still a difference between the two, and it is not any of the
  eight things checked.

The honest read: `wps_roundtrip` drives a GUI by coordinates and keystrokes,
and that is fragile in a way this project cannot afford to keep paying for at
ten minutes a hypothesis. Finding the remaining difference wants the probe and
the pipeline reduced to one process and bisected inside it — worth doing, not
worth doing before the corpus is being scaled.

**So it runs with `--no-wps` on HF Jobs, and this is a recorded gap rather
than a quiet weakening.** What is lost:

* the `gt_roundtrip` attack, the only one that puts the ground truth through
  the application the task is actually solved in. `harden` already records a
  rejection reason saying the sweep proves nothing about it.
* the measurement that sets position tolerances.

Both are CPU-only work of about twelve seconds a deck, no agent involved, so
running them on this machine is not a scaling bottleneck — it is a second pass
over the same decks. That is the arrangement until the difference is found.

### The probe's kernel barrier does not work here either

`unshare --user --mount` is refused on HF Jobs — `Operation not permitted`,
with no `CAP_SYS_ADMIN` to fix it. Every deck stopped at `solvable`, which is
the code behaving correctly: the barrier fails the stage rather than quietly
downgrading itself, because the whole defect it was built for was a barrier
everyone believed was in force.

**Run with `PPTXGYM_PROBE_BARRIER=cwd`, at the user's decision, 2026-08-06.**
What that keeps and what it costs, so the trade is legible later:

*Kept.* The probe still runs in a temporary directory holding the bundle and
nothing else. The `deny` rules still apply, in the `//abs/**` form that was
*measured* to work — the plain `/abs/**` form denies nothing — and that form
covers Bash commands naming the path as well as reads. The log scan still
voids any verdict produced by a probe that reached outside its bundle. Every
`probe.json` records `barrier: deny`, so a verdict is always readable next to
the strength of what produced it.

*Lost.* The kernel mount mask, which made reaching the answer key impossible
rather than disallowed. The threat model moves from "cannot" to "may not, and
will be caught".

**Codex witness fallback, 2026-08-09.** A Claude account reaching its weekly
limit must not stop a Codex batch at `solvable`. The witness is independently
routable with `PPTXGYM_PROBE_ENGINE`, `PPTXGYM_PROBE_MODEL`, and
`PPTXGYM_PROBE_EFFORT`. The production Codex setting is currently
`codex / gpt-5.6-terra / medium`.

Codex does not read Claude's deny settings, so it may never reuse the `deny`
fallback described above. `crun.sh` closes `work/` and `/srv/decks` to
group/other, gives the witness a private HOME with no GitHub/HF credentials,
and permanently drops only that process to uid 65534 with `setpriv` and
`no-new-privs`. Root-owned orchestrators continue to use the same work tree.
Where `unshare` works, the mount namespace remains preferred; where it does
not, UID isolation is an OS-enforced barrier rather than a prompt rule. If
neither can be established, the stage fails instead of probing unsealed.

Worth remembering why the barrier exists at all: the probe was **measured**
reading not only its own deck's `plan.json` and `delta.json` but every other
deck's directory and the undamaged corpus, and the detector of the day only
knew the deck being probed — so reading a sibling's answer key scanned as
clean.

### Getting results out, incrementally

The disk is ephemeral. A job that dies at minute 40 takes everything with it,
so the run log and enough of `work/` to diagnose must leave *during* the run,
not at the end. `runs/<run-id>/events.jsonl` is the minimum.

### Watching it

Supervision here was sampling a local file every thirty seconds. That is not
available on HF Jobs. Either the observer's alerts go somewhere reachable, or
we accept post-hoc analysis and size the run accordingly.

### Blocked: HF Jobs refuses every namespace we have

Tried 2026-08-05, all three:

    Lytttttt    402  Pre-paid credit balance is insufficient
    xlangai     402  A valid payment method is required to use Jobs
    osworldv3   402  Pre-paid credit balance is insufficient

Nothing runs there until this is resolved — not the ten-deck run, not the
single-deck smoke, not the free-tier runtime probe. **This blocks steps 2, 3
and 4 of the plan above**, and it is the first thing to fix.

Cheaper than assumed once it is unblocked: `cpu-basic` is $0.01/h, so the
runtime-cap probe costs cents rather than the $0.72 first estimated. Also
found while reading the CLI: `-v hf://buckets/org/b:/mnt` mounts a bucket
**read-write**, which is a better answer to incremental result upload than
pushing during the run, and `-v hf://datasets/org/ds:/data` mounts the corpus
read-only rather than fetching it.

### Settling the runtime cap

Undocumented: the default is 30 minutes and `--timeout` accepts `3d`, but the
only published number is a client-side constant. Settle it for cents before
betting a real run on it:

    hf jobs run --detach --timeout 3d --flavor cpu-basic python:3.12 \
        python -c "import time; [print(i, flush=True) or time.sleep(60) for i in range(9999)]"

then poll.

### Credentials, and the one that is not a secret we hold

Hugging Face (corpus in, assets out) and GitHub (task files out) are ordinary
tokens, passed with `--secrets`. Both work from here today.

**Anthropic looked like a decision and turned out to be a lookup.** There is no
`ANTHROPIC_API_KEY` here — every `claude -p` the pipeline has run went through
the subscription's OAuth credential — and HF Jobs offers no way in to log in:
its subcommands are `run / logs / ps / inspect / cancel / stats / scheduled`,
with **no `exec`, no `attach`, no shell**. A job is fire-and-forget.

`claude setup-token` is the mechanism for exactly this: a long-lived token
(one year), passed as `CLAUDE_CODE_OAUTH_TOKEN`. It draws on the subscription,
so the measured $87.85 per ten decks stays included capacity rather than
becoming billed API spend, and no personal credential file goes to a
third-party runner.

Verified rather than assumed: `claude -p` under a **fresh empty `HOME`** with
only that variable set answers, and writes no `.credentials.json` — so the
token really is the only auth, not a fallback quietly finding the file. That
test is the one worth repeating in any new environment, because "it worked"
and "it worked for the reason I think" are different answers.

The token lives in `pptx-tasks/.env_vars` (gitignored, now `chmod 600`).

### Concurrency

32 vCPU / 256 GB. RAM stops being the constraint — roughly 400 concurrent
`claude -p` by memory — and the **account rate limit** governs the agent pool,
which is per account and therefore not bought by adding machines. CPU stages
around 8–16; WPS round trips bounded by CPU rather than by memory.

## The corpus for the B run

`/home/yitongli/XLANG/pptx-tasks/WPS_PPT_Scaling`. Two things recorded here
earlier were wrong, and both were wrong in the same direction — they made the
corpus look bigger and better-organised than it is.

**It is 220 decks, not 1452.** 1452 is the file count. Hashing them gives 220
distinct decks: 110 that appear in some `accepted/` directory and 110 that
never do. The nine numbered directories are **not a partition of the decks** —
they are labels saying which kind of degradation a deck is a candidate for,
and every one of the 110 accepted decks appears in more than one of them
(commonly six or seven). Difficulty *is* per-deck: easy 56, medium 65,
hard 99.

So "pick across families" buys nothing. Picking has to go on what a deck
actually contains.

**The last ten were one bucket.** All ten came from
`04_layout_structure_scatter/hard/accepted`. The proposals resembling one
another was recorded here as an observation about the model; it was a sampling
error. Of those ten, none had a chart, none had animation, and none was
outside a narrow band of slide counts.

The ten picked for the B run (`/tmp/brun_picks.json`, and named in the commit
that added this paragraph) spread deliberately: 1 to 40 slides, easy/medium/
hard, five with charts, six with SmartArt, four with tables, five animated.
Decks dominated by EMF/WMF vector media were avoided on purpose — that is what
made deck0008 unearnable, since the reward compares picture bytes exactly and
an instruction that says "cut it from the reference" can never produce them.

**This corpus does not reach 450 tasks.** 110 usable decks at two or three
tasks each is two or three hundred, and that assumes every deck yields. Worth
settling before the delivery plan depends on it.

Do not reuse the ten already processed under `work/`.
