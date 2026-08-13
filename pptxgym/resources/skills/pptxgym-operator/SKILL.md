---
name: pptxgym-operator
description: Configure, launch, monitor, resume, verify, and publish CUA-Gym.pptx task-generation runs.
---

# Operate CUA-Gym.pptx

Read the installed `resources/guides/AGENTS.md` first. In a source checkout,
read the repository-root `AGENTS.md`, which is authoritative.

## Discover

Inspect the host, config, existing run snapshots, source, authentication
availability, disk, Office tools, and destinations without reading secret
values or making pipeline harness calls. Suppress stdout and stderr from auth
status commands and report only available/unavailable. Reuse valid settings
already present.

## Configure

Ask only missing choices, in small groups: operation, harness and model routes,
mode, executor, source, storage, publishing destinations, and concurrency.
Never ask for a token in chat. Use native login or a credential reference.
Run `pptxgym setup`, then non-billable `pptxgym doctor`; make a pipeline harness
smoke only after explicit approval. Distinguish it from the outer setup agent
when reporting whether calls occurred.

## Run

Collect deck count, mode, workers, run name, executor, monitoring, and publish
choice. Recommend a 3-10 deck pilot for an uncalibrated model or endpoint.
Show the effective configuration and exact command before launch. Monitor
authentication, 429s, platform exits, disk, parked decks, probes, and publish.

## Resume

Inspect the frozen `run.toml` and resume the same run. Preserve completed stage
state; do not silently select new decks or restart a completed tail.

## Publish

Repeat the exact rollout repository/checkout, assets dataset, registry and ID
series, task lists, push policy, and VM smoke choice before publication. Never
run two publishers against one registry. Finish by verifying both remote
destinations and running `pptxgym verify`.
