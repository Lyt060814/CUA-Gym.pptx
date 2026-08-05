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

### The image, and WPS is the only real unknown

Everything else is `apt-get`: LibreOffice (`soffice`), `poppler-utils` for
`pdftoppm`, `xvfb`, `xdotool`, CJK fonts, Node plus the `claude` CLI, and the
Python dependencies.

WPS is proprietary and **we have never installed it** — on the OSWorld AMI it
is already there. Unknowns, each of which can only be settled by trying:

- where the `.deb` comes from, and whether it can be fetched non-interactively
- the first-run EULA or setup wizard, which `_settle_dialogs` has never seen
- `/dev/shm` defaults to 64 MB in a container; office applications commonly
  need more
- no `CAP_SYS_ADMIN` on HF Jobs, so nothing that wants a nested namespace
- WPS segfaults on startup at a measured ~2 in 80 launches even on this
  machine; `roundtrip_wps` retries once, which should carry over

### Getting results out, incrementally

The disk is ephemeral. A job that dies at minute 40 takes everything with it,
so the run log and enough of `work/` to diagnose must leave *during* the run,
not at the end. `runs/<run-id>/events.jsonl` is the minimum.

### Watching it

Supervision here was sampling a local file every thirty seconds. That is not
available on HF Jobs. Either the observer's alerts go somewhere reachable, or
we accept post-hoc analysis and size the run accordingly.

### Settling the runtime cap first

Undocumented: the default is 30 minutes and `--timeout` accepts `3d`, but the
only published number is a client-side constant. Settle it for about $0.72
before betting a real run on it:

    hf jobs run --detach --timeout 3d cpu-basic sleep infinity

then poll.

### Credentials

Anthropic (agent stages), Hugging Face (corpus in, assets out), GitHub (task
files out). All via `--secrets`.

### Concurrency

32 vCPU / 256 GB. RAM stops being the constraint — roughly 400 concurrent
`claude -p` by memory — and the **account rate limit** governs the agent pool,
which is per account and therefore not bought by adding machines. CPU stages
around 8–16; WPS round trips bounded by CPU rather than by memory.

## The corpus for the B run

`/home/yitongli/XLANG/pptx-tasks/WPS_PPT_Scaling` — 1452 decks, median 3.4 MB,
max 23.5 MB, already sorted into eight degradation families. Ten are picked by
hand for this run; the pre-filter is deliberately not built yet (see
`BACKLOG.md`).

**Pick across families.** The last ten decks produced proposals that resembled
one another, and the repair loop took 60% of the run's agent time. Variety in
the corpus is the cheapest available lever on that.

Do not reuse the ten already processed under `work/`.
