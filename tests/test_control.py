"""The product surface: config, routing, setup, and launch planning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pptxgym.management import config, control, setup
from pptxgym.orchestration import agent


def test_defaults_are_local_safe_and_provider_neutral():
    claude = config.defaults("claude")
    codex = config.defaults("codex")
    assert claude["execution"]["executor"] == "local"
    assert claude["publish"]["enabled"] is False
    assert claude["storage"]["results_repo"] == ""
    assert claude["routes"]["owner"]["harness"] == "main"
    assert codex["harnesses"]["main"]["type"] == "codex"
    assert codex["connections"]["default"]["auth"] == "credential-store"


def test_auto_workers_respect_the_harness_capacity_and_deck_count(monkeypatch):
    monkeypatch.setattr(config.os, "cpu_count", lambda: 16)
    cfg = config.defaults("codex")
    assert config.resolve_workers(cfg, 3) == {
        "deck_workers": 3, "probe_workers": 2,
        "cpu_workers": 3, "selection_workers": 3,
    }
    got = config.resolve_workers(cfg, 100)
    assert got["deck_workers"] == 5
    assert got["probe_workers"] == 2
    cfg["concurrency"]["deck_workers"] = "all"
    assert config.resolve_workers(cfg, 100)["deck_workers"] == 100


def test_config_round_trip_and_cli_override(tmp_path):
    path = tmp_path / "config.toml"
    cfg = config.defaults("codex")
    cfg["concurrency"]["deck_workers"] = 7
    config.save(cfg, path)
    got = config.load(path, require=True)
    assert got["concurrency"]["deck_workers"] == 7
    assert got["_config_path"] == str(path.resolve())


def test_credentials_live_in_a_separate_mode_600_file(tmp_path):
    path = tmp_path / "credentials.toml"
    config.save_credentials({"relay": "secret-value"}, path)
    assert config.load_credentials(path) == {"relay": "secret-value"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert "secret-value" not in config.save(
        config.defaults("codex"), tmp_path / "config.toml").read_text()


def test_route_table_can_mix_harnesses():
    cfg = config.defaults("codex")
    cfg["harnesses"]["witness"] = {
        "type": "claude", "connection": "claude-native",
        "max_concurrency": 4, "probe_concurrency": 2,
    }
    cfg["connections"]["claude-native"] = {
        "kind": "native", "base_url": "", "wire_api": "anthropic",
        "auth": "credential-store",
    }
    cfg["routes"]["probe"].update(harness="witness", model="haiku", effort="")
    config.validate(cfg)
    routes = config.route_runtime(cfg)
    assert routes["owner"]["harness"] == "codex"
    assert routes["probe"]["harness"] == "claude"


def test_each_connection_gets_an_independent_secret_variable(tmp_path):
    cfg = config.defaults("codex")
    cfg["credentials_file"] = str(tmp_path / "credentials.toml")
    cfg["connections"]["default"].update(kind="relay", auth="secret:owner")
    cfg["connections"]["probe"] = {
        "kind": "relay", "base_url": "https://probe.example",
        "wire_api": "responses", "auth": "secret:probe"}
    cfg["harnesses"]["witness"] = {
        "type": "codex", "connection": "probe", "max_concurrency": 2,
        "probe_concurrency": 1}
    cfg["routes"]["probe"]["harness"] = "witness"
    config.save_credentials({"owner": "a", "probe": "b"},
                            tmp_path / "credentials.toml")
    routes = config.route_runtime(cfg)
    env = control._secret_env(cfg, routes)
    assert env[routes["owner"]["auth_env"]] == "a"
    assert env[routes["probe"]["auth_env"]] == "b"
    assert routes["owner"]["auth_env"] != routes["probe"]["auth_env"]


def test_configured_route_reaches_agent_specs(monkeypatch):
    routes = {"owner": {"harness": "codex", "model": "terra",
                         "effort": "high", "base_url": ""}}
    monkeypatch.setenv(agent.ROUTES_ENV, json.dumps(routes))
    spec = agent.AgentRun("orchestrator", "x", engine="claude")
    agent.apply_route(spec, "owner", owner=True)
    assert (spec.engine, spec.model, spec.effort) == ("codex", "terra", "high")


def test_setup_writes_no_api_key_into_public_config(tmp_path):
    path = tmp_path / "config.toml"
    args = type("Args", (), {
        "config": str(path), "force": False, "harness": "codex",
        "executor": "local", "results_repo": "", "rollout_repo": "",
        "assets_repo": "", "base_url": "https://relay.example",
        "api_key": "very-secret", "non_interactive": True,
        "work_root": str(tmp_path / "runs"), "source_type": "zenodo10k",
        "source_repo": "", "source_path": "", "source_manifest": "",
        "pipeline_repo": "", "revision": "main",
    })()
    assert control._setup(args) == 0
    assert "very-secret" not in path.read_text()
    credentials = path.with_name("credentials.toml")
    assert "very-secret" in credentials.read_text()
    assert config.load(path)["connections"]["default"]["auth"] == \
        "secret:main_api_key"


def test_local_dry_run_freezes_resolved_config(tmp_path, monkeypatch):
    cfg = config.defaults("claude")
    cfg["source"].update(type="local", path=str(tmp_path / "decks"))
    cfg["execution"]["work_root"] = str(tmp_path / "runs")
    cpath = tmp_path / "config.toml"
    config.save(cfg, cpath)
    (tmp_path / "decks").mkdir()
    args = type("Args", (), {
        "config": str(cpath), "mode": "fast", "count": 2, "name": "trial",
        "executor": None, "workers": None, "probe_workers": None,
        "cpu_workers": None, "selection_workers": None, "publish": False,
        "dry_run": True, "detach": None,
    })()
    assert control._launch(args) == 0
    snap = config.load(tmp_path / "runs" / "trial" / control.RUN_FILE)
    assert snap["resolved"]["count"] == 2
    assert snap["resolved"]["workers"]["deck_workers"] == 2


def test_setup_accepts_a_credential_reference_without_copying_the_secret(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_TEST_KEY", "not-written-to-config")
    cpath = tmp_path / "config.toml"
    args = type("Args", (), {
        "config": str(cpath), "harness": "codex", "executor": "local",
        "work_root": str(tmp_path / "runs"), "source_type": "local",
        "source_repo": "", "source_path": str(tmp_path / "decks"),
        "source_manifest": "", "base_url": "https://relay.example/v1",
        "api_key": "", "api_key_ref": "env:RELAY_TEST_KEY",
        "results_repo": "", "pipeline_repo": "", "revision": "main",
        "rollout_repo": "", "assets_repo": "", "non_interactive": True,
        "force": False,
    })()
    assert setup._setup(args) == 0
    text = cpath.read_text()
    assert "env:RELAY_TEST_KEY" in text
    assert "not-written-to-config" not in text
    assert not cpath.with_name("credentials.toml").exists()


def test_invalid_worker_setting_fails_before_a_run():
    cfg = config.defaults()
    cfg["concurrency"]["deck_workers"] = 0
    with pytest.raises(config.ConfigError):
        config.validate(cfg)


def test_package_resources_exist_outside_dot_claude():
    assert agent.agent_manual("orchestrator")
    assert Path(agent.skill_path("ppt-task-proposal")).is_file()
    assert "pptxgym/resources" in agent.skill_path("ppt-task-proposal")
    assert (agent.RESOURCE_ROOT / "executors" / "crun.sh").is_file()


def test_packaged_hf_executor_stays_in_sync_with_source_script():
    root = Path(__file__).resolve().parents[1]
    assert (root / "image" / "crun.sh").read_bytes() == \
        (agent.RESOURCE_ROOT / "executors" / "crun.sh").read_bytes()


def test_local_source_selection_is_frozen_to_exact_count(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("c.pptx", "a.pptx", "b.pptx"):
        (source / name).write_bytes(name.encode())
    dest = tmp_path / "decks"
    dest.mkdir()
    control._copy_local_source(source, dest, 2)
    assert [p.name for p in sorted(dest.iterdir())] == [
        "0001-a.pptx", "0002-b.pptx", "source-map.json"]
    assert json.loads((dest / "source-map.json").read_text()) == {
        "0001-a.pptx": "a.pptx", "0002-b.pptx": "b.pptx"}


def test_nested_local_sources_with_duplicate_basenames_keep_provenance(tmp_path):
    source = tmp_path / "source"
    for folder in ("a", "b"):
        (source / folder).mkdir(parents=True)
        (source / folder / "deck.pptx").write_bytes(folder.encode())
    dest = tmp_path / "decks"
    dest.mkdir()
    control._copy_local_source(source, dest, 2)
    files = sorted(p.name for p in dest.glob("*.pptx"))
    assert files == ["0001-deck.pptx", "0002-deck.pptx"]
    assert json.loads((dest / "source-map.json").read_text()) == {
        "0001-deck.pptx": "a/deck.pptx", "0002-deck.pptx": "b/deck.pptx"}


def test_hf_command_carries_custom_publish_layout(tmp_path):
    cfg = config.defaults("codex")
    cfg["concurrency"]["cpu_workers"] = 3
    cfg["execution"]["executor"] = "hf-jobs"
    cfg["storage"].update(type="hf", results_repo="owner/results")
    cfg["executors"]["hf-jobs"]["repo"] = "owner/pptxgym"
    cfg["publish"].update(
        enabled=True, rollout_repo="owner/rollout", assets_repo="owner/assets",
        task_lists=["eval/all.json", "eval/pptx.json"], series="222",
        series_first=2220001, series_last=2229999)
    resolved = {"name": "batch", "count": 3, "mode": "fast",
                "workers": config.resolve_workers(cfg, 3), "resume": False}
    cmd, _ = control._hf_command(cfg, resolved)
    joined = "\n".join(cmd)
    assert "PPTXGYM_ASSETS_REPO=owner/assets" in joined
    assert "PPTXGYM_ASSETS_PRIVATE=1" in joined
    assert "PPTXGYM_TASK_LISTS_JSON=[\"eval/all.json\",\"eval/pptx.json\"]" in joined
    assert "PPTXGYM_SERIES=222" in joined
    assert "PPTXGYM_CPU_WORKERS=3" in joined
    assert "PPTXGYM_TIMEOUT_MINUTES=90" in joined


def test_hf_command_can_create_public_assets_repo(tmp_path):
    cfg = config.defaults("codex")
    cfg["execution"]["executor"] = "hf-jobs"
    cfg["storage"].update(type="hf", results_repo="owner/results")
    cfg["executors"]["hf-jobs"]["repo"] = "owner/pptxgym"
    cfg["publish"].update(
        enabled=True, rollout_repo="owner/rollout", assets_repo="owner/assets",
        assets_private=False)
    resolved = {"name": "batch", "count": 1, "mode": "fast",
                "workers": config.resolve_workers(cfg, 1), "resume": False}

    cmd, _ = control._hf_command(cfg, resolved)

    assert "PPTXGYM_ASSETS_PRIVATE=0" in "\n".join(cmd)


def test_local_publish_requires_provenance_manifest(tmp_path):
    cfg = config.defaults()
    cfg["source"].update(type="local", path=str(tmp_path))
    cfg["publish"].update(enabled=True, rollout_repo="x/y", assets_repo="x/z")
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True)
    with pytest.raises(control.ControlError, match="requires source.manifest"):
        control._local_provenance(cfg, run)


def test_publish_dry_run_builds_a_plan_without_upload_or_ledger(
        tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    rollout = tmp_path / "rollout"
    rollout.mkdir()
    cfg = config.defaults("codex")
    cfg["execution"].update(executor="local", work_root=str(tmp_path))
    cfg["publish"].update(
        enabled=True, rollout_checkout=str(rollout),
        rollout_repo="owner/rollout", assets_repo="owner/assets")
    snapshot = config.public(cfg)
    snapshot["resolved"] = {
        "name": "run", "count": 1, "mode": "fast",
        "workers": config.resolve_workers(cfg, 1), "resume": False,
    }
    (run / control.RUN_FILE).write_text(__import__("tomli_w").dumps(snapshot))
    plan = {
        "work": str(run / "work"), "staging": "stage",
        "rollout": str(rollout), "repo": "owner/assets",
        "registry": "registry", "registry_next": 1100002,
        "notes": [], "rows": [], "already": [], "refused": [],
        "leaks": [], "no_source_record": [], "hf_files": 0,
        "hf_bytes": 0, "hf_commits": 0, "git_files": 0,
        "git_bytes": 0, "allocated": [],
    }
    monkeypatch.setattr(control, "_clone_rollout", lambda *_: rollout)
    from pptxgym.delivery import publish
    monkeypatch.setattr(publish, "build", lambda *a, **k: plan)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: pytest.fail(
        "dry-run must not publish"))
    args = type("Args", (), {
        "run": str(run), "dry_run": True, "no_git_push": False,
    })()
    assert control._publish_managed(args) == 0
    assert not (run / "events.jsonl").exists()
    assert not (run / "publish.json").exists()


def test_managed_publish_creates_private_assets_by_default(
        tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True)
    rollout = tmp_path / "rollout"
    (rollout / "evaluation_examples/task_class").mkdir(parents=True)
    cfg = config.defaults("codex")
    cfg["publish"].update(
        enabled=True, rollout_checkout=str(rollout),
        rollout_repo="owner/rollout", assets_repo="owner/assets")
    seen = {}
    from pptxgym.delivery import publish
    monkeypatch.setattr(publish, "build", lambda *a, **k: {
        "leaks": [], "rows": [], "work": "work", "staging": "stage",
        "rollout": str(rollout), "repo": "owner/assets",
        "registry": "registry", "registry_next": 1100001, "notes": [],
        "already": [], "refused": [], "no_source_record": [],
        "hf_files": 0, "hf_bytes": 0, "hf_commits": 0,
        "git_files": 0, "git_bytes": 0, "allocated": [],
    })
    monkeypatch.setattr(publish, "publish", lambda *a, **kw: (
        seen.update(kw) or {"uploaded": 0, "verified": True,
                            "written": [], "git": "none"}))
    control._publish_local(cfg, run, {})
    assert seen["assets_private"] is True


def test_config_rejects_local_publish_without_attribution_manifest(tmp_path):
    cfg = config.defaults()
    cfg["source"].update(type="local", path=str(tmp_path))
    cfg["publish"].update(enabled=True, rollout_repo="x/y", assets_repo="x/z")
    with pytest.raises(config.ConfigError, match="source.manifest"):
        config.validate(cfg)


def test_native_credential_store_is_exported_only_for_remote_container(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"secret"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cfg = config.defaults("codex")
    cfg["credentials_file"] = str(tmp_path / "missing.toml")
    routes = config.route_runtime(cfg)
    assert "CODEX_AUTH_B64" not in control._secret_env(cfg, routes)
    assert "CODEX_AUTH_B64" in control._secret_env(cfg, routes, portable=True)


def test_model_environment_excludes_publish_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("HF_TOKEN", "hub-secret")
    cfg = config.defaults("claude")
    routes = config.route_runtime(cfg)
    assert "GH_TOKEN" not in control._secret_env(cfg, routes)
    assert "HF_TOKEN" not in control._secret_env(cfg, routes)
    platform = control._secret_env(cfg, routes, platform=True)
    assert platform["GH_TOKEN"] == "github-secret"
    assert platform["HF_TOKEN"] == "hub-secret"


def test_zenodo_provenance_joins_by_filename_not_deck_position(tmp_path):
    run = tmp_path / "batch"
    decks = run / "decks"
    work = run / "work"
    decks.mkdir(parents=True)
    work.mkdir()
    rows = [
        {"name": "z.pptx", "record": "10/z", "license": "cc-by-4.0",
         "url": "https://example/z", "sha256": "z"},
        {"name": "a.pptx", "record": "10/a", "license": "cc0-1.0",
         "url": "https://example/a", "sha256": "a"},
    ]
    (decks / "batch-fetch.json").write_text(json.dumps(rows))
    for did, name in (("deck0001", "a.pptx"), ("deck0002", "z.pptx")):
        d = work / did
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"origin": str(decks / name)}))
    control._zenodo_provenance(run)
    first = json.loads((work / "deck0001" / "provenance.json").read_text())
    assert first["doi"] == "10/a"
    assert first["license"] == "cc0-1.0"
