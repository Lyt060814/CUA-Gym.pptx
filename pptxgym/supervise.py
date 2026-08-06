"""Watching a batch of decks run somewhere else, without being lied to.

The supervisor this replaces was forty lines of shell and it misled its reader
three times in one day, each time in the same direction — by looking like it
was working:

  * it took a "last line already reported" argument, and being called with the
    default re-announced the whole history on every launch, so it exited
    immediately and supervised nothing;
  * it re-reported historical alarms as new ones;
  * and `hf jobs inspect` has no timeout, so when the API hung the watcher hung
    with it. Two of them sat frozen for four and a half hours against a job
    that had finished, and a frozen watcher is indistinguishable from a quiet
    one from the outside.

The last is the worst and it shapes this module. **The parsing and the
judgement are pure functions over text**, so they can be tested without a
network; fetching is a thin caller's job that can hang, and when it does, the
state file's own age says so. A monitor that cannot say "I last heard anything
at 13:40" cannot be trusted to say "everything is fine".

The other rule is from the same day: a detector has to be checked against a
known-good input and has to state what it says when nothing is wrong. Silence
is not a report — it is the thing a dead monitor also produces.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: `  deck0007  task_8cf41208274f  9 component(s)  consistency=ok`
DECK_LINE = re.compile(r"^\s{2}[·~]?\s*(deck\d+)\s\s+(.*?)\s*$")

#: `### run — eleven stages, ten decks  [10:29:04]`
PHASE_LINE = re.compile(r"^###\s+(.*?)\s+\[(\d\d:\d\d:\d\d)\]\s*$")

#: How a deck's line is read. Order matters: the first match wins, and the
#: terminal outcomes are tested before the shapes that also appear inside them
#: (`PARKED after 3 repair attempts` contains neither "REJECTED" nor "FAILED",
#: but `ESCALATED` lines carry a signature that reads like prose).
KINDS: tuple[tuple[str, re.Pattern], ...] = (
    ("packaged", re.compile(r"consistency=ok")),
    ("escalated", re.compile(r"\bESCALATED\b")),
    ("parked", re.compile(r"\bPARKED\b")),
    ("crashed", re.compile(r"\bCRASHED\b")),
    ("infra", re.compile(r"\bINFRA\b|429|session limit|Too Many Requests")),
    ("failed", re.compile(r"\bFAILED\b")),
    ("rejected", re.compile(r"\bREJECTED\b")),
    ("no_op_repair", re.compile(r"repair CHANGED NOTHING")),
    # A deck that cannot start because something else holds its lock. It read
    # as `progress` — it is a line, and nothing else matched — which is how two
    # decks sat locked by a dead run's pid for twenty minutes looking like they
    # were working. "Waiting" and "working" must not print the same.
    ("blocked", re.compile(r"\bBUSY\b")),
    ("truncated", re.compile(r"\bTRUNCATED\b")),
    ("repairing", re.compile(r"\brepaired \(attempt")),
    ("skipped", re.compile(r"^skipped —")),
    ("progress", re.compile(r".")),
)

#: Terminal for this run: nothing more will happen to the deck without a human
#: or a new job.
#:
#: `failed` belongs here even though it sounds transient. A deck the renderer
#: will not open is not going to be picked up again *in this run*, and leaving
#: it out had the two decks nobody can fix reported as "quiet for 32 minutes"
#: every poll — which is true, and is also what a finished deck looks like.
SETTLED = {"packaged", "parked", "escalated", "failed"}

#: Minutes with no line at all from a deck before it is worth mentioning.
#: Not a guess: the longest legitimate agent stage seen across runs 7–11 is the
#: solvability probe at roughly twenty minutes, so twenty-five leaves room
#: without waiting out a whole extra stage.
STALL_MINUTES = 25

#: The same complaint this many times, with no change in between, is a loop
#: rather than a retry. Two is enough — deck0003 received an identical work
#: order three times and every one of them was spent on a defect the repairer
#: had no power to fix.
LOOP_REPEATS = 2

_NOISE = (
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d+\.\d+\b"), "<num>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def _norm(text: str) -> str:
    out = text.lower()
    for pattern, repl in _NOISE:
        out = pattern.sub(repl, out)
    return out.strip()


def classify(text: str) -> str:
    for kind, pattern in KINDS:
        if pattern.search(text):
            return kind
    return "progress"


@dataclass
class DeckState:
    deck: str
    kind: str = "unknown"
    last: str = ""
    #: local seconds-since-epoch when a line for this deck was *first seen*.
    #: The log carries no per-line timestamp, so elapsed time is measured by
    #: the watcher rather than read off the text — which means it is only as
    #: good as the polling, and the state file's age is what says so.
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    lines: int = 0
    complaints: dict = field(default_factory=dict)

    def settled(self) -> bool:
        return self.kind in SETTLED


@dataclass
class Alert:
    level: str          # "stop" | "look"
    deck: str
    what: str
    detail: str = ""


#: Where the run stops narrating and starts summarising. Everything after it
#: re-lists every deck in the same two-space format — `deck0003
#: attacks.json:rejected → recipe: ...` — so reading on turns one summary into
#: ten fresh "events", every deck's final state becomes whatever the summary
#: said about it last, and four settled decks came back reading `progress`.
#: Found by running this against a real log rather than against its fixtures.
#: There are two of them, and cutting at only the second left the residue that
#: made this worth chasing: `run` prints its own stage table *before* the
#: script's `### where it got to` prints another. Its "N deck(s) waiting on a
#: repair" block lists decks in the same two-space format, so four settled
#: decks came back reading `progress` with a work-order line as their last
#: event. Whichever marker appears first is where narration ends.
SUMMARY_MARKERS = ("stages: 1·ingested", "### where it got to")


def read_log(text: str) -> list[tuple[str, str]]:
    """Every deck line in order, as `(deck, message)`.

    Progress bars and upload chatter are not deck lines and are dropped here
    rather than filtered by every caller — an earlier version matched `429`
    inside `wget`'s byte counts and cried about a rate limit on a healthy
    download, which is the exact failure this whole module exists to avoid.
    """
    cut = min((text.find(m) for m in SUMMARY_MARKERS if m in text),
              default=len(text))
    out = []
    for line in text[:cut].splitlines():
        hit = DECK_LINE.match(line)
        if hit:
            out.append((hit.group(1), hit.group(2)))
    return out


def phases(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in
            (PHASE_LINE.match(x) for x in text.splitlines()) if m]


def update(states: dict[str, DeckState], text: str, now: float | None = None,
           seen: int = 0) -> tuple[dict[str, DeckState], int]:
    """Fold new log lines into per-deck state. Returns `(states, seen)`.

    `seen` is how many deck lines have already been folded in. Passing it back
    is what makes repeated polling cheap and, more to the point, what stops the
    watcher re-reacting to history — the bug that made its predecessor exit
    instantly on every launch.
    """
    now = time.time() if now is None else now
    lines = read_log(text)
    for deck, message in lines[seen:]:
        st = states.get(deck)
        if st is None:
            st = states[deck] = DeckState(deck, first_seen_at=now)
        st.kind = classify(message)
        st.last = message
        st.last_seen_at = now
        st.lines += 1
        # Only what the repair loop can act on.
        #
        # `failed` was in this set and it produced two false alarms on the
        # first real log: deck0002 (soffice will not convert it) and deck0010
        # (thirteen of fourteen pages) each report the same thing twice because
        # the *run* retries `inspect`, not because a repair is going round in
        # circles. A deck LibreOffice cannot open is a fact about the deck that
        # will repeat every time it is asked, and calling that a loop is how a
        # detector teaches its reader to ignore it.
        if st.kind in ("rejected", "no_op_repair"):
            key = _norm(message)[:200]
            st.complaints[key] = st.complaints.get(key, 0) + 1
    return states, len(lines)


def diagnose(states: dict[str, DeckState], now: float | None = None,
             stall_minutes: int = STALL_MINUTES) -> list[Alert]:
    """What is worth waking somebody for. Empty means empty, and the caller
    is expected to say so out loud rather than print nothing."""
    now = time.time() if now is None else now
    out: list[Alert] = []
    for st in sorted(states.values(), key=lambda s: s.deck):
        if st.kind == "escalated":
            out.append(Alert("stop", st.deck, "escalated", st.last[:200]))
            continue
        if st.settled():
            continue
        for text, count in st.complaints.items():
            if count >= LOOP_REPEATS:
                out.append(Alert(
                    "stop", st.deck, f"same complaint {count}x",
                    f"{text[:160]} — a repair that cannot change the outcome "
                    f"is spending attempts on something it may not be able to "
                    f"fix"))
        if st.kind == "blocked":
            out.append(Alert(
                "stop", st.deck, "waiting on a lock",
                f"{st.last[:170]} — if the holder is not alive, this deck "
                f"will wait for the rest of the run"))
        if st.kind in ("crashed", "infra"):
            out.append(Alert("look", st.deck, st.kind, st.last[:200]))
        # Silence is only news when nothing else has explained it. A deck
        # waiting on a lock is quiet *because* it is waiting, and saying both
        # is how a reader learns to skim the second line.
        idle = (now - st.last_seen_at) / 60.0
        already = any(a.deck == st.deck for a in out)
        if idle >= stall_minutes and not already:
            out.append(Alert("look", st.deck, f"quiet {idle:.0f}m",
                             f"last: {st.last[:120]}"))
    return out


def report(states: dict[str, DeckState], alerts: list[Alert],
           now: float | None = None, log_age_s: float | None = None) -> str:
    """One screen. Says what it checked even when it found nothing.

    "No alerts" printed alone is what a broken monitor prints too, so the
    counts and the age of the evidence are part of the report rather than
    something a reader has to ask for.
    """
    now = time.time() if now is None else now
    by_kind: dict[str, list[str]] = {}
    for st in states.values():
        by_kind.setdefault(st.kind, []).append(st.deck)

    lines = []
    done = len(by_kind.get("packaged", []))
    lines.append(f"{done}/{len(states)} packaged   "
                 + "   ".join(f"{k}:{len(v)}" for k, v in
                              sorted(by_kind.items()) if k != "packaged"))
    for st in sorted(states.values(), key=lambda s: s.deck):
        idle = (now - st.last_seen_at) / 60.0
        flag = "  " if st.settled() else ("!!" if idle >= STALL_MINUTES else "  ")
        lines.append(f"{flag} {st.deck}  {st.kind:12s} {idle:5.1f}m  "
                     f"{st.last[:88]}")
    if alerts:
        lines.append("")
        for a in alerts:
            lines.append(f"[{a.level}] {a.deck}  {a.what}  {a.detail[:150]}")
    else:
        lines.append("\nnothing alarming: no deck escalated, none repeating a "
                     f"complaint, none quiet for {STALL_MINUTES}m")
    if log_age_s is not None:
        lines.append(f"(evidence is {log_age_s / 60:.1f} minutes old — if this "
                     f"stops moving, the watcher is stuck, not the run)")
    return "\n".join(lines)


def load(path) -> tuple[dict[str, DeckState], int]:
    p = Path(path)
    if not p.exists():
        return {}, 0
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}, 0
    states = {k: DeckState(**v) for k, v in (raw.get("decks") or {}).items()}
    # Re-read every stored verdict with today's rules.
    #
    # `update` only classifies lines it has not seen, so a deck that has gone
    # quiet keeps whatever the classifier said when its last line arrived —
    # including from a version of the classifier that no longer exists. It
    # showed up within minutes of `BUSY` becoming its own state: the two decks
    # stuck on a dead run's lock went on printing `progress`, because their
    # kind was decided before `blocked` was a kind — and a stuck deck is by
    # definition never going to produce the new line that would re-read it.
    #
    # A cached judgement from superseded code is the defect the stage
    # fingerprints exist to catch, turning up in the one tool whose whole job
    # is not to mislead its reader. Reclassifying on load is cheap and cannot
    # go stale.
    for st in states.values():
        if st.last:
            st.kind = classify(st.last)
    return states, int(raw.get("seen") or 0)


def save(path, states: dict[str, DeckState], seen: int) -> None:
    Path(path).write_text(json.dumps({
        "seen": seen, "at": time.time(),
        "decks": {k: v.__dict__ for k, v in states.items()},
    }, ensure_ascii=False))


def main(argv=None) -> int:
    """`python3 -m pptxgym.supervise <log-file> [--state F] [--quiet-ok]`

    Reads a log *file*, not a network. Fetching is the caller's job, so that a
    hanging API cannot hang the judgement — the failure that left two watchers
    frozen for four and a half hours against a job that had already finished.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="pptxgym.supervise")
    ap.add_argument("log", help="a file, or - for stdin")
    ap.add_argument("--state", default=None,
                    help="carry per-deck state between polls")
    ap.add_argument("--age", type=float, default=None,
                    help="seconds since the log was fetched")
    args = ap.parse_args(argv)

    text = (sys.stdin.read() if args.log == "-"
            else Path(args.log).read_text(encoding="utf-8", errors="replace"))
    states, seen = load(args.state) if args.state else ({}, 0)
    states, seen = update(states, text, seen=seen)
    if args.state:
        save(args.state, states, seen)
    alerts = diagnose(states)
    print(report(states, alerts, log_age_s=args.age))
    # 2 means "something wants a human", so a caller can branch without
    # parsing the report. 0 is a clean look, and it still printed one.
    return 2 if any(a.level == "stop" for a in alerts) else 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
