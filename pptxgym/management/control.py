"""The user-facing control plane: configure once, then launch reproducibly."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from ..orchestration import agent, profiles
from . import config
from .setup import (_doctor, _path, _read_gh_token, _read_hf_token, _run,
                    _secret_env, _setup, _which)


RUN_FILE = "run.toml"
META_FILE = "run.json"
PID_FILE = "runner.pid"
LOG_FILE = "runner.log"


class ControlError(RuntimeError):
    pass


def _now_name(mode: str) -> str:
    return f"{mode}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}"


def _run_dir(cfg: dict, name: str) -> Path:
    root = _path(cfg["execution"].get("work_root") or cfg["storage"]["root"])
    return root / name


def _snapshot(cfg: dict, run_dir: Path, resolved: dict) -> Path:
    snap = config.public(cfg)
    snap["resolved"] = resolved
    path = run_dir / RUN_FILE
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(__import__("tomli_w").dumps(snap))
    return path


def _ledger(run_dir: Path, event: str, **detail) -> None:
    record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "event": event, **detail}
    with open(run_dir / "events.jsonl", "a") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _clone_rollout(cfg: dict, run_dir: Path, env: dict) -> Path:
    publish = cfg["publish"]
    checkout = publish.get("rollout_checkout")
    if checkout:
        path = _path(checkout)
        if not path.exists():
            raise ControlError(f"rollout checkout does not exist: {path}")
        return path
    remote = publish["rollout_repo"]
    if not remote:
        raise ControlError("publish.rollout_repo is not configured")
    path = run_dir / "rollout"
    if path.exists():
        return path
    url = remote if "://" in remote else f"https://github.com/{remote}.git"
    clone_env = {**os.environ, **env}
    askpass = None
    if env.get("GH_TOKEN") and url.startswith("https://github.com/"):
        askpass = run_dir / ".git-askpass"
        askpass.write_text(
            "#!/bin/sh\ncase \"$1\" in *Username*) echo x-access-token;; "
            "*) printf '%s\\n' \"$GH_TOKEN\";; esac\n")
        os.chmod(askpass, 0o700)
        clone_env.update(GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
    try:
        got = _run(["git", "clone", "--quiet", url, str(path)],
                   env=clone_env, check=False)
    finally:
        if askpass:
            askpass.unlink(missing_ok=True)
    if got.returncode:
        raise ControlError(f"could not clone rollout repository {remote}")
    return path


def _configure_publish_layout(cfg: dict):
    """Apply the rollout schema for this process only."""
    from ..delivery import publish
    settings = cfg["publish"]
    publish.configure_layout(
        task_class_dir=settings["task_class_dir"],
        task_assets_dir=settings["task_assets_dir"],
        registry=settings["registry"],
        task_lists=settings.get("task_lists") or (),
        series=str(settings.get("series", "110")),
        series_first=int(settings.get("series_first", 1100001)),
        series_last=int(settings.get("series_last", 1109999)))


def _publish_local(cfg: dict, run_dir: Path, env: dict) -> dict:
    from ..delivery import publish
    _configure_publish_layout(cfg)
    target = cfg["publish"]
    rollout = _clone_rollout(cfg, run_dir, env)
    if not (rollout / target["task_class_dir"]).exists():
        # A new sandbox target can start empty; production schemas are checked
        # by publish.rollout_problems before any push.
        (rollout / target["task_class_dir"]).mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="pptxgym-publish-", dir=run_dir))
    plan = publish.build(run_dir / "work", stage, rollout,
                         target["assets_repo"], run_id=run_dir.name)
    print(publish.render(plan))
    if plan["leaks"]:
        raise ControlError("publish leak guard failed")
    vm = None
    if target.get("aws_verify"):
        vm = publish.VmCheck(
            artefacts=stage / "aws",
            osworld=Path(target["aws_osworld"]).expanduser()
                    if target.get("aws_osworld") else None,
            uv=target.get("aws_uv") or None,
            aws_workers=int(target["aws_workers"]),
            hf_workers=int(target["hf_workers"]),
            attempts=int(target["aws_attempts"]),
            instance_type=target.get("aws_instance_type") or None,
            region=target.get("aws_region") or None,
            log=lambda line: print(f"    {line}", flush=True))
        vm.ready()
    result = publish.publish(plan, token=env.get("HF_TOKEN"),
                             push_git=bool(target.get("push", True)), vm=vm)
    (run_dir / "publish.json").write_text(json.dumps({
        "repo": target["assets_repo"], "rollout": target["rollout_repo"],
        "result": result,
    }, ensure_ascii=False, indent=2, default=str) + "\n")
    return result


def _publish_managed(args) -> int:
    run_dir = _path(args.run)
    cfg = _load_snapshot(run_dir)
    if args.no_git_push:
        cfg["publish"]["push"] = False
    if not ((cfg["publish"].get("rollout_repo") or
             cfg["publish"].get("rollout_checkout")) and
            cfg["publish"].get("assets_repo")):
        raise ControlError("this run has no rollout/assets publish target")
    cfg["publish"]["enabled"] = True
    if cfg["execution"]["executor"] == "hf-jobs":
        if args.no_git_push:
            raise ControlError("--no-git-push is only supported by the local executor")
        resolved = dict(cfg["resolved"])
        resolved.update(resume=True, publish_only=True)
        return _launch_hf(args, cfg, run_dir, resolved)
    routes = config.route_runtime(cfg)
    env = {**os.environ, **_secret_env(cfg, routes, platform=True)}
    _ledger(run_dir, "publish_started", retry=True)
    try:
        result = _publish_local(cfg, run_dir, env)
    except Exception as error:
        _ledger(run_dir, "publish_failed", error=f"{type(error).__name__}: {error}")
        raise
    _ledger(run_dir, "publish_finished", result=result)
    return 0


def _selected_local_decks(source: Path, count: int) -> Iterable[Path]:
    files = sorted(source.rglob("*.pptx"), key=lambda p: str(p.relative_to(source)))
    if len(files) < count:
        raise ControlError(f"local source contains {len(files)} deck(s), need {count}")
    return files[:count]


def _copy_local_source(source: Path, decks: Path, count: int) -> None:
    if not source.is_dir():
        raise ControlError(f"local source does not exist or is not a directory: {source}")
    mapping = {}
    for index, item in enumerate(_selected_local_decks(source, count), 1):
        target = decks / f"{index:04d}-{item.name}"
        shutil.copy2(item, target)
        mapping[target.name] = item.relative_to(source).as_posix()
    (decks / "source-map.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")


def _source_mapping(run_dir: Path) -> dict[str, str]:
    with contextlib.suppress(OSError, ValueError):
        return json.loads((run_dir / "decks" / "source-map.json").read_text())
    return {}


def _local_provenance(cfg: dict, run_dir: Path) -> None:
    """Attach local-source attribution after ingest has assigned deck IDs."""
    source = cfg["source"]
    manifest_name = source.get("manifest")
    if not manifest_name:
        if cfg["publish"].get("enabled"):
            raise ControlError(
                "publishing a local source requires source.manifest with "
                "name, license, source/url, and optional title/doi")
        return
    path = _path(manifest_name)
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ControlError(f"cannot read local source manifest {path}: {error}") from error
    if not isinstance(rows, list):
        raise ControlError("local source manifest must be a JSON list")
    by_name = {str(row.get("name")): row for row in rows if isinstance(row, dict)}
    mapping = _source_mapping(run_dir)
    for deck in sorted((run_dir / "work").glob("deck*")):
        try:
            origin = Path(json.loads((deck / "meta.json").read_text())["origin"])
        except (OSError, ValueError, KeyError):
            continue
        # Frozen local inputs are prefixed with a stable ordinal.
        original = mapping.get(origin.name) or (
            origin.name.split("-", 1)[1] if "-" in origin.name else origin.name)
        row = by_name.get(original)
        if not row or not row.get("license") or not (row.get("source") or row.get("url")):
            raise ControlError(f"local source manifest has no usable provenance for {original}")
        provenance = {
            "title": row.get("title") or Path(original).stem,
            "doi": row.get("doi") or "",
            "source": row.get("source") or original,
            "url": row.get("url") or "",
            "sha256": row.get("sha256") or "",
            "license": row["license"],
            "corpus": row.get("corpus") or "local",
        }
        (deck / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")


def _zenodo_provenance(run_dir: Path) -> None:
    manifest_path = run_dir / "decks" / f"{run_dir.name}-fetch.json"
    try:
        rows = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise ControlError(f"cannot read selected-deck manifest {manifest_path}: {error}") from error
    by_name = {row["name"]: row for row in rows}
    for deck in sorted((run_dir / "work").glob("deck*")):
        try:
            origin = Path(json.loads((deck / "meta.json").read_text())["origin"])
        except (OSError, ValueError, KeyError):
            continue
        row = by_name.get(origin.name)
        if not row:
            raise ControlError(f"selected-deck manifest has no row for {origin.name}")
        stem = origin.stem
        title = stem.split("-", 1)[1] if "-" in stem else stem
        provenance = {
            "title": title.replace("_", " "),
            "doi": row.get("record") or "",
            "source": row["name"],
            "url": row.get("url") or "",
            "sha256": row.get("sha256") or "",
            "license": row.get("license") or "",
            "corpus": "Zenodo10K",
        }
        if not provenance["license"]:
            raise ControlError(f"selected-deck manifest has no license for {origin.name}")
        (deck / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")


def _manifest_provenance(cfg: dict, run_dir: Path) -> None:
    path = _path(cfg["source"]["manifest"])
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ControlError(f"cannot read source manifest {path}: {error}") from error
    by_name = {row["name"]: row for row in rows}
    mapping = _source_mapping(run_dir)
    for deck in sorted((run_dir / "work").glob("deck*")):
        try:
            origin = Path(json.loads((deck / "meta.json").read_text())["origin"])
        except (OSError, ValueError, KeyError):
            continue
        original = mapping.get(origin.name, origin.name)
        row = by_name.get(original)
        if not row or not row.get("license"):
            raise ControlError(f"source manifest has no licensed row for {original}")
        provenance = {
            "title": row.get("title") or Path(original).stem.replace("_", " "),
            "doi": row.get("doi") or row.get("record") or "",
            "source": row.get("source") or row["name"],
            "url": row.get("url") or "",
            "sha256": row.get("sha256") or "",
            "license": row["license"],
            "corpus": row.get("corpus") or "manifest",
        }
        (deck / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")


def _worker(args) -> int:
    run_dir = _path(args.run)
    cfg = _load_snapshot(run_dir)
    resolved = dict(cfg["resolved"])
    resume = bool(args.resume)
    cmd, env = _prepare_local(cfg, run_dir, resolved["name"],
                              resolved["count"], resolved["mode"],
                              resolved["workers"], resume=resume)
    _ledger(run_dir, "pipeline_started", resume=resume, argv=cmd)
    code = _run(cmd, env=env, check=False).returncode
    _ledger(run_dir, "pipeline_finished", exit_code=code)
    publish_result = None
    if cfg["publish"]["enabled"]:
        try:
            if cfg["source"]["type"] == "local":
                _local_provenance(cfg, run_dir)
            elif cfg["source"]["type"] == "zenodo10k":
                _zenodo_provenance(run_dir)
            elif cfg["source"]["type"] == "manifest":
                _manifest_provenance(cfg, run_dir)
            publish_env = {**os.environ,
                           **_secret_env(cfg, config.route_runtime(cfg),
                                       platform=True)}
            publish_result = _publish_local(cfg, run_dir, publish_env)
            _ledger(run_dir, "publish_finished", result=publish_result)
        except Exception as error:  # evidence first; resume can retry publish
            _ledger(run_dir, "publish_failed", error=f"{type(error).__name__}: {error}")
            print(f"PUBLISH FAILED: {error}", file=sys.stderr)
            return 3
    meta = {"name": resolved["name"], "executor": "local",
            "status": "completed" if code == 0 else "partial",
            "exit_code": code, "publish": publish_result}
    (run_dir / META_FILE).write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return code


def _load_snapshot(run_dir: Path) -> dict:
    path = run_dir / RUN_FILE
    if not path.exists():
        raise ControlError(f"{run_dir} has no {RUN_FILE}; it is not a managed run")
    return config.load(path, require=True)


def _assignment(routes: dict) -> str:
    return ",".join(
        f"{stage}={routes[name]['model']}:{routes[name]['effort']}"
        for stage, name in (("propose", "proposal"), ("recipe", "recipe"),
                            ("reconcile", "reconcile"),
                            ("solvable", "probe"))
        if routes[name]["model"] and routes[name]["effort"])


def _prepare_local(cfg: dict, run_dir: Path, name: str, count: int,
                   mode: str, workers: dict, *, resume: bool) -> tuple[list[str], dict]:
    decks = run_dir / "decks"
    work = run_dir / "work"
    scan = run_dir / "scan"
    decks.mkdir(parents=True, exist_ok=True)
    source = cfg["source"]
    storage = cfg["storage"]
    routes = config.route_runtime(cfg)
    env = {**os.environ}
    for key in ("GH_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_AUTH_B64"):
        env.pop(key, None)
    env.update({**_secret_env(cfg, routes),
           agent.ROUTES_ENV: json.dumps(routes),
           profiles.PROFILE_ENV: "full" if mode == "full" else "fast",
           agent.PROBE_ENGINE_ENV: routes["probe"]["harness"],
           agent.PROBE_MODEL_ENV: routes["probe"]["model"],
           "PPTXGYM_PROBE_WORKERS": str(workers["probe_workers"]),
           "PPTXGYM_SOURCE_REPO": source.get("repo", "Forceless/Zenodo10K")})
    if routes["probe"]["effort"]:
        env[agent.PROBE_EFFORT_ENV] = routes["probe"]["effort"]
    if mode == "focused":
        env[profiles.FOCUS_ENV] = "advanced"
    if not resume:
        if source["type"] == "local":
            _copy_local_source(_path(source["path"]), decks, count)
            paths = [str(decks)]
        elif source["type"] == "manifest":
            manifest = json.loads(_path(source["manifest"]).read_text())
            if len(manifest) < count:
                raise ControlError(f"manifest contains {len(manifest)} decks, need {count}")
            import urllib.request
            import hashlib
            mapping = {}
            for index, row in enumerate(manifest[:count], 1):
                name = str(row.get("name") or "")
                if not name or Path(name).name != name or name in (".", ".."):
                    raise ControlError(f"manifest name must be a plain filename: {name!r}")
                target = decks / f"{index:04d}-{name}"
                urllib.request.urlretrieve(row["url"], target)
                if row.get("sha256") and hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
                    raise ControlError(f"checksum mismatch for {row['name']}")
                mapping[target.name] = name
            (decks / "source-map.json").write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
            paths = [str(decks)]
        else:
            from ..delivery import corpus
            pool = (storage["results_repo"] if storage["type"] == "hf"
                    else "file:" + str(_path(storage["root"]).resolve()))
            corpus.autoselect(
                count, decks, name, repo=pool,
                scan=source.get("scan") or None,
                min_score=float(source.get("min_score", 50.0)),
                workers=workers["selection_workers"], scratch=scan,
                focus="advanced" if mode == "focused" else None)
            paths = [str(decks)]
    else:
        paths = []
    owner = routes["owner"]
    cmd = [sys.executable, "-m", "pptxgym.orchestration.foreman", *paths,
           "--work", str(work), "--workers", str(workers["deck_workers"]),
           "--cpu-workers", str(workers["cpu_workers"]),
           "--max-turns", str(cfg["execution"]["max_turns"]),
           "--timeout", str(cfg["execution"]["timeout_minutes"]),
           "--profile", "full" if mode == "full" else "fast",
           "--engine-split", f"{owner['harness']}={count}"]
    if owner["harness"] == "codex":
        cmd += ["--codex-model", owner["model"],
                "--codex-workers", str(workers["deck_workers"])]
    else:
        cmd += ["--model", owner["model"], "--effort", owner["effort"]]
    assign = _assignment(routes)
    if assign:
        cmd += ["--assign", assign]
    if cfg["execution"].get("wps") == "on" and not _which("wpp"):
        raise ControlError("execution.wps=on but `wpp` is not installed")
    if cfg["execution"].get("wps") == "off" or not _which("wpp"):
        cmd += ["--no-wps"]
    return cmd, env


def _launch_local(args, cfg: dict, run_dir: Path, resolved: dict,
                  *, resume: bool = False) -> int:
    cmd = [sys.executable, "-m", "pptxgym.commands.cli", "_worker",
           "--run", str(run_dir)]
    if resume:
        cmd.append("--resume")
    print("executor  local")
    print("command   " + shlex.join(cmd))
    print(f"work      {run_dir}")
    if args.dry_run:
        return 0
    log = open(run_dir / LOG_FILE, "a", buffering=1)
    detach = args.detach if args.detach is not None else cfg["execution"]["detach"]
    if detach:
        proc = subprocess.Popen(cmd, stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
        (run_dir / PID_FILE).write_text(str(proc.pid) + "\n")
        print(f"started pid {proc.pid}; logs: {run_dir / LOG_FILE}")
        return 0
    return _run(cmd, check=False).returncode


def _hf_command(cfg: dict, resolved: dict) -> tuple[list[str], dict]:
    hf = cfg["executors"]["hf-jobs"]
    storage = cfg["storage"]
    routes = config.route_runtime(cfg)
    if not storage["results_repo"]:
        raise ControlError("hf-jobs requires storage.results_repo")
    if cfg["source"]["type"] == "local":
        raise ControlError("hf-jobs cannot access source.type=local; use a manifest or Zenodo")
    owner, probe = routes["owner"], routes["probe"]
    env = {
        "PPTXGYM_RUN": resolved["name"],
        "PPTXGYM_SELECT": str(resolved["count"]),
        "PPTXGYM_RESULTS_REPO": storage["results_repo"],
        "PPTXGYM_PROFILE": "full" if resolved["mode"] == "full" else "fast",
        "PPTXGYM_ENGINE_SPLIT": f"{owner['harness']}={resolved['count']}",
        "PPTXGYM_WORKERS": str(resolved["workers"]["deck_workers"]),
        "PPTXGYM_CPU_WORKERS": str(resolved["workers"]["cpu_workers"]),
        "PPTXGYM_SELECTION_WORKERS": str(resolved["workers"]["selection_workers"]),
        "PPTXGYM_CODEX_WORKERS": str(resolved["workers"]["deck_workers"]),
        "PPTXGYM_PROBE_WORKERS": str(resolved["workers"]["probe_workers"]),
        "PPTXGYM_PROBE_ENGINE": probe["harness"],
        "PPTXGYM_PROBE_MODEL": probe["model"],
        "PPTXGYM_PROBE_EFFORT": probe["effort"],
        "PPTXGYM_CODEX_MODEL": owner["model"] if owner["harness"] == "codex" else "",
        "PPTXGYM_MODEL": owner["model"] if owner["harness"] == "claude" else "",
        "PPTXGYM_EFFORT": owner["effort"],
        "PPTXGYM_TIMEOUT_MINUTES": str(cfg["execution"]["timeout_minutes"]),
        "PPTXGYM_MAX_TURNS": str(cfg["execution"]["max_turns"]),
        "PPTXGYM_FOCUS": "advanced" if resolved["mode"] == "focused" else "",
        agent.ROUTES_ENV: json.dumps(routes, separators=(",", ":")),
        "PPTXGYM_SOURCE_REPO": cfg["source"].get("repo", "Forceless/Zenodo10K"),
    }
    needs_claude_oauth = any(
        route["harness"] == "claude" and route.get("kind") == "native"
        for route in routes.values())
    env["PPTXGYM_NEEDS_CLAUDE_OAUTH"] = "1" if needs_claude_oauth else "0"
    if cfg["source"]["type"] == "manifest" and not resolved.get("resume"):
        env["PPTXGYM_FETCH"] = resolved.get("manifest_repo_path") or \
            cfg["source"]["manifest"]
    if not cfg["publish"]["enabled"]:
        env["PPTXGYM_NO_PUBLISH"] = "1"
    else:
        env["PPTXGYM_ROLLOUT_REPO"] = cfg["publish"]["rollout_repo"]
        env["PPTXGYM_ASSETS_REPO"] = cfg["publish"]["assets_repo"]
        env["PPTXGYM_TASK_CLASS_DIR"] = cfg["publish"]["task_class_dir"]
        env["PPTXGYM_TASK_ASSETS_DIR"] = cfg["publish"]["task_assets_dir"]
        env["PPTXGYM_REGISTRY"] = cfg["publish"]["registry"]
        env["PPTXGYM_TASK_LISTS_JSON"] = json.dumps(
            cfg["publish"].get("task_lists") or [], separators=(",", ":"))
        env["PPTXGYM_SERIES"] = str(cfg["publish"]["series"])
        env["PPTXGYM_SERIES_FIRST"] = str(cfg["publish"]["series_first"])
        env["PPTXGYM_SERIES_LAST"] = str(cfg["publish"]["series_last"])
        if cfg["publish"].get("aws_verify"):
            env["PPTXGYM_AWS_VERIFY"] = "1"
            env["PPTXGYM_AWS_WORKERS"] = str(cfg["publish"]["aws_workers"])
            env["PPTXGYM_HF_WORKERS"] = str(cfg["publish"]["hf_workers"])
            env["PPTXGYM_AWS_ATTEMPTS"] = str(cfg["publish"]["aws_attempts"])
            env["PPTXGYM_AWS_INSTANCE_TYPE"] = \
                cfg["publish"].get("aws_instance_type") or ""
            env["PPTXGYM_AWS_REGION"] = cfg["publish"].get("aws_region") or ""
            osworld_repo = cfg["executors"]["hf-jobs"].get("osworld_repo")
            if not osworld_repo:
                raise ControlError(
                    "HF AWS verification requires executors.hf-jobs.osworld_repo")
            env["PPTXGYM_OSWORLD_REPO"] = osworld_repo
    codex_routes = [route for route in routes.values()
                    if route["harness"] == "codex" and route.get("base_url")]
    if codex_routes:
        env["PPTXGYM_CODEX_BASE_URL"] = codex_routes[0]["base_url"]
    if resolved.get("resume"):
        env["PPTXGYM_RESUME_FROM"] = resolved["name"]
        env["PPTXGYM_FETCH"] = f"corpus/{resolved['name']}/{resolved['name']}-fetch.json"
    if resolved.get("publish_only"):
        env["PPTXGYM_PUBLISH_ONLY"] = "1"
    repo = hf.get("repo")
    if not repo:
        raise ControlError("executors.hf-jobs.repo must name the pipeline GitHub repo")
    env["PPTXGYM_REPO"] = repo
    env["PPTXGYM_COMMIT"] = hf.get("revision", "main")
    secret_env = _secret_env(cfg, routes, portable=True)
    secrets = sorted(secret_env)
    cmd = ["hf", "jobs", "run", "--detach", "--flavor", hf["flavor"],
           "--timeout", hf["timeout"]]
    for key in secrets:
        cmd += ["--secrets", key]
    for key, value in env.items():
        if value:
            cmd += ["-e", f"{key}={value}"]
    crun = Path(__file__).resolve().parents[1] / "resources" / "executors" / "crun.sh"
    if not crun.exists():
        crun = Path(__file__).resolve().parents[2] / "image" / "crun.sh"
    if not crun.exists():
        raise ControlError("HF executor requires image/crun.sh from a source checkout")
    cmd += ["ubuntu:22.04", "bash", "-c", crun.read_text()]
    return cmd, secret_env


def _launch_hf(args, cfg: dict, run_dir: Path, resolved: dict) -> int:
    manifest = cfg["source"].get("manifest")
    if cfg["source"]["type"] == "manifest" and manifest and not \
            resolved.get("resume"):
        local = _path(manifest)
        if local.exists():
            try:
                rows = json.loads(local.read_text())
            except (OSError, ValueError) as error:
                raise ControlError(f"cannot read source manifest {local}: {error}") from error
            if not isinstance(rows, list) or len(rows) < resolved["count"]:
                raise ControlError(
                    f"source manifest must contain at least {resolved['count']} rows")
            for row in rows[:resolved["count"]]:
                if not all(row.get(key) for key in ("name", "url", "sha256")):
                    raise ControlError("each source manifest row needs name, url, and sha256")
            remote = f"corpus/{resolved['name']}/{resolved['name']}-fetch.json"
            resolved["manifest_repo_path"] = remote
            if not args.dry_run:
                from huggingface_hub import HfApi
                token = _read_hf_token()
                if not token:
                    raise ControlError("uploading a local manifest needs an HF token")
                HfApi(token=token).upload_file(
                    path_or_fileobj=str(local), repo_id=cfg["storage"]["results_repo"],
                    repo_type="dataset", path_in_repo=remote)
                print(f"manifest  {local} -> {remote}")
    cmd, secrets = _hf_command(cfg, resolved)
    print("executor  hf-jobs")
    print(f"run       {resolved['name']}")
    print(f"workers   {resolved['workers']['deck_workers']} deck / "
          f"{resolved['workers']['probe_workers']} probe")
    if args.dry_run:
        print("command   " + shlex.join(cmd[:12]) + " ...")
        return 0
    env = {**os.environ, **secrets}
    got = _run(cmd, env=env, capture=True, check=False)
    sys.stdout.write(got.stdout)
    sys.stderr.write(got.stderr)
    if got.returncode == 0:
        match = __import__("re").search(r"ID:\s*([0-9a-f]+)", got.stdout)
        old = {}
        with contextlib.suppress(OSError, ValueError):
            old = json.loads((run_dir / META_FILE).read_text())
        job_id = match.group(1) if match else ""
        jobs = list(old.get("jobs") or [])
        jobs.append({"id": job_id,
                     "kind": "publish" if resolved.get("publish_only") else
                             ("resume" if resolved.get("resume") else "run"),
                     "at": dt.datetime.now(dt.timezone.utc).isoformat()})
        meta = {"name": resolved["name"], "executor": "hf-jobs",
                "job_id": job_id, "jobs": jobs, "status": "submitted"}
        (run_dir / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")
    return got.returncode


def _launch(args, *, resume: bool = False) -> int:
    if resume:
        run_dir = _path(args.run)
        cfg = _load_snapshot(run_dir)
        resolved = dict(cfg["resolved"])
        resolved["resume"] = True
    else:
        cfg = config.load(args.config, require=True)
        mode = args.mode or cfg["default_mode"]
        name = args.name or _now_name(mode)
        count = args.count
        if count < 1:
            raise ControlError("--count must be positive")
        for key, value in (("deck_workers", args.workers),
                           ("probe_workers", args.probe_workers),
                           ("cpu_workers", args.cpu_workers),
                           ("selection_workers", args.selection_workers)):
            if value is not None:
                cfg["concurrency"][key] = value
        if args.executor:
            cfg["execution"]["executor"] = args.executor
        if args.publish:
            cfg["publish"]["enabled"] = True
        config.validate(cfg)
        workers = config.resolve_workers(cfg, count, mode=mode)
        resolved = {"name": name, "count": count, "mode": mode,
                    "workers": workers, "resume": False}
        if cfg["execution"]["executor"] == "hf-jobs" and not \
                cfg["executors"]["hf-jobs"].get("repo"):
            raise ControlError("hf-jobs requires executors.hf-jobs.repo")
        run_dir = _run_dir(cfg, name)
        if run_dir.exists() and any(run_dir.iterdir()):
            raise ControlError(f"run {name!r} already exists at {run_dir}")
        _snapshot(cfg, run_dir, resolved)
    print(f"mode      {resolved['mode']}")
    print(f"decks     {resolved['count']}")
    print(f"workers   {resolved['workers']['deck_workers']} deck / "
          f"{resolved['workers']['probe_workers']} probe / "
          f"{resolved['workers']['cpu_workers']} cpu")
    if cfg["execution"]["executor"] == "local":
        return _launch_local(args, cfg, run_dir, resolved, resume=resume)
    return _launch_hf(args, cfg, run_dir, resolved)


def _managed_status(args) -> int:
    run_dir = _path(args.run)
    cfg = _load_snapshot(run_dir)
    meta = {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((run_dir / META_FILE).read_text())
    if cfg["execution"]["executor"] == "hf-jobs":
        job = meta.get("job_id")
        if not job:
            print("run was prepared but no HF job id was recorded")
            return 1
        return _run(["hf", "jobs", "inspect", job], check=False).returncode
    work = run_dir / "work"
    if not work.exists():
        print("run has not produced a work directory yet")
        return 1
    return _run([sys.executable, "-m", "pptxgym.commands.cli", "--work", str(work),
                 "status", "--all"], check=False).returncode


def _logs(args) -> int:
    run_dir = _path(args.run)
    cfg = _load_snapshot(run_dir)
    if cfg["execution"]["executor"] == "hf-jobs":
        meta = json.loads((run_dir / META_FILE).read_text())
        cmd = ["hf", "jobs", "logs"]
        if args.follow:
            cmd.append("--follow")
        cmd.append(meta["job_id"])
        return _run(cmd, check=False).returncode
    path = run_dir / LOG_FILE
    if not path.exists():
        print(f"no log at {path}")
        return 1
    cmd = ["tail", "-f" if args.follow else "-n", "200", str(path)]
    if args.follow:
        cmd = ["tail", "-f", str(path)]
    return _run(cmd, check=False).returncode


def _harness(args) -> int:
    cfg = config.load(args.config, require=True)
    routes = config.route_runtime(cfg)
    if args.action == "list":
        print(f"{'route':10s} {'harness':8s} {'model':24s} {'effort':8s} connection")
        for name in config.ROUTE_NAMES:
            route = routes[name]
            endpoint = route.get("base_url") or route.get("kind", "native")
            print(f"{name:10s} {route['harness']:8s} {route['model']:24s} "
                  f"{(route['effort'] or '-'):8s} {route['connection']} ({endpoint})")
        return 0
    names = [args.name] if args.name else sorted(cfg["harnesses"])
    secrets = _secret_env(cfg, routes)
    failed = False
    import asyncio
    for name in names:
        if name not in cfg["harnesses"]:
            raise ControlError(f"unknown harness {name!r}")
        route_name = next((key for key, value in cfg["routes"].items()
                           if value["harness"] == name), None)
        if not route_name:
            print(f"SKIP  {name}: no route uses this harness")
            continue
        route = routes[route_name]
        spec = agent.AgentRun(
            "orchestrator", "Reply with the single word: ok", max_turns=1,
            timeout_min=3, engine=route["harness"], model=route["model"],
            effort=route["effort"] or None, connection=route, env=secrets,
            allowed_tools=[])
        result = asyncio.run(agent.run_agent(spec))
        ok = result.get("status") == "exited" and result.get("returncode") == 0
        print(f"{'PASS' if ok else 'FAIL'}  {name}: "
              f"{route['harness']}/{route['model']} ({result.get('status')})")
        failed |= not ok
    return 2 if failed else 0


def _verify(args) -> int:
    run_dir = _path(args.run)
    cfg = _load_snapshot(run_dir)
    work = run_dir / "work"
    problems = []
    if not work.exists():
        problems.append(f"missing local work tree {work}")
    else:
        from ..core import pipeline as pl
        decks = pl.decks_in(work)
        shipped = 0
        for deck in decks:
            try:
                foreman = json.loads((deck.root / "foreman.json").read_text())
            except (OSError, ValueError):
                problems.append(f"{deck.id}: no valid foreman.json")
                continue
            if foreman.get("outcome") == "shipped":
                shipped += 1
                for file in ("plan.json", "task.json", "package.json"):
                    if not (deck.root / file).exists():
                        problems.append(f"{deck.id}: shipped without {file}")
        print(f"decks      {len(decks)}\nshipped    {shipped}")
    published = run_dir / "publish.json"
    if published.exists():
        try:
            record = json.loads(published.read_text())
            result = record.get("result") or {}
            uploaded = int(result.get("uploaded") or 0)
            written = result.get("written") or []
            print(f"published  {uploaded}\ngit files  {len(written)}")
            if not result.get("verified"):
                problems.append(
                    "publish record does not confirm material fetch verification")
        except (OSError, ValueError, TypeError) as error:
            problems.append(f"invalid publish.json: {error}")
    for problem in problems:
        print(f"FAIL       {problem}")
    return 2 if problems else 0


def add_commands(sub) -> None:
    p = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p.add_argument("--run", required=True)
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=_worker)

    p = sub.add_parser("setup", help="create the user configuration")
    p.add_argument("--config", default=None)
    p.add_argument("--harness", choices=config.HARNESS_TYPES, default="claude")
    p.add_argument("--executor", choices=config.EXECUTORS, default="local")
    p.add_argument("--work-root", default="./runs")
    p.add_argument("--source-type", choices=("zenodo10k", "manifest", "local"),
                   default="zenodo10k")
    p.add_argument("--source-repo", default="")
    p.add_argument("--source-path", default="")
    p.add_argument("--source-manifest", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--results-repo", default="")
    p.add_argument("--pipeline-repo", default="",
                   help="GitHub owner/repo containing pptxgym for HF Jobs")
    p.add_argument("--revision", default="main")
    p.add_argument("--rollout-repo", default="")
    p.add_argument("--assets-repo", default="")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_setup)

    p = sub.add_parser("doctor", help="validate dependencies, credentials and targets")
    p.add_argument("--config", default=None)
    p.add_argument("--smoke", action="store_true",
                   help="also make one real, potentially billable model call")
    p.set_defaults(func=_doctor)

    p = sub.add_parser("harness", help="list or test configured model harnesses")
    p.add_argument("action", choices=("list", "test"))
    p.add_argument("--name", default=None,
                   help="with test: test only this harness name")
    p.add_argument("--config", default=None)
    p.set_defaults(func=_harness)

    p = sub.add_parser("run", help="launch a managed fast, full or focused batch")
    p.add_argument("--config", default=None)
    p.add_argument("--mode", choices=config.MODES, default=None)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--executor", choices=config.EXECUTORS, default=None)
    p.add_argument("--workers", default=None, type=_worker_value)
    p.add_argument("--probe-workers", default=None, type=_worker_value)
    p.add_argument("--cpu-workers", default=None, type=_worker_value)
    p.add_argument("--selection-workers", default=None, type=_worker_value)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--detach", action=argparse.BooleanOptionalAction, default=None)
    p.set_defaults(func=lambda a: _launch(a, resume=False))

    p = sub.add_parser("resume", help="resume a managed run from its frozen config")
    p.add_argument("run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--detach", action=argparse.BooleanOptionalAction, default=None)
    p.set_defaults(func=lambda a: _launch(a, resume=True))

    p = sub.add_parser("run-status", help="status of a managed local or HF run")
    p.add_argument("run")
    p.set_defaults(func=_managed_status)

    p = sub.add_parser("logs", help="read a managed run's local or HF logs")
    p.add_argument("run")
    p.add_argument("--follow", action="store_true")
    p.set_defaults(func=_logs)

    p = sub.add_parser("verify", help="audit a managed run's local artefacts")
    p.add_argument("run")
    p.set_defaults(func=_verify)

    p = sub.add_parser("publish", help="publish or retry a completed managed run")
    p.add_argument("run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-git-push", action="store_true",
                   help="upload and commit locally without pushing the rollout repository")
    p.set_defaults(func=_publish_managed)


def _worker_value(value: str):
    if value in ("auto", "all"):
        return value
    try:
        got = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use auto, all, or a positive integer") from error
    if got < 1:
        raise argparse.ArgumentTypeError("worker count must be positive")
    return got
