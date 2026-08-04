# Platform options for the pptx batch pipeline

Investigated 2026-08-04. Read-only: no account created, no job started, nothing pushed.

**Workload under test.** Per `.pptx` deck, spawn the Claude Code CLI (`claude -p`, Node, 632 MB RSS
measured) as a subprocess calling `api.anthropic.com` over the public internet; also run LibreOffice
headless for rendering. ~30 min agent time per deck; a batch of several hundred decks runs for days.
Fully resumable — state on disk, content-hashed.

**Local tooling used for evidence** (preferred over prose where it disagrees):

- `huggingface_hub` 1.9.0 at `/home/yitongli/.local/lib/python3.12/site-packages/huggingface_hub`
  (latest is 1.26.0 — the docs below are the 1.26.0 docs, so a few flags are newer than the local CLI)
- `hf` CLI at `/home/yitongli/.local/bin/hf`
- The Hub OpenAPI spec, fetched live from `https://huggingface.co/.well-known/openapi.json`
  (975 KB; the `huggingface-openapi.hf.space` page is only a Scalar viewer shell that points at it)

**Note on the companion doc.** The task referenced
`/home/yitongli/XLANG/CUA-Gym.pptx/corpus-zenodo10k.md`. **It does not exist** — neither that file nor
any file matching `*zenodo*` anywhere under `/home/yitongli/XLANG/CUA-Gym.pptx/`
(`find . -iname "*zenodo*"` returned nothing; directory listing shows only `README.md`, `REWARD.md`,
`TOOLS.md`, `roundtrip-remeasure.md`, `wps-verification.md`). Section 4 below therefore answers the
platform half only and explicitly flags what belongs to the missing corpus half.

---

## 1. HF Jobs

### 1.1 Maximum wall-clock runtime per job

**Default: 30 minutes. Documented maximum: none. Per-tier differences: none documented.**

