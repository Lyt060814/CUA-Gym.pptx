"""Versioned user configuration for the pptxgym control plane.

The stage commands deliberately remain flag-driven.  This module sits above
them: it turns one small TOML file into a reproducible run snapshot, while
keeping credentials out of both files.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

import tomli_w


SCHEMA_VERSION = 1
MODES = ("fast", "full", "focused")
EXECUTORS = ("local", "hf-jobs")
HARNESS_TYPES = ("claude", "codex")
ROUTE_NAMES = ("owner", "proposal", "recipe", "reconcile", "probe")


class ConfigError(ValueError):
    """A configuration is malformed or incomplete for the requested action."""


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "pptxgym" / "config.toml"


def default_credentials_path() -> Path:
    return default_config_path().with_name("credentials.toml")


def defaults(harness: str = "claude") -> dict[str, Any]:
    """Conservative, local-first defaults with no writable remote target."""
    if harness not in HARNESS_TYPES:
        raise ConfigError(f"unknown harness {harness!r}; choose claude or codex")
    if harness == "claude":
        models = {
            "owner": ("sonnet", "medium"),
            "proposal": ("sonnet", "high"),
            "recipe": ("sonnet", "medium"),
            "reconcile": ("opus", "high"),
            "probe": ("haiku", ""),
        }
        capacity, probe_capacity = 10, 4
        auth = "credential-store"
    else:
        models = {
            "owner": ("gpt-5.6-terra", "high"),
            "proposal": ("gpt-5.6-terra", "high"),
            "recipe": ("gpt-5.6-terra", "medium"),
            "reconcile": ("gpt-5.6-terra", "high"),
            "probe": ("gpt-5.6-terra", "medium"),
        }
        capacity, probe_capacity = 5, 2
        auth = "credential-store"
    routes = {
        name: {"harness": "main", "model": model, "effort": effort}
        for name, (model, effort) in models.items()
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "credentials_file": str(default_credentials_path()),
        "default_mode": "fast",
        "default_harness": "main",
        "execution": {
            "executor": "local",
            "work_root": "./runs",
            "detach": False,
            "timeout_minutes": 90,
            "max_turns": 260,
            "wps": "auto",
        },
        "concurrency": {
            "deck_workers": "auto",
            "probe_workers": "auto",
            "cpu_workers": "auto",
            "selection_workers": "auto",
        },
        "source": {
            "type": "zenodo10k",
            "repo": "Forceless/Zenodo10K",
            "path": "",
            "manifest": "",
            "min_score": 50.0,
            "scan": 0,
        },
        "storage": {
            "type": "local",
            "root": "./runs",
            "results_repo": "",
            "checkpoint_minutes": 10,
        },
        "publish": {
            "enabled": False,
            "rollout_repo": "",
            "rollout_checkout": "",
            "assets_repo": "",
            "assets_private": True,
            "registry": "evaluation_examples/task_assets/pptxgym-ids.json",
            "task_class_dir": "evaluation_examples/task_class",
            "task_assets_dir": "evaluation_examples/task_assets",
            "task_lists": [
                "evaluation_examples/test_pptxgym.json",
                "evaluation_examples/test_cua_scaling.json",
            ],
            "series": "110",
            "series_first": 1100001,
            "series_last": 1109999,
            "push": True,
            "aws_verify": False,
            "aws_osworld": "",
            "aws_uv": "",
            "aws_workers": 4,
            "hf_workers": 4,
            "aws_attempts": 3,
            "aws_instance_type": "",
            "aws_region": "",
        },
        "executors": {
            "hf-jobs": {
                "flavor": "cpu-performance",
                "timeout": "8h",
                "repo": "",
                "revision": "main",
                "osworld_repo": "",
            }
        },
        "harnesses": {
            "main": {
                "type": harness,
                "connection": "default",
                "max_concurrency": capacity,
                "probe_concurrency": probe_capacity,
            }
        },
        "connections": {
            "default": {
                "kind": "native",
                "base_url": "",
                "wire_api": "responses" if harness == "codex" else "anthropic",
                "auth": auth,
            }
        },
        "routes": routes,
        "credentials": {
            "huggingface": "hf-cache",
            "github": "gh-cli",
            "claude_oauth": "env:CLAUDE_CODE_OAUTH_TOKEN",
        },
    }


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(path: str | Path | None = None, *, require: bool = False) -> dict:
    path = Path(path).expanduser() if path else default_config_path()
    if not path.exists():
        if require:
            raise ConfigError(f"no configuration at {path}; run `pptxgym setup`")
        return defaults()
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    harness = raw.get("default_harness", "main")
    htype = ((raw.get("harnesses") or {}).get(harness) or {}).get("type", "claude")
    cfg = _merge(defaults(htype), raw)
    cfg["_config_path"] = str(path.resolve())
    validate(cfg)
    return cfg


def save(config: dict, path: str | Path | None = None) -> Path:
    path = Path(path).expanduser() if path else default_config_path()
    clean = {k: copy.deepcopy(v) for k, v in config.items()
             if not k.startswith("_")}
    validate(clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(clean))
    os.chmod(path, 0o600)
    return path


def load_credentials(path: str | Path | None = None) -> dict[str, str]:
    path = Path(path).expanduser() if path else default_credentials_path()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read credentials {path}: {error}") from error
    return {str(k): str(v) for k, v in (data.get("secrets") or {}).items()}


def save_credentials(secrets: dict[str, str],
                     path: str | Path | None = None) -> Path:
    path = Path(path).expanduser() if path else default_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({"secrets": secrets}))
    os.chmod(path, 0o600)
    return path


def resolve_reference(ref: str, *, credentials: dict[str, str] | None = None,
                      allow_missing: bool = False) -> str | None:
    """Resolve a credential reference without ever placing it in config."""
    ref = (ref or "").strip()
    credentials = credentials if credentials is not None else load_credentials()
    value = None
    if ref.startswith("env:"):
        value = os.environ.get(ref[4:])
    elif ref.startswith("secret:"):
        value = credentials.get(ref[7:])
    elif ref.startswith("file:"):
        path = Path(os.path.expandvars(os.path.expanduser(ref[5:])))
        if path.exists():
            value = path.read_text().strip()
    elif ref in ("", "none", "credential-store", "hf-cache", "gh-cli"):
        return None
    else:
        raise ConfigError(f"unsupported credential reference {ref!r}")
    if value:
        return value
    if allow_missing:
        return None
    raise ConfigError(f"credential {ref!r} is not available")


def validate(cfg: dict) -> None:
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            f"unsupported schema_version {cfg.get('schema_version')!r}; "
            f"this release reads {SCHEMA_VERSION}")
    if cfg.get("default_mode") not in MODES:
        raise ConfigError(f"default_mode must be one of {', '.join(MODES)}")
    executor = (cfg.get("execution") or {}).get("executor")
    if executor not in EXECUTORS:
        raise ConfigError(f"execution.executor must be one of {', '.join(EXECUTORS)}")
    if cfg["execution"].get("wps") not in ("auto", "on", "off"):
        raise ConfigError("execution.wps must be auto, on, or off")
    source = cfg.get("source") or {}
    if source.get("type") not in ("zenodo10k", "manifest", "local"):
        raise ConfigError("source.type must be zenodo10k, manifest, or local")
    if source.get("type") == "local" and not source.get("path"):
        raise ConfigError("source.path is required when source.type is local")
    if source.get("type") == "manifest" and not source.get("manifest"):
        raise ConfigError("source.manifest is required when source.type is manifest")
    storage = cfg.get("storage") or {}
    if storage.get("type") not in ("local", "hf"):
        raise ConfigError("storage.type must be local or hf")
    if executor == "hf-jobs" and not storage.get("results_repo"):
        raise ConfigError("storage.results_repo is required for hf-jobs")
    harnesses = cfg.get("harnesses") or {}
    connections = cfg.get("connections") or {}
    for name, harness in harnesses.items():
        if harness.get("type") not in HARNESS_TYPES:
            raise ConfigError(f"harnesses.{name}.type must be claude or codex")
        if harness.get("connection") not in connections:
            raise ConfigError(f"harnesses.{name} names missing connection "
                              f"{harness.get('connection')!r}")
    for name in ROUTE_NAMES:
        route = (cfg.get("routes") or {}).get(name)
        if not route:
            raise ConfigError(f"routes.{name} is required")
        if route.get("harness") not in harnesses:
            raise ConfigError(f"routes.{name} names missing harness "
                              f"{route.get('harness')!r}")
        if route.get("effort") not in ("", "minimal", "low", "medium",
                                       "high", "xhigh", "max"):
            raise ConfigError(f"routes.{name}.effort is invalid")
    for section, key in (("concurrency", "deck_workers"),
                         ("concurrency", "probe_workers"),
                         ("concurrency", "cpu_workers"),
                         ("concurrency", "selection_workers")):
        value = cfg[section][key]
        if value not in ("auto", "all") and not (
                isinstance(value, int) and not isinstance(value, bool) and value > 0):
            raise ConfigError(f"{section}.{key} must be auto, all, or a positive integer")
    for cname, connection in connections.items():
        if connection.get("kind", "native") not in ("native", "relay"):
            raise ConfigError(f"connections.{cname}.kind must be native or relay")
        auth = connection.get("auth", "")
        if auth and not (auth in ("credential-store", "none")
                         or re.match(r"^(env|file|secret):.+", auth)):
            raise ConfigError(f"connections.{cname}.auth must be credential-store, "
                              "env:NAME, file:PATH, secret:NAME, or none")
    publish = cfg.get("publish") or {}
    if publish.get("enabled") and not (
            (publish.get("rollout_repo") or publish.get("rollout_checkout")) and
            publish.get("assets_repo")):
        raise ConfigError(
            "publish.assets_repo and either rollout_repo or rollout_checkout "
            "are required when publishing")
    if publish.get("enabled") and source.get("type") == "local" and not \
            source.get("manifest"):
        raise ConfigError(
            "publishing source.type=local requires source.manifest for attribution")
    if publish.get("enabled") and executor == "hf-jobs" and not \
            publish.get("rollout_repo"):
        raise ConfigError("HF Jobs publishing requires publish.rollout_repo")
    lists = publish.get("task_lists") or []
    if not isinstance(lists, list) or not all(isinstance(x, str) and x for x in lists):
        raise ConfigError("publish.task_lists must be a list of repository paths")
    for key in ("aws_workers", "hf_workers", "aws_attempts"):
        if not isinstance(publish.get(key), int) or publish[key] < 1:
            raise ConfigError(f"publish.{key} must be a positive integer")


def public(config: dict) -> dict:
    """A run snapshot with credential values impossible to include."""
    out = copy.deepcopy(config)
    out.pop("_config_path", None)
    return out


def resolve_workers(config: dict, count: int, *, mode: str | None = None) -> dict:
    """Resolve independent API and CPU pools for one run."""
    mode = mode or config["default_mode"]
    conc = config["concurrency"]
    route = config["routes"]["owner"]
    harness = config["harnesses"][route["harness"]]

    def value(raw, automatic, ceiling=count):
        if raw == "all":
            return max(1, ceiling)
        if raw == "auto":
            return max(1, min(ceiling, automatic))
        return max(1, min(ceiling, int(raw)))

    deck_default = int(harness.get("max_concurrency") or
                       (10 if harness["type"] == "claude" else 5))
    probe_default = int(harness.get("probe_concurrency") or
                        (4 if harness["type"] == "claude" else 2))
    cpu_default = max(1, min(8, (os.cpu_count() or 4) // 4))
    selection_default = max(1, min(8, os.cpu_count() or 4))
    return {
        "deck_workers": value(conc["deck_workers"], deck_default),
        "probe_workers": value(conc["probe_workers"], probe_default),
        "cpu_workers": value(conc["cpu_workers"], cpu_default),
        "selection_workers": value(conc["selection_workers"], selection_default),
    }


def route_runtime(config: dict) -> dict:
    """Provider-neutral route description consumed by nested agent stages."""
    routes = {}
    for name, route in config["routes"].items():
        harness = config["harnesses"][route["harness"]]
        connection_name = route.get("connection") or harness["connection"]
        connection = config["connections"][connection_name]
        auth = connection.get("auth", "credential-store")
        auth_env = (auth[4:] if auth.startswith("env:") else
                    "PPTXGYM_AUTH_" + re.sub(r"[^A-Za-z0-9]", "_",
                                             connection_name).upper())
        routes[name] = {
            "harness": harness["type"],
            "model": route.get("model") or "",
            "effort": route.get("effort") or "",
            "connection": connection_name,
            "kind": connection.get("kind", "native"),
            "base_url": connection.get("base_url", ""),
            "wire_api": connection.get("wire_api", "responses"),
            "auth": auth,
            "auth_env": auth_env,
        }
    return routes
