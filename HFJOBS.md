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

Still unknown, and only a container can answer them:

- `/dev/shm` defaults to 64 MB in a container and office applications commonly
  want more. `hf jobs run` exposes no `--shm-size`, and with no `CAP_SYS_ADMIN`
  we cannot mount our own tmpfs — so if WPS needs it, the workaround is not
  obvious and we should find out early. `smoke.sh` prints `df -h /dev/shm`.
- Whether a dialog we have never seen appears on a machine with no user
  profile at all.

`smoke.sh` is the instrument for both: seven checks, in the order a failure
would stop the pipeline, ending with a real WPS round trip. **It runs 7/7 green
on this machine**, which is what makes a red line in a container mean the
container rather than the script. Two of its own bugs were found that way — it
asserted `pg-1.png` where `pdftoppm` zero-pads to `pg-01.png`, and it let a
stale PDF stand in for a working `soffice`.

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

**Anthropic is different and is a decision, not a lookup.** There is no
`ANTHROPIC_API_KEY` on this machine: every `claude -p` the pipeline has ever
run went through the subscription's OAuth credential, and a container has no
way to complete an interactive login. So either the agent stages run on an API
key — which turns the measured $87.85 per ten decks from included capacity
into billed spend — or a personal OAuth credential is handed to a third-party
job runner. That is the user's call and it gates the whole B run, because the
agent stages are the pipeline.

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
