"""Argument parser for the managed and diagnostic command surfaces."""

from __future__ import annotations

import argparse

from ..core import pipeline as pl
from ..management import control
from ..orchestration import agent, mailbox, profiles


def build_parser(commands):
    """Build the CLI and bind callbacks from ``commands``.

    Keeping callbacks injected avoids a parser/implementation import cycle and
    makes the command table independently reviewable.
    """
    ap = argparse.ArgumentParser(
        prog="pptxgym", description=commands.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=str(commands.DEFAULT_WORK),
                    help="working directory (default: ./work)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    control.add_commands(sub)

    p = sub.add_parser("ingest", help="register source decks")
    p.add_argument("paths", nargs="+", help=".pptx files or directories")
    p.set_defaults(func=commands.cmd_ingest)

    def deck_args(parser):
        parser.add_argument("--deck", nargs="*", help="deck ids (default: all)")
        parser.add_argument(
            "--workers", "--agent-workers", type=int, default=1,
            dest="workers", help="agent stages in parallel (default: 1). These "
            "spend API capacity and are ~85% of the wall clock")
        parser.add_argument(
            "--cpu-workers", type=int, default=None,
            help="soffice/render stages in parallel (default: cores/4 = "
                 f"{commands._default_cpu_workers()})")

    def model_args(parser):
        parser.add_argument(
            "--model", default=None,
            help="`opus` for every agent stage, or per stage: "
                 "`propose=opus,recipe=sonnet` (stages: "
                 + ", ".join(agent.ROLES) + ")")
        parser.add_argument(
            "--effort", default=None,
            help="`claude --effort`: " + ", ".join(agent.EFFORTS)
                 + ". Bare or per stage, like --model")
        parser.add_argument(
            "--fallback-model", default=None, dest="fallback_model",
            help="model used when the primary is overloaded; the actual model "
                 "is recorded per stage")

    def retry_arg(parser):
        parser.add_argument(
            "--api-retries", type=int, default=None, dest="api_retries",
            help="retries when the API itself fails, never a timeout or a "
                 f"truncated answer (lane default: {agent.API_RETRIES} private, "
                 f"{agent.SHARED_RETRIES} shared; expired login multiplier "
                 f"{agent.AUTH_RETRIES})")

    def render_args(parser):
        parser.add_argument(
            "--dpi", type=int, default=None,
            help=f"render DPI (default: {pl.RENDER_DPI_COARSE}, or "
                 f"{pl.RENDER_DPI} where text is under {pl.SMALL_TEXT_PT:g}pt)")
        parser.add_argument(
            "--roundtrip", action="store_true",
            help="also round-trip through LibreOffice as a corpus-fragility signal")

    p = sub.add_parser("inspect", help="digest + renders (deterministic)")
    deck_args(p)
    render_args(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=commands.cmd_inspect)

    p = sub.add_parser("wps", help="measure the WPS round trip (GUI)")
    p.add_argument("--deck", nargs="*", help="deck ids (default: all)")
    p.add_argument("--workers", type=int, default=1,
                   help="decks at once, one virtual display and ~660MB each")
    p.add_argument("--sample", type=int, default=None,
                   help="measure this many decks, spread across the batch")
    p.add_argument("--force", action="store_true",
                   help="re-measure decks that already have a number")
    p.set_defaults(func=commands.cmd_wps)

    agent_commands = (
        ("propose", "agent: which tasks this deck should yield", 30,
         commands.cmd_propose),
        ("recipe", "agent: proposal -> executable recipe", 40,
         commands.cmd_recipe),
        ("reconcile", "agent: does the file still match the instruction", 30,
         commands.cmd_reconcile),
        ("solvable", "agent: can this task actually be done", 30,
         commands.cmd_solvable),
    )
    for name, help_text, timeout, callback in agent_commands:
        p = sub.add_parser(name, help=help_text)
        deck_args(p)
        p.add_argument("--force", action="store_true")
        model_args(p)
        p.add_argument("--timeout", type=int, default=timeout, help="minutes")
        retry_arg(p)
        p.set_defaults(func=callback)

    for name, help_text, callback in (
            ("degrade", "apply the recipe + package gate", commands.cmd_degrade),
            ("materialise", "produce the assets the task promises",
             commands.cmd_materialise),
            ("score", "derive the reward from delta.json and calibrate it",
             commands.cmd_score)):
        p = sub.add_parser(name, help=help_text)
        deck_args(p)
        p.add_argument("--force", action="store_true")
        p.set_defaults(func=callback)

    p = sub.add_parser(
        "adopt", help="fast profile: record an artefact you wrote yourself, checked")
    deck_args(p)
    p.add_argument("--stage", required=True, choices=list(profiles.ADOPTABLE))
    p.add_argument("--force", action="store_true")
    p.add_argument("--profile", choices=list(profiles.PROFILES), default=None,
                   help=f"default: ${profiles.PROFILE_ENV} or {profiles.FULL}")
    p.set_defaults(func=commands.cmd_adopt)

    def harden_args(parser):
        parser.add_argument("--attack-workers", type=int, default=4)
        parser.add_argument("--wps-workers", type=int, default=2)
        parser.add_argument(
            "--no-wps", action="store_true",
            help="skip the round-trip attack; the unproven gate rejects the task")
        parser.add_argument(
            "--keep-candidates", action="store_true",
            help="keep candidate decks under work/<deck>/attacks/")

    p = sub.add_parser("harden", help="try to cheat the task; reject it if that works")
    deck_args(p)
    harden_args(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=commands.cmd_harden)

    p = sub.add_parser("package", help="consistency gate, then write the runnable task")
    deck_args(p)
    p.add_argument("--out", default=None,
                   help="where runnable tasks go (default: <work>/emitted)")
    p.add_argument("--task-id", default=None,
                   help="override the content-derived id (one deck only)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=commands.cmd_package)

    p = sub.add_parser("status", help="stage table")
    deck_args(p)
    p.add_argument("--all", action="store_true",
                   help=f"full table even beyond {commands.BIG_BATCH} decks")
    p.set_defaults(func=commands.cmd_status)

    p = sub.add_parser("mail", help="act on the supervising side's reply")
    p.add_argument("--file", default=None,
                   help=f"where the reply is (default: <work>/{mailbox.FILENAME})")
    p.set_defaults(func=commands.cmd_mail)

    p = sub.add_parser("blocked", help="defects grouped rather than listed per deck")
    p.add_argument("--json", action="store_true",
                   help="machine-readable, for the supervising side")
    p.set_defaults(func=commands.cmd_blocked)

    p = sub.add_parser("history", help="render one run's event stream")
    p.add_argument("run", nargs="?", default=None,
                   help=f"run id or path to {pl.RUN_EVENTS}; default: latest")
    p.add_argument("--list", action="store_true", help="every recorded run")
    p.add_argument("--at", default=None, metavar="HH:MM:SS",
                   help="who was in which stage at that moment")
    p.set_defaults(func=commands.cmd_history)
    return ap
