"""A deck's way of saying "this one is not mine to fix".

The repair loop can send a deck back to `proposed`, `recipe` or `materialise`,
and that is the whole of its authority. It may not change code — the repairer
runs with `revert_tool_changes` watching, and any edit to the shared tools is
reverted and the deck parked. So when what actually blocks a deck is a defect
in the pipeline itself, three repair attempts are spent rewriting a recipe that
was never wrong, and the deck is parked reading like a bad deck.

deck0003 is the worked example. Its `wrong_params` attack could not perturb a
`text_runs` component because no branch existed, then could not perturb a
`move` on a placeholder because `_get_box` returns `None` for a shape that
inherits its transform. Both are our defects. It received the same work order
three times, changed the recipe three times, and was parked. The skill even
tells the repairer what to do here — "write down in `repair.md` where it is
stuck and what decision a human has to make, then stop" — and the pipeline
scored that as `CHANGED NOTHING`, identical to a repairer that did nothing at
all, because `repair.md` is not among the files it watches.

This module is the missing channel. Two things use it, and the difference
between them matters more than anything else here:

  * **A gate escalates a fact.** `wrong_params` reporting an operator with no
    perturbation branch is not an opinion — an operator the executor emits and
    the battery cannot perturb is our gap by definition, whatever deck it
    turned up on. `who="pipeline"`, and it can be believed.

  * **A repair agent escalates a claim.** It has every incentive to reach for
    this: "the pipeline is broken" is the one answer that ends a repair it
    cannot finish. `who="unknown"` until somebody checks, and the frontend has
    to show it as a claim, not as a finding.

`signature` is what makes this worth building. At ten decks a defect blocks
one; at four hundred it blocks forty, and forty separate investigations of one
bug is worse than the problem. The signature is derived mechanically — never
written by an agent, which could not produce a stable one — so N reports of one
defect collapse to one entry with N decks attached, fixed once and resumed
together.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

#: The file a repair agent writes to escalate, beside the deck.
FILENAME = "escalation.json"

#: Where a run collects them for shipping out.
RUN_FILE = "escalations.jsonl"

#: Who a report says is at fault. `pipeline` is only ever set by the pipeline
#: about itself; an agent's report starts at `unknown` no matter what it claims,
#: and is promoted by a human or by the frontend having checked.
WHO = ("pipeline", "deck", "unknown")

#: Trust, and it is not a formality. A gate's report is a mechanical fact; an
#: agent's is a statement of belief by something that wants to stop working.
SOURCES = ("gate", "repair-agent")

#: Everything that makes one occurrence itself rather than the class it belongs
#: to. Stripped before hashing, or nothing would ever match anything: two decks
#: hitting one bug differ in deck id, slide number, shape path, component id,
#: every measured number and every hash.
_NOISE = (
    (re.compile(r"\bdeck\d+\b", re.I), "<deck>"),
    (re.compile(r"\bc\d{3,}\b"), "<component>"),
    (re.compile(r"\bd\d+\b"), "<deg>"),
    (re.compile(r"\bslides?\s+[\d,\s and]+", re.I), "slide <n> "),
    (re.compile(r"@\d+"), "@<n>"),
    (re.compile(r"\bp-\d+\b"), "p-<n>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d+\.\d+\b"), "<num>"),
    (re.compile(r"\b\d+%"), "<pct>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"[/\\][\w./\\-]+"), "<path>"),
    (re.compile(r"\s+"), " "),
)


def normalise(text: str) -> str:
    """One occurrence reduced to the class of defect it is an instance of."""
    out = str(text or "").lower()
    for pattern, repl in _NOISE:
        out = pattern.sub(repl, out)
    return out.strip()


def signature(kind: str, detail: str, explicit: str | None = None) -> str:
    """The dedup key.

    `explicit` is for callers that know the class exactly — an operator with no
    perturbation branch is `attack/wrong_params/no-branch/text_runs` and does
    not need a hash of prose to say so. Everything else falls back to hashing
    the normalised text, which is coarser but never wrong in the direction that
    matters: two different defects hashing together would be, and two
    occurrences of one defect failing to is merely a missed saving.
    """
    if explicit:
        return explicit
    digest = hashlib.sha1(normalise(detail).encode()).hexdigest()[:12]
    return f"{kind}/{digest}"


def record(deck_id: str, stage: str, kind: str, detail: str, *,
           source: str, who: str = "unknown", explicit_signature=None,
           evidence: dict | None = None, attempt: int | None = None) -> dict:
    """One escalation, as a plain dict.

    `who` is forced to `unknown` for anything an agent said, whatever it said.
    That is not distrust of a particular agent; it is that "the pipeline is
    broken" is the answer which ends a repair the agent cannot finish, so it is
    exactly the claim that must not be self-certifying.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")
    if source != "gate":
        who = "unknown"
    if who not in WHO:
        raise ValueError(f"who must be one of {WHO}, not {who!r}")
    return {
        "deck": deck_id,
        "stage": stage,
        "kind": kind,
        "signature": signature(kind, detail, explicit_signature),
        "who": who,
        "source": source,
        "detail": str(detail)[:1000],
        "evidence": evidence or {},
        "attempt": attempt,
        # not `time.time()`: a run has to be comparable to the log beside it,
        # and the log stamps in UTC to the second
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write(deck_root, rec: dict) -> Path:
    """Put it beside the deck, where `_repair_one` looks for it."""
    path = Path(deck_root) / FILENAME
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return path


def read(deck_root) -> dict | None:
    """Whatever the repair agent left, or nothing.

    Never raises. A malformed escalation is the agent failing to escalate, not
    a reason to end the stage — the same rule the prompt summaries follow, and
    for the same reason: the most volatile input to a stage must not be what
    kills it.
    """
    path = Path(deck_root) / FILENAME
    if not path.exists():
        return None
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def sanitise(deck_id: str, stage: str, raw: dict, attempt=None) -> dict:
    """Rebuild an agent's escalation from the only parts it may decide.

    The file is written by the repair agent, so every field in it is a claim.
    Three of them are not the agent's to make and are taken back here:

      * `who` — always `unknown`. See `record`.
      * `source` — always `repair-agent`, whatever it says.
      * `signature` — derived, never copied. An agent cannot produce a stable
        one across decks, and the dedup that makes this channel worth having
        depends entirely on the key being mechanical. An agent-chosen key would
        either never match (forty investigations of one bug) or match too much
        (one fix resuming decks blocked by something else).

    What the agent does decide is the part only it knows: what it tried, what
    it observed, and where it thinks the wall is.
    """
    detail = str(raw.get("detail") or raw.get("why") or raw.get("what") or "")
    out = record(deck_id, stage, str(raw.get("kind") or "repair"), detail,
                 source="repair-agent", attempt=attempt,
                 evidence=raw.get("evidence") if isinstance(
                     raw.get("evidence"), dict) else {})
    out["verdict"] = str(raw.get("verdict") or "").lower()
    # kept, clearly labelled, because it is worth reading and worth not
    # believing: what the agent thought was at fault is a lead, not a finding
    out["agent_says_who"] = str(raw.get("who") or "unknown")
    out["tried"] = str(raw.get("tried") or "")[:600]
    return out


def is_blocked(rec: dict | None) -> bool:
    """Does this record actually say "stop", as against merely existing?

    An agent that writes an escalation and then repairs the deck anyway has not
    given up, and reading the file's presence as a verdict would park a deck
    that was just fixed.
    """
    return bool(rec) and str(rec.get("verdict") or "").lower() == "blocked"


def collect(work) -> list[dict]:
    """Every escalation currently standing, one deck at a time."""
    out = []
    for deck_dir in sorted(Path(work).glob("deck*")):
        got = read(deck_dir)
        if got:
            out.append(got)
    return out


def group(records: list[dict]) -> list[dict]:
    """Collapse to one entry per defect, with the decks it is blocking.

    This is the whole economic argument for the channel. Ten decks report one
    missing perturbation branch as ten rejections today; grouped, it is one
    thing to check, one fix, and ten decks to resume — and the count of decks
    is itself the priority order, which no per-deck view can show.
    """
    by_sig: dict[str, dict] = {}
    for rec in records:
        sig = rec.get("signature") or "?"
        item = by_sig.setdefault(sig, {
            "signature": sig, "kind": rec.get("kind"),
            "who": rec.get("who"), "source": rec.get("source"),
            "detail": rec.get("detail"), "decks": [], "evidence": [],
        })
        if rec.get("deck") and rec["deck"] not in item["decks"]:
            item["decks"].append(rec["deck"])
        if rec.get("evidence"):
            item["evidence"].append(rec["evidence"])
        # A gate's fact outranks an agent's claim about the same defect: if
        # anything mechanical reported this signature, the group is a finding.
        if rec.get("source") == "gate":
            item["source"], item["who"] = "gate", rec.get("who")
    return sorted(by_sig.values(),
                  key=lambda i: (-len(i["decks"]), i["signature"]))


def append_to_run(run_dir, rec: dict) -> None:
    """Add it to the run's own stream, which is what leaves the machine."""
    path = Path(run_dir) / RUN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
