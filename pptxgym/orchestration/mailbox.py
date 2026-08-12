"""The other direction: what the supervising side sends back.

`escalate` carries a defect out of a run. This carries an answer in. Together
they close the loop that today needs a human between them — a deck reports
what it cannot fix, the frontend checks it, fixes it, and says so, and the
decks behind that defect carry on in the same job instead of waiting for the
next one.

**Why a mailbox and not a call.** HF Jobs offers no exec, no attach and no
shell into a running job, so there is no channel to make a call over. What
there is: the job can write to a dataset (it already ships state every two
minutes) and it can read one back, and it holds a `GH_TOKEN`. So both
directions are files, and the frontend's reply is a file the run looks for
when it has nothing left to do.

**Why the reply carries a commit rather than a patch.** A running Python
process has already imported its modules; rewriting the files under it changes
nothing until something re-executes. So a fix arrives as a commit to check out
and the *run* is restarted against it, which is safe because the pipeline is
resumable from `work/` — finished stages keep their ticks and only what the
change invalidated runs again. Every stage records the digest of the code that
produced it, so `stale_by_code` names exactly the verdicts the fix reached and
the deck's owner picks the work back up from there.

**What a reply may not do.** `do` is a closed set. The reply is read by a
process holding tokens for three repositories, so "run this" is not one of the
things it can say, whatever arrives in the file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

#: The file the frontend publishes and a run reads.
FILENAME = "reply.json"

#: Where a run remembers what it has already acted on, so a reply that stays
#: in the dataset is not applied twice on the next poll.
APPLIED = "replies-applied.json"

#: Everything a reply may ask for. Closed, and checked before anything is
#: acted on: this file is read by a process holding write tokens for three
#: repositories, and "do as you are told" is not a property worth having there.
VERDICTS = {
    # the defect behind this signature is fixed; check out `commit` and let the
    # decks behind it carry on
    "fixed",
    # checked, and it is the deck's own problem after all — stop protecting it
    # and let the normal gates finish the job
    "not-ours",
    # checked, real, and not being fixed now: park the decks with the reason
    # rather than spending their remaining attempts on it
    "wontfix",
    # stop working this deck in this run, whatever its state
    "stop",
}


class BadReply(ValueError):
    """The reply is not something to act on. Never a reason to end the run —
    a malformed answer is the frontend failing to answer, and a run that dies
    of one has turned a supervision channel into a way to lose the batch."""


def _clean(item: dict, index: int) -> dict:
    if not isinstance(item, dict):
        raise BadReply(f"reply {index} is not a record")
    verdict = str(item.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise BadReply(f"reply {index}: verdict {verdict!r} is not one of "
                       f"{sorted(VERDICTS)}")
    signature = str(item.get("signature") or "").strip()
    decks = [str(d) for d in (item.get("decks") or []) if str(d).strip()]
    if not signature and not decks:
        raise BadReply(f"reply {index}: names neither a signature nor a deck, "
                       f"so there is no way to tell who it is for")
    commit = str(item.get("commit") or "").strip()
    if verdict == "fixed" and not commit:
        raise BadReply(f"reply {index}: 'fixed' with no commit — there is "
                       f"nothing to check out, and a fix nobody can name is "
                       f"not a fix")
    if commit and not (8 <= len(commit) <= 64 and
                       all(c in "0123456789abcdef" for c in commit.lower())):
        raise BadReply(f"reply {index}: {commit!r} is not a commit id")
    return {"id": str(item.get("id") or f"{signature}:{verdict}:{commit}"),
            "signature": signature, "decks": decks, "verdict": verdict,
            "commit": commit, "note": str(item.get("note") or "")[:500]}


def parse(raw: dict | None) -> list[dict]:
    """Validate a published reply into the entries worth acting on."""
    if not isinstance(raw, dict):
        raise BadReply("a reply is a record with a `replies` list")
    items = raw.get("replies")
    if not isinstance(items, list):
        raise BadReply("`replies` is missing or is not a list")
    return [_clean(item, i) for i, item in enumerate(items)]


def read(path) -> list[dict]:
    """Whatever is published, or nothing. Never raises.

    A reply that cannot be read is the frontend having failed to answer, which
    is the state the run was already in. Ending a batch over it would make the
    supervision channel a way to lose work.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        return parse(json.loads(p.read_text()))
    except (OSError, ValueError):
        return []


def unapplied(replies: list[dict], applied_path) -> list[dict]:
    """The entries this run has not acted on yet.

    The reply stays in the dataset after it is read — the frontend has no way
    to know when the run picked it up — so a run that polls twice would check
    out the same commit twice and, worse, count a `wontfix` against a deck's
    budget again.
    """
    seen = set()
    p = Path(applied_path)
    if p.exists():
        try:
            seen = set(json.loads(p.read_text()) or [])
        except (OSError, ValueError):
            seen = set()
    return [r for r in replies if r["id"] not in seen]


def mark_applied(replies: list[dict], applied_path) -> None:
    p = Path(applied_path)
    seen = []
    if p.exists():
        try:
            seen = list(json.loads(p.read_text()) or [])
        except (OSError, ValueError):
            seen = []
    for r in replies:
        if r["id"] not in seen:
            seen.append(r["id"])
    p.write_text(json.dumps(seen, ensure_ascii=False))


def targets(reply: dict, escalations: list[dict]) -> list[str]:
    """Which decks a reply is for.

    Named decks win when they are given. Otherwise every deck standing behind
    the signature — which is the point of the signature: one defect, one check,
    one fix, and every deck it was holding carries on at once. At ten decks
    that saves a round; at four hundred it is the difference between a fix and
    a backlog.
    """
    if reply["decks"]:
        return list(reply["decks"])
    return sorted({e["deck"] for e in escalations
                   if e.get("signature") == reply["signature"] and e.get("deck")})


def publish(path, replies: list[dict], run: str | None = None) -> Path:
    """Write a reply file. Validates first, so a malformed one is caught on
    the side that can still do something about it."""
    body = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run": run, "replies": replies}
    parse(body)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, ensure_ascii=False, indent=1))
    return p
