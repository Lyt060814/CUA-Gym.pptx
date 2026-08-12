"""Configuration setup, credentials, and non-destructive environment checks."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

from ..orchestration import agent
from . import config


def _path(value: str, base: Path | None = None) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() or base is None else base / path


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd, *, cwd=None, env=None, capture=False, check=True):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True,
                          capture_output=capture, check=check)


def _read_hf_token() -> str | None:
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    path = Path.home() / ".cache" / "huggingface" / "token"
    return path.read_text().strip() if path.exists() else None


def _read_gh_token() -> str | None:
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    if not _which("gh"):
        return None
    got = _run(["gh", "auth", "token"], capture=True, check=False)
    return got.stdout.strip() if got.returncode == 0 else None


def _secret_env(cfg: dict, routes: dict, *, platform: bool = False,
                portable: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    credentials = config.load_credentials(cfg.get("credentials_file"))
    for route in routes.values():
        ref = route.get("auth", "")
        value = config.resolve_reference(ref, credentials=credentials,
                                         allow_missing=True)
        if not value:
            continue
        out[route["auth_env"]] = value
        if route["harness"] == "codex":
            out.setdefault("OPENAI_API_KEY", value)
            out.setdefault("RELAY_API_KEY", value)
        else:
            out.setdefault("ANTHROPIC_API_KEY", value)
    if platform or portable:
        if token := _read_hf_token():
            out["HF_TOKEN"] = token
        if token := _read_gh_token():
            out["GH_TOKEN"] = token
    if portable:
        claude = config.resolve_reference(
            cfg.get("credentials", {}).get("claude_oauth", ""),
            credentials=credentials, allow_missing=True)
        if claude:
            out["CLAUDE_CODE_OAUTH_TOKEN"] = claude
        needs_codex_store = any(
            route["harness"] == "codex" and route.get("kind") == "native"
            for route in routes.values())
        if needs_codex_store and "OPENAI_API_KEY" not in out:
            auth = Path.home() / ".codex" / "auth.json"
            if auth.exists():
                import base64
                out["CODEX_AUTH_B64"] = base64.b64encode(
                    auth.read_bytes()).decode()
        if cfg["publish"].get("aws_verify"):
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN"):
                if os.environ.get(key):
                    out[key] = os.environ[key]
    return out


def _setup(args) -> int:
    path = Path(args.config).expanduser() if args.config else \
        config.default_config_path()
    if path.exists() and not args.force:
        print(f"configuration already exists: {path}\n"
              "use --force to replace it or edit it directly")
        return 2
    harness = args.harness
    if not args.non_interactive:
        harness = input(
            f"Harness [claude/codex] ({harness}): ").strip().lower() or harness
    cfg = config.defaults(harness)
    cfg["credentials_file"] = str(path.with_name("credentials.toml"))
    cfg["execution"].update(executor=args.executor, work_root=args.work_root)
    cfg["storage"]["root"] = args.work_root
    cfg["source"].update(type=args.source_type)
    for key, value in (("repo", args.source_repo), ("path", args.source_path),
                       ("manifest", args.source_manifest)):
        if value:
            cfg["source"][key] = value
    connection = cfg["connections"]["default"]
    if args.base_url:
        connection.update(kind="relay", base_url=args.base_url)
    if args.results_repo:
        cfg["storage"].update(type="hf", results_repo=args.results_repo)
    if args.pipeline_repo:
        cfg["executors"]["hf-jobs"]["repo"] = args.pipeline_repo
    if args.revision:
        cfg["executors"]["hf-jobs"]["revision"] = args.revision
    if args.rollout_repo or args.assets_repo:
        cfg["publish"].update(
            enabled=bool(args.rollout_repo and args.assets_repo),
            rollout_repo=args.rollout_repo or "",
            assets_repo=args.assets_repo or "")
    credentials_path = path.with_name("credentials.toml")
    secrets = config.load_credentials(credentials_path)
    value = args.api_key
    if not value and not args.non_interactive and connection["kind"] == "relay":
        value = getpass.getpass(
            "Relay API key (empty to use an existing credential store): ")
    if value:
        secrets["main_api_key"] = value
        connection["auth"] = "secret:main_api_key"
        config.save_credentials(secrets, credentials_path)
    saved = config.save(cfg, path)
    print(f"wrote {saved}\n"
          f"secrets, when entered, live in {credentials_path}\n"
          "next: pptxgym doctor")
    return 0


def _check(label: str, ok: bool, detail: str) -> tuple[bool, str]:
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<18} {detail}")
    return ok, detail


def _doctor(args) -> int:
    cfg = config.load(args.config, require=True)
    routes = config.route_runtime(cfg)
    checks = [_check(
        "configuration", True,
        cfg.get("_config_path", str(args.config or config.default_config_path())))]
    for command in ("git", "soffice", "pdftoppm"):
        checks.append(_check(command, bool(_which(command)),
                             _which(command) or "not found"))
    for harness in sorted({route["harness"] for route in routes.values()}):
        checks.append(_check(f"{harness} CLI", bool(_which(harness)),
                             _which(harness) or "not found"))
    source = cfg["source"]
    if source["type"] == "local":
        path = _path(source["path"])
        count = len(list(path.rglob("*.pptx"))) if path.is_dir() else 0
        checks.append(_check("source", count > 0,
                             f"{path} ({count} deck(s))"))
    else:
        checks.append(_check(
            "source", source["type"] in ("zenodo10k", "manifest"),
            source.get("repo") or source.get("manifest") or source["type"]))
    storage = cfg["storage"]
    if cfg["execution"]["executor"] == "hf-jobs" or storage["type"] == "hf":
        checks.extend((
            _check("HF CLI", bool(_which("hf")),
                   _which("hf") or "install huggingface_hub"),
            _check("HF token", bool(_read_hf_token()),
                   "available" if _read_hf_token() else "login with `hf auth login`"),
            _check("results repo", bool(storage["results_repo"]),
                   storage["results_repo"] or "not configured"),
            _check("pipeline repo", bool(cfg["executors"]["hf-jobs"].get("repo")),
                   cfg["executors"]["hf-jobs"].get("repo") or
                   "set executors.hf-jobs.repo"),
        ))
    if cfg["execution"].get("wps") == "on":
        checks.append(_check("WPS", bool(_which("wpp")),
                             _which("wpp") or "required by execution.wps=on"))
    if cfg["publish"]["enabled"]:
        checks.append(_check(
            "GitHub token", bool(_read_gh_token()),
            "available" if _read_gh_token() else "login with `gh auth login`"))
        for key in ("rollout_repo", "assets_repo"):
            checks.append(_check(key.replace("_", " "), bool(cfg["publish"][key]),
                                 cfg["publish"][key] or "not configured"))
    secret_env = _secret_env(cfg, routes)
    credential_values = config.load_credentials(cfg.get("credentials_file"))
    for name, route in routes.items():
        ref = route.get("auth", "")
        native = ref == "credential-store" or (
            ref.startswith("file:") and Path(os.path.expanduser(ref[5:])).exists())
        present = native or ref in ("", "none") or bool(config.resolve_reference(
            ref, credentials=credential_values, allow_missing=True))
        checks.append(_check(
            f"route {name}", present,
            f"{route['harness']}/{route['model'] or 'default'}"
            + (f" via {route['base_url']}" if route["base_url"] else "")))
    usage = shutil.disk_usage(Path.cwd())
    checks.append(_check("free disk", usage.free >= 20 * 1024**3,
                         f"{usage.free / 1024**3:.1f} GiB"))
    if args.smoke and all(ok for ok, _ in checks):
        route = routes["owner"]
        spec = agent.AgentRun(
            "orchestrator", "Reply with the single word: ok", max_turns=1,
            timeout_min=3, engine=route["harness"], model=route["model"],
            effort=route["effort"] or None, connection=route, env=secret_env,
            allowed_tools=[])
        import asyncio
        result = asyncio.run(agent.run_agent(spec))
        ok = result.get("status") == "exited" and result.get("returncode") == 0
        checks.append(_check("model smoke", ok,
                             result.get("why") or result["status"]))
    return 0 if all(ok for ok, _ in checks) else 2