- Default, stated three times in the docs:
  > "Jobs have a default timeout (30 minutes), after which they will automatically stop."
  — [jobs-configuration#timeout](https://huggingface.co/docs/hub/jobs-configuration),
  [huggingface_hub guides/jobs](https://huggingface.co/docs/huggingface_hub/en/guides/jobs),
  and [jobs-pricing](https://huggingface.co/docs/hub/jobs-pricing) ("Note that the default timeout is
  set to **30 minutes**").
- `hf jobs run --help` (local CLI, verbatim):
  ```
  --timeout TEXT   Max duration: int/float with s (seconds, default), m (minutes), h (hours) or d (days).
  ```
  The `d` (days) unit is first-class. Docs show `--timeout 1d` as a supported example.
- **No server-side cap in the API schema.** From the live OpenAPI spec, the job-creation body:
  ```json
  "timeoutSeconds": {"default": null, "anyOf": [{"type": "integer", "exclusiveMinimum": 0,
                     "maximum": 9007199254740991}, {"type": "null"}]}
  ```
  `9007199254740991` is `Number.MAX_SAFE_INTEGER` — a JS serialization bound, not a policy. There is
  no schema-level maximum.
- **No client-side validation either.** `_jobs_api.py:371-391` just multiplies by the unit factor and
  sets `job_spec["timeoutSeconds"]`; no bound check.
- **Tiers.** Jobs are not gated on plan. "Hugging Face Jobs are available to any user or organization
  with a positive [credit balance]" ([guides/jobs](https://huggingface.co/docs/huggingface_hub/en/guides/jobs),
  [jobs-pricing](https://huggingface.co/docs/hub/jobs-pricing)). No free/PRO/Enterprise runtime ladder
  is documented anywhere I looked.

**The 24-hour number, and why it is a red herring.** The Sandboxes guide says:

> "The job also has a fixed 24h maximum lifetime as a hard backstop (not configurable)."
> — [guides/sandbox#lifecycle](https://huggingface.co/docs/huggingface_hub/en/guides/sandbox)

This reads like a platform cap but **is not**. I pulled the source
(`gh api repos/huggingface/huggingface_hub/contents/src/huggingface_hub/_sandbox.py`):

```python
SANDBOX_MAX_LIFETIME = "24h"          # line 65
...
    timeout=SANDBOX_MAX_LIFETIME,     # lines 586 and 1512 — passed into run_job()
```

It is a **client-side constant the sandbox helper passes as the job's own `timeout`**. Ordinary
`hf jobs run` is not subject to it.

> **UNKNOWN:** whether an *undocumented operational* cap exists (node draining, spot preemption,
> scheduler eviction). Nothing in the docs, the OpenAPI spec, the client source, or the two searches I
> ran states one — but absence of a documented cap is not a guarantee of uptime. **Do not bet weeks of
> work on a multi-day contiguous run without an empirical test**: submit one `hf jobs run --detach
> --timeout 3d cpu-basic sleep infinity`, poll `hf jobs inspect`, and see when it actually dies.
> Cost of that test is ~$0.72 for 3 days on `cpu-basic`. Note that HF themselves chose 24h as the
> backstop for their own sandbox product, which is weak evidence that ≤24h is the well-trodden path.

### 1.2 CPU flavors and RAM

Confirmed three ways — the docs table, the pricing page, and `hf jobs hardware` run live against
`GET /api/jobs/hardware` (first attempt 429-rate-limited from this IP unauthenticated; the retry
succeeded):

| flavor | vCPU | RAM | ephemeral storage | $/hr |
|---|---|---|---|---|
| `cpu-basic` (default) | 2 | 16 GB | 50 GB | $0.01 |
| `cpu-upgrade` | 8 | 32 GB | 50 GB | $0.03 |
| `cpu-xl` | 16 | 124 GB | 1000 GB | $1.00 |
| **`cpu-performance`** | **32** | **256 GB** | **1024 GB** | **$1.90** |

Sources: [jobs-pricing#cpu](https://huggingface.co/docs/hub/jobs-pricing),
[jobs-configuration#hardware-flavor](https://huggingface.co/docs/hub/jobs-configuration) (which embeds
the literal `hf jobs hardware` output), and my own live `hf jobs hardware` run.

**Largest CPU flavor = `cpu-performance`, 32 vCPU / 256 GB RAM / 1024 GB disk / $1.90 per hour.**

At 632 MB per agent, 256 GB is ~400 agents by memory. **vCPU, not RAM, is the binding constraint** —
32 cores. Since a `claude -p` agent is mostly blocked on network I/O you can oversubscribe, but
LibreOffice rendering is CPU-bound and will not oversubscribe well.

> Two flavor names appear in the local CLI's `--flavor` enum but not in any pricing table:
> `sprx8` (local CLI 1.9.0) and `inf2x6` (in the live OpenAPI enum). **UNKNOWN** what these are — not
> in `hf jobs hardware` output, not in `jobs-pricing`. Presumably Inferentia/TPU; irrelevant to us.

The OpenAPI spec also exposes `"arch": {"enum": ["amd64", "arm64"]}` on the job spec.

### 1.3 Outbound public internet — **YES**

This is the answer I was least willing to guess at, and there is no direct sentence in the Jobs docs.
The authoritative statement is in the Sandboxes *conceptual* guide, describing the kernel-level
confinement of a stock Job:

> "A cannot `bind` a TCP port, so there is no inter-sandbox localhost service (**outbound `connect`
> stays allowed, so the internet works**)."
> — [concepts/sandbox](https://huggingface.co/docs/huggingface_hub/en/concepts/sandbox)

Three corroborating mechanisms from the same page and the Jobs docs:

1. The sandbox bootstrap "downloads the binary from the HF CDN with `wget` or `curl`" at job startup.
2. `hf jobs uv run` is the *default* Jobs workflow and installs Python dependencies from PyPI at
   runtime inside the container.
3. Docker images are pulled from Docker Hub.

No egress allowlist, proxy requirement, or blocked-port list is documented for Jobs. (Contrast
**Spaces**, which *does* publish a restriction — see §2.) So `api.anthropic.com:443` is fine.

**Sandboxing caveat that affects us.** Same page, describing a stock Job:

> "A stock Job runs as root inside a user namespace that maps only uids 0..65535, with a seccomp
> filter on and without `CAP_SYS_ADMIN` / `CAP_NET_ADMIN` / `CAP_NET_RAW`. That rules out the usual
> heavyweight isolation tools: no nested namespaces, no new mounts, no cgroup delegation (`unshare`,
> `mount`, writing to `/sys/fs/cgroup/...` all fail)."

Consequences for our image: no Docker-in-Docker, no `unshare`. LibreOffice headless and a Node
process do not need those. Anything that tries to build its own sandbox (a bundled Chromium, for
instance) would need `--no-sandbox`.

### 1.4 Secrets / environment variables — **YES**

From `hf jobs run --help` (local CLI, verbatim):

```
-e, --env TEXT          Set environment variables. E.g. --env ENV=value
-s, --secrets TEXT      Set secret environment variables. E.g. --secrets SECRET=value
                        or `--secrets HF_TOKEN` to pass your Hugging Face token.
    --env-file TEXT     Read in a file of environment variables.
    --secrets-file TEXT Read in a file of secret environment variables.
```

> "Pass secrets - they will be **encrypted server side**."
> — [guides/jobs#pass-environment-variables-and-secrets](https://huggingface.co/docs/huggingface_hub/en/guides/jobs)

So `--secrets ANTHROPIC_API_KEY=...` or `--secrets-file .env.secrets`. Built-ins also injected:
`JOB_ID`, `ACCELERATOR`, `CPU_CORES`, `MEMORY`
([jobs-configuration#built-in-environment-variables](https://huggingface.co/docs/hub/jobs-configuration)).

### 1.5 Arbitrary Docker image — **YES**

`hf jobs run --help`: `IMAGE  The Docker image to use.  [required]`.

> "Hugging Face Jobs supports any workload based on Docker and a command … you can specify a Docker
> image from Hugging Face Spaces or Docker Hub, as well as the command to run."
> — [jobs-overview](https://huggingface.co/docs/hub/jobs-overview)

Examples in the docs span `python:3.12`, `ubuntu`, `duckdb/duckdb`, `pytorch/pytorch:...`,
`vllm/vllm-openai:latest`, and `hf.co/spaces/lhoestq/duckdb`. The only stated image requirement (from
the sandbox design notes) is `/bin/sh` and `x86_64` Linux. So baking Node + `@anthropic-ai/claude-code`
+ LibreOffice into one image is squarely supported.

Billing note in our favour: **"there is no cost during build"** ([jobs-pricing](https://huggingface.co/docs/hub/jobs-pricing)) —
billing covers only `Starting` and `Running`. A fat image with LibreOffice is not billed while pulling.

### 1.6 Storage: ephemeral vs persistent

- **Ephemeral disk per job**, sized by flavor — the "Ephemeral Storage" column above: 50 GB on
  `cpu-basic`/`cpu-upgrade`, **1000 GB on `cpu-xl`, 1024 GB on `cpu-performance`**.
  > "Ephemeral disk doesn't survive the Job."
  > — [jobs-large-datasets#save-results](https://huggingface.co/docs/hub/jobs-large-datasets)
- **Persistence across runs = Storage Buckets**, mounted as a read-write volume:
  ```bash
  hf jobs run -v hf://buckets/username/my-bucket:/mnt python:3.12 python script.py
  ```
  > "Storage buckets are **read-write by default** … Files written under the bucket mount path
  > **persist after the Job ends**."
  > — [jobs-configuration#volumes](https://huggingface.co/docs/hub/jobs-configuration),
  > [jobs-large-datasets](https://huggingface.co/docs/hub/jobs-large-datasets)

  Buckets are S3-like, **non-versioned and mutable**, Xet-backed, "available to all users and
  organizations" ([storage-buckets](https://huggingface.co/docs/hub/storage-buckets)). The docs name
  our exact use case:
  > "**Agentic storage.** AI agents need scratch storage for intermediate results, tool outputs,
  > traces, and working memory."

  Also: **"Data processing pipelines** … Process raw data, write intermediate outputs to a bucket,
  then promote the final artifact to a versioned Dataset repository when the pipeline completes."

**Our ~7 GB per 100 decks is a non-issue.** 10,000 decks ≈ 700 GB — that fits on `cpu-xl`/`cpu-performance`
ephemeral disk even within a single run, and a bucket removes the constraint entirely. Bucket storage is
billed per TB against the same account quota (§3), with a free allowance.

Resumable-state design that follows directly: keep the content-hashed state dir **in a bucket mounted
read-write**, so a killed job restarts and picks up where it left off with zero extra machinery.

### 1.7 Billing

> "Billing on Jobs is based on hardware usage and is **computed by the minute** … During a Job's
> lifecycle, it is only billed when the Job is **Starting or Running**. This means that there is no
> cost during build. If a running Job starts to fail, it will be automatically suspended and the
> billing will stop." — [jobs-pricing](https://huggingface.co/docs/hub/jobs-pricing)

Pay-as-you-go against a **credit balance**; requires a credit card
([billing](https://huggingface.co/docs/hub/billing): "The only payment method supported for Hugging
Face compute services is credit cards"). Bill to an org with `--namespace`. Exposed ports cost an
extra flat $0.01/hr per job (we don't need them).

> **UNKNOWN: concurrency limits on Jobs.** "Many jobs can run in parallel"
> ([jobs-overview](https://huggingface.co/docs/hub/jobs-overview)) but no per-account cap on
> simultaneous jobs is published. Looked in jobs-overview, jobs-pricing, jobs-configuration,
> rate-limits, and the OpenAPI spec.

---

## 2. HF Spaces as a multi-day host

**Headline finding that changes the calculus:** running a Space on compute now requires a paid plan.

> "Static Spaces are free for everyone. **Gradio and Docker Spaces run on compute and require a paid
> plan to create: PRO for personal accounts, Team or Enterprise for organizations.** Free personal
> accounts in good standing can still host up to 2 Gradio Spaces running on ZeroGPU."
> — [spaces-overview](https://huggingface.co/docs/hub/spaces-overview)

**CPU/RAM tiers** ([spaces-gpus#hardware-specs](https://huggingface.co/docs/hub/spaces-gpus)) — note
there are only **two** CPU options, and the ceiling is far below Jobs:

| Hardware | vCPU | Memory | Disk | $/hr |
|---|---|---|---|---|
| CPU Basic (default) | 2 | 16 GB | 50 GB | Free (but Space creation needs a paid plan) |
| CPU Upgrade | 8 | 32 GB | 50 GB | $0.03 |

Everything above CPU Upgrade is a GPU flavor. **8 vCPU / 32 GB is the CPU ceiling on Spaces** vs
32 vCPU / 256 GB on Jobs.

**Persistent storage.** The old fixed persistent-storage tiers are gone; the current doc says:

> "Every Space comes with a small amount of disk storage. **This disk space is ephemeral**, meaning
> its content will be lost if your Space restarts or is stopped. If you need to persist data with a
> longer lifetime than the Space itself, you can attach one or more **Storage Buckets** as volumes."
> — [spaces-storage](https://huggingface.co/docs/hub/spaces-storage)

Same bucket mechanism as Jobs — so no storage advantage either way.

**Sleep / idle behaviour:**

> "If your Space runs on the default `cpu-basic` hardware, it will go to sleep if inactive for more
> than a set time (**currently, 48 hours**). … **By default, an upgraded Space will never go to
> sleep.**" — [spaces-gpus#sleep-time](https://huggingface.co/docs/hub/spaces-gpus)
>
> "Spaces running on free hardware are suspended automatically if they are not used for an extended
> period of time (e.g. two days). **Upgraded Spaces run indefinitely by default, even if there is no
> usage.**"

So a background worker *can* run for days — on **upgraded** (paid, $0.03/hr) hardware. On free
`cpu-basic` it is suspended after 48h of inactivity, and "inactivity" is measured by visitors, not by
your worker's CPU usage — a headless batch process with no HTTP traffic is exactly the "unused" case.

**Networking — a real restriction, unlike Jobs:**

> "If your Space needs to make any network requests, you can make requests through the standard HTTP
> and HTTPS ports (80 and 443) along with port 8080. **Any requests going to other ports will be
> blocked.**" — [spaces-overview#networking](https://huggingface.co/docs/hub/spaces-overview)

`api.anthropic.com` is HTTPS/443, so this happens not to bite us — but it is a documented egress
restriction that Jobs does not have.

**Other frictions for a multi-day batch process on Spaces:** "Each time a new commit is pushed, the
Space will automatically rebuild and restart" — so the deploy mechanism is a git push, and any commit
kills a running batch. Billing is per-minute "regardless of whether the Space is used". Secrets are
supported (Settings → Variables and secrets, exposed as env vars).

**Verdict on Spaces:** technically capable of multi-day background work on upgraded hardware, but it
is a *web app host* being abused as a batch runner — 4× less CPU than Jobs' ceiling, a git-push deploy
model that restarts on every commit, restricted egress ports, and a PRO requirement just to create it.
Jobs is the same infrastructure with the right interface.

---

## 3. Dataset repos as an output target

All from [storage-limits](https://huggingface.co/docs/hub/storage-limits) (which is where
`repositories-recommendations` now resolves).

### Account-level storage quota

| Account | Public storage | Private storage |
|---|---|---|
| Free user or org | Best-effort | 100 GB |
| PRO | Up to 10 TB included + add-on | 1 TB + pay-as-you-go |
| Team org | 12 TB base + 1 TB/seat | 1 TB/seat + PAYG |
| Enterprise org | 200 TB base + 1 TB/seat | 1 TB/seat + PAYG |

Add-on public storage $12/TB/mo; extra private storage $18/TB/mo. Our few hundred × (few MB pptx +
handful of images) is comfortably inside even the free tier.

### Per-repo limits

| Characteristic | Limit | Nature |
|---|---|---|
| Repo size | no per-repo limit for models/datasets | counts against account quota |
| **File size** | **<200 GB recommended; 500 GB hard limit** | "no single file will exceed 500GB" |
| **Files per repo** | **<100k recommended** | soft, UX degrades |
| **Entries per folder** | **≤10k — HARD** | "cannot exceed 10k files per folder" |
| **Commit size** | **<100 files recommended** | "keep around 50-100 files per commit" |
| Commits per repo | no hard limit; degrades "after a few thousand commits" | soft |

Also: "**When pushing data through HTTP, a timeout of 60s is set on the request**" — relevant if a
single commit carries too much.

### Rate limit on commits/pushes — **UNKNOWN, and authoritatively so**

This is the one number I cannot give you, and the docs say why:

> "In addition to those main classes of rate limits, we enforce limits on certain specific kinds of
> user actions, like: repo creation, **repo commits**, discussions and comments, moderation actions,
> etc. **We don't currently document the rate limits for those specific actions**, given they tend to
> change over time more often."
> — [rate-limits#granular-user-action-rate-limits](https://huggingface.co/docs/hub/rate-limits)

So: **a commit rate limit exists, its value is deliberately undocumented, and there is no
commit-per-hour or files-per-commit *ceiling* published** — only the <100-files-per-commit
recommendation above. The general API bucket, which commits also consume, is published (all quotas
over **5-minute fixed windows**):

| Plan | API | Resolvers | Pages |
|---|---|---|---|
| Anonymous (per IP) | 500 | 3,000 | 100 |
| Free user | 1,000 | 5,000 | 200 |
| PRO user | 2,500 | 12,000 | 400 |
| Team org | 3,000 | 20,000 | 400 |
| Enterprise org | 6,000 | 50,000 | 600 |

I hit this live during this investigation: an unauthenticated `hf jobs hardware` returned
`429 Too Many Requests … 0/10000 requests remaining in current 300s window`, with the advice to pass
an `HF_TOKEN`. `huggingface_hub` ≥1.2.0 auto-parses the `RateLimit` header and sleeps exactly the
reset interval, so use the library rather than raw HTTP.

### Is incremental pushing the intended pattern?

**Per-deck commits: no. Time-batched commits: yes, that's the supported idiom.** Two corrections to
the assumption in the brief:

1. **`upload_large_folder` is deprecated.**
   > "The legacy `upload_large_folder()` method and `hf upload-large-folder` command are
   > **deprecated** and will be removed in a future release. Use `upload_folder()` / `hf upload`
   > instead." — [guides/upload](https://huggingface.co/docs/huggingface_hub/en/guides/upload)

   `upload_folder` now does what `upload_large_folder` did: streamed pipeline, "folders with many
   files are automatically split into several commits to stay below server limits", and **resumable** —
   "if the upload is interrupted for any reason, simply re-run the same call. Files already committed
   are detected and skipped … **No local state is involved: you can even resume from a different
   machine.**" That resumability composes perfectly with our content-hashed pipeline state.

2. **For a long-running producer, the documented tool is `CommitScheduler`**, which batches on a timer:
   ```python
   scheduler = CommitScheduler(repo_id=..., repo_type="dataset",
                               folder_path=..., path_in_repo="data", every=10)  # minutes
   ```
   > "The scheduler will commit the folder every `every` minutes. **To avoid polluting the git
   > repository too much, it is recommended to set a minimal value of 5 minutes.**" It drops empty
   > commits automatically and is thread-safe via `scheduler.lock`.

**Recommended shape for our output.** Three layers, in order of preference:

- **During the run:** write completed deck bundles into a **Storage Bucket** mounted read-write. No
  git, no commits, no rate limit, mutable, and the docs explicitly bless it as the staging layer for a
  pipeline. Zero commit pressure over days.
- **Publishing:** at the end (or on a `CommitScheduler` timer at ≥5 min), `upload_folder` the bundle
  tree into a dataset repo. A few hundred commits is well under the "few thousand" degradation point,
  but batching to one commit per N minutes keeps it in the tens.
- **Layout:** shard by prefix (`decks/00/`, `decks/01/`, …) so **no folder exceeds the hard 10k-entry
  cap**. At a few hundred decks this is precautionary; at 10,000 it is mandatory — and it is exactly
  what Zenodo10K itself does (see §4).
- Parquet shards are the right format only if the consumer is `datasets`; for opaque `.pptx` binaries
  that downstream tools open by path, plain files (or WebDataset tars) are more usable.

> One caveat on the bucket route, from [storage-buckets](https://huggingface.co/docs/hub/storage-buckets):
> "transferring data the other way from a bucket to a repository (model, dataset, Space) **without
> reuploading is not yet available**, but is on the roadmap." Repo→bucket is server-side and instant;
> bucket→repo is a re-upload. Budget for that if you stage in a bucket and publish to a dataset repo.

---

## 4. Reading `Forceless/Zenodo10K` from inside HF compute

> Scope note: the companion corpus investigation at `corpus-zenodo10k.md` **does not exist** (see
> header). Everything below is the *platform* half — how a job gets at the bytes. What is actually
> *in* the rows, licence mix, and deck quality is the corpus half and is **not** answered here.

**Dataset facts, from the live Hub API** (`GET /api/datasets/Forceless/Zenodo10K?full=true`):

- Public, **not gated**, 10,992 downloads, 26 likes, last modified 2025-10-31.
- Tags include `format:parquet`, `size_categories:10K<n<100K`, `arxiv:2501.03936`.
- **10,451 files**: 10,448 `.pptx` under `pptx/<licence>/<year>/<md5>-<name>.pptx`
  (e.g. `pptx/apache2.0/2021/dd7f08ed…-escience_2021_conferentie_2.pptx`), plus `README.md`,
  `.gitattributes`, and **one parquet: `data/pptx-00000-of-00001.parquet`**.
  Note the repo is already sharded by licence/year — consistent with the 10k-per-folder hard cap.
- Auto-converted parquet exists:
  `GET /api/datasets/Forceless/Zenodo10K/parquet` → `{"default":{"pptx":[".../parquet/default/pptx/0.parquet"]}}`
- Viewer capabilities (`GET https://datasets-server.huggingface.co/is-valid?dataset=Forceless/Zenodo10K`):
  `{"preview":true,"viewer":true,"search":true,"filter":true,"statistics":true}` — so streaming and
  server-side filtering are supported.

**Can a job stream or mount rows without a full download? Yes — three ways**
([jobs-large-datasets](https://huggingface.co/docs/hub/jobs-large-datasets)):

1. **Mount** — the right one for us, because LibreOffice and the Claude CLI want real file paths:
   ```bash
   hf jobs uv run --flavor cpu-upgrade -v hf://datasets/Forceless/Zenodo10K:/mnt/data process.py
   ```
   > "Because mounted files are **fetched lazily**, mounting lets a Job work with datasets far larger
   > than its local disk." … "Mounting is the natural fit when files are consumed whole — model
   > weights, audio or image files, archives — or when a tool only accepts file paths."

   You can even mount a subfolder: `-v hf://datasets/Forceless/Zenodo10K/pptx/apache2.0:/mnt/data`.
   Datasets mount **read-only**.
2. **Stream** — `load_dataset(..., streaming=True)`: "no download, no local copy". Reportedly "up to
   100× more efficient" after recent releases. Good for scanning the parquet metadata.
3. **Query over `hf://`** — Polars/DuckDB/pandas scan Hub parquet natively with filter and column
   pushdown. Docs benchmark: "~28 GB of Parquet in about four minutes on the default CPU flavor."
   For *large multi-file parquet scans* this is "typically several times faster than scanning through
   a mount"; for whole-file reads, mounting wins.

**Is there a local cache advantage when the job runs on HF infrastructure?**

Partially, and **not quantified anywhere** — treat the size of the win as UNKNOWN.

- Documented and real: **"Files read through a mount are cached on the Job's ephemeral disk**, so
  reading lazily (one file at a time) keeps the footprint small."
  So within a run you pay for each file once; a re-read is local.
- Documented and real, but for **buckets, not dataset repos**: **pre-warming**.
  > "Pre-warming caches files at edge locations near specific cloud providers and regions, so your
  > jobs read data locally instead of pulling it across regions."
  > — [storage-buckets#pre-warming-and-cdn](https://huggingface.co/docs/hub/storage-buckets)
- **UNKNOWN:** no doc states that Jobs compute is co-located with Hub storage, gives a
  same-region/zero-egress guarantee, or publishes a throughput figure for repo→job reads. Searched
  jobs-overview, jobs-configuration, jobs-large-datasets, storage-buckets, storage-buckets-access.
  The only measured throughput I found is for the *sandbox proxy* file API (~340 MiB/s down,
  ~441 MiB/s up), which is a different path.
- **Practical inference, not a citation:** if you will read all 10,448 decks repeatedly across many
  job runs, `hf buckets cp hf://datasets/Forceless/Zenodo10K/pptx hf://buckets/<you>/zenodo` is a
  **server-side, instant copy** for Xet-tracked files ("only the Xet content hashes are migrated …
  even very large files are copied instantly"), after which you can pre-warm the bucket near your
  compute. Caveat from the same page: "Server-side copy also requires the source and destination to be
  in the same storage region."

---

## 5. GitHub, for contrast

**Rejected on the 6-hour job cap.** [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)
gives GitHub-hosted runners **6 hours of execution time per job** (35 days per *workflow run*, 5 days
per job on self-hosted runners), with concurrent-job ceilings of 20 (Free), 40 (Pro), 60 (Team), 500
(Enterprise). Standard hosted Linux runners
([github-hosted-runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners))
are **4 vCPU / 16 GB RAM / 14 GB SSD** on public repos and only **2 vCPU / 8 GB / 14 GB** on private
repos — the 14 GB disk alone kills a 700 GB corpus, and 8 GB RAM caps us at ~12 concurrent 632 MB
agents before the OS. Larger runners exist "for organizations and enterprises" with "more RAM, CPU,
and disk space", but **the exact vCPU/RAM ladder was not on the page I fetched — UNKNOWN** (looked at
`actions/reference/runners/github-hosted-runners` and `actions/reference/limits`). Git LFS
([billing/git-lfs](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)) gives
**10 GiB storage + 10 GiB bandwidth free**, 250 GiB each on Team/Enterprise, now on metered billing
("data packs … have been removed and replaced with metered billing"). Ten GiB of LFS bandwidth against
a 700 GB corpus is not a discussion. The 6-hour cap would force the agent phase into dozens of chained
runs, and the 14 GB disk means re-fetching state from LFS every time — paying bandwidth we don't have.

---

## Recommendation

| | **Workload A — CPU triage of ~10,000 decks**<br>embarrassingly parallel, no API calls | **Workload B — agent phase**<br>days long, 632 MB/agent, needs outbound API |
|---|---|---|
| **HF Jobs** | ✅ **Best fit.** `cpu-performance` (32 vCPU / 256 GB / 1024 GB) at $1.90/hr, or fan out N × `cpu-upgrade` at $0.03/hr. Mount the dataset read-only (`-v hf://datasets/…`), write results to a bucket. At even 60 s/deck wall time, 10,000 decks ÷ 32-way ≈ 5 h ≈ **$10 total**. Nothing here is near a limit. | ✅ **Best fit, with one open risk.** Outbound internet confirmed; secrets encrypted server-side; arbitrary Docker image for Node + Claude CLI + LibreOffice; buckets give cross-run persistence for the resumable state. **Deciding limit: the max wall-clock, which is undocumented.** Default 30 min will silently kill you — always pass `--timeout`. |
| **HF Spaces** | ❌ **No.** CPU ceiling is 8 vCPU / 32 GB (CPU Upgrade) — 4× less than Jobs. 50 GB ephemeral disk. Wrong tool: it's a web-app host, and Gradio/Docker Spaces now require a paid plan to create at all. | ⚠️ **Possible, inferior.** Upgraded hardware "never goes to sleep", so days-long runs work; free `cpu-basic` is suspended after 48 h of *visitor* inactivity, which a headless worker will always trip. **Deciding limit: 8 vCPU / 32 GB CPU ceiling** (~50 agents by RAM but only 8 cores), plus a git-push deploy that restarts the Space on every commit, plus egress restricted to ports 80/443/8080. |
| **GitHub Actions** | ❌ **No. Deciding limit: 14 GB runner disk** (and 10 GiB free LFS bandwidth) against a ~700 GB corpus. | ❌ **No. Deciding limit: 6-hour hosted-job cap**, compounded by 8 GB RAM on private repos (~12 concurrent agents) and 14 GB disk forcing an LFS state re-fetch per chained run. |

**Recommended architecture (HF Jobs, both phases):**

1. One Docker image with Node + `@anthropic-ai/claude-code` + LibreOffice + the pipeline. Build cost
   is not billed.
2. Corpus in read-only via `-v hf://datasets/Forceless/Zenodo10K:/mnt/data` (lazy fetch, cached on
   ephemeral disk). If you'll re-read it across many runs, server-side-copy it into a bucket once.
3. Pipeline state and outputs in a read-write bucket (`-v hf://buckets/<ns>/pptx-state:/state`) —
   this is what makes the content-hashed resume work across job boundaries, and it sidesteps the
   undocumented commit rate limit entirely.
4. `ANTHROPIC_API_KEY` via `--secrets`, never `--env`.
5. **Chunk phase B into runs of ≤24 h** and let the resume logic chain them. 24 h is the value HF's
   own sandbox layer uses as a backstop; it costs nothing given full resumability, and it removes the
   single unresolved risk in this whole document.
6. Publish with `upload_folder` (not the deprecated `upload_large_folder`), sharded so no folder
   exceeds 10k entries.

**The one thing to verify empirically before committing:** submit a single
`hf jobs run --detach --timeout 3d cpu-basic sleep infinity`, poll `hf jobs inspect <id>`, and record
when it actually terminates. ~$0.72. That converts §1.1's UNKNOWN into a number, and it is the only
finding in this report that could still send weeks of work sideways.
