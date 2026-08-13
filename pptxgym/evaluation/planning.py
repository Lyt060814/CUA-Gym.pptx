"""Compile degradation records into deterministic reward plans.

Runtime scoring remains in ``comparators``; this module owns producer-side
file I/O, weight selection, floor calibration and coherence probes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import comparators as C

PLAN_FORMAT = C.PLAN_FORMAT
FLOOR_LIMIT = C.FLOOR_LIMIT
POS_TOL = C.POS_TOL
NATIVE_KINDS = C.NATIVE_KINDS
_BULK = C._BULK
Scene = C.Scene
Unscorable = C.Unscorable
inventory_pptx = C.inventory_pptx
_run_component = C._run_component
_sha = C._sha
_norm = C._norm
_damage = C._damage
_diagram = C._diagram
_table = C._table
_bbox = C._bbox
score = C.score


def _entries(delta: dict) -> list[tuple[int, dict]]:
    """Every recorded change as (gt slide index, entry), in a stable order."""
    out: list[tuple[int, dict]] = []
    for key in sorted((delta.get("slides") or {}), key=lambda k: int(k)):
        for entry in delta["slides"][key]:
            out.append((int(key), entry))
    for entry in (delta.get("cleared_notes") or []):
        out.append((int(entry["slide"]) - 1, {**entry, "op": "clear_notes"}))
    for entry in (delta.get("layout_edits") or []):
        out.append((0, {**entry, "op": "layout_edit"}))
    if delta.get("reorder_slides"):
        out.append((0, {**delta["reorder_slides"], "op": "reorder_slides"}))
    if delta.get("deleted_slides"):
        out.append((0, {"op": "delete_slides",
                        "pages": delta["deleted_slides"], "deg": None}))
    return out


def _est_steps(root: Path, task: dict) -> dict[str, int]:
    """Per-degradation step estimates.

    They live in `proposal.json`, not in `task.json`: reconcile copies the
    degradation records forward but drops `est_steps`, so the weights have to
    be read from the proposal the task was accepted from, matched by name.
    """
    path = root / "proposal.json"
    if not path.exists():
        return {}
    proposal = json.loads(path.read_text(encoding="utf-8"))
    for candidate in proposal.get("tasks") or []:
        if candidate.get("name") == task.get("name"):
            return {d["id"]: int(d.get("est_steps") or 0)
                    for d in candidate.get("degradations") or []
                    if d.get("id")}
    return {}


#: How far the proposer's declared step count may sit from the probe's measured
#: one before the declaration is treated as contradicted.  Per **degradation**,
#: worst case, and only consulted when the measurement is too partial to weight
#: by — a complete measurement simply wins, so this never arbitrates between
#: two usable numbers.  Re-derived on the ten-deck corpus (the figures this
#: comment used to carry — deck0004 1.9x, deck0010 2.3x, deck0007 4.6x — no
#: longer reproduce; only deck0006's 8.0x does):
#:
#:     1.000 deck0010 · 1.600 deck0003 · 1.857 deck0004 · 1.867 deck0009
#:     1.889 deck0002 · 1.917 deck0007 · 8.000 deck0006
#:
#: 3.0 still sits in the one gap that separates them, between 1.917 and 8.000.
#:
#: It is **not** the same fact as `agent.STEP_BAND` and the two are not in
#: competition: `STEP_BAND` is 25% on the **deck total**, and what it decides is
#: whether the probe owed a `rework` note against `proposed`; this is a ratio on
#: **one degradation**, and what it decides is whether reward may still be split
#: by a breakdown the probe has contradicted.  deck0010 is the worked example —
#: 480 measured against a declared 280 is 71% out, so `STEP_BAND` required the
#: note (it was filed), while nothing here fired, because the measurement was
#: complete and became the weights.
STEP_DISAGREEMENT = 3.0

#: How far two statements of the **same** step total may sit apart before they
#: contradict each other, as a fraction.  Deliberately the same number as
#: `agent.STEP_BAND` — that is the band the solvability rubric already uses for
#: "measured against declared", and "the parts against their own total" is the
#: same question asked of one document instead of two.  `test_comparators` pins
#: them together so they cannot drift into two opinions.
DECLARATION_SPLIT = 0.25


def _agrees(part: float, whole: float | None) -> bool:
    """Whether a breakdown agrees with the total it claims to break down."""
    if not whole:
        return False
    return abs(part - whole) / abs(whole) <= DECLARATION_SPLIT


def _num_or_none(value: Any) -> int | None:
    """A positive step count, or nothing.  `"280"` and `280.0` both count; a
    missing field, a zero and a stray string do not."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None

_STEP_DEG_RE = C._STEP_DEG_RE
#: A step figure, and only a step figure.  "d1 rebuild row on slide 12 ~55"
#: must not read 12, so a bare number is never taken — it has to be marked as
#: an estimate (`~55`, `about 60`, `roughly 140`) or counted as steps
#: (`140 steps`).
_STEP_N_RE = C._STEP_N_RE


def _measured_steps(root: Path,
                    declared: list[str]) -> tuple[dict[str, int], str, bool]:
    """Per-degradation step counts as the **solvability probe** measured them.

    `est_steps` is the proposer's declaration and nothing validated it.  The
    probe then does the same job independently — it opens the bundle, works out
    what each degradation would take in the GUI, and writes the total into
    `solvability.json` — and it disagrees per degradation by up to **8x**.
    Only the totals were ever compared, and loosely ("agrees with the declared
    285 within a band"), which is exactly why it survived: the per-degradation
    errors cancel in the sum.  Measured consequence on deck0006: the cheapest
    job (d1, one bitmap, ~15 steps) carried **0.3158** of the reward while the
    most expensive (d2, twenty shapes, ~140 steps) carried **0.2368** — 12.4x
    more reward per step for the trivial one, which points an agent that
    maximises reward per step at exactly the wrong work.

    **The probe's numbers are prose, and this is what makes them usable.**  No
    schema carries them: on the ten-deck corpus they appear as free text in
    `notes[]` or `est_steps_note`, on 5 decks of 10, in a form like
    *"d2 dominates at roughly 140 steps …, d3 about 60, … d1 about 15"*.  So a
    structured field is read first when one exists, and the prose is parsed
    only under three conditions that make a regex safe here:

    * a figure is taken only when it is **marked** as one (`~55`, `about 60`,
      `140 steps`) — never a bare number, because "d1 rebuild row on slide 12"
      would otherwise read 12;
    * **all or nothing** — if any declared degradation goes unmatched the parse
      is discarded, so it cannot half-work;
    * the parsed figures must **sum to the probe's own total** within
      `DECLARATION_SPLIT`.  That is the self-check that turns this from a guess
      into a measurement: `est_steps_measured` is written by the same agent in
      the same file and is not derived from the breakdown.  Measured on the
      five decks that carry a breakdown: 310 vs 310, 305 vs 310, 265 vs 270,
      165 vs 185, 170 vs 175.

    **The structured path is held to that third condition too**, and it used to
    be exempt from it — which put the weaker check on the stronger source.  A
    breakdown that does not add up to the total written beside it is one
    self-contradicting statement whichever field it arrived in, and the
    structured one is the field the weights actually come from on 7 of the 10
    decks.  (`agent.solvability_rubric_problems` enforces the same sum, but it
    guards the *stage*; `build_plan` reads `solvability.json` off disk — an
    archived attempt, a hand-edited file, a probe whose rubric changed — and a
    reader that trusts its input because some other reader checked it is not
    checking anything.)

    The third value is whether the numbers may be **weighted by**.  Numbers
    that fail one of the conditions are still returned, because a measurement
    too partial or too self-contradictory to redistribute reward is still a
    measurement that can contradict the declaration, and `build_plan` refuses
    on that.
    """
    path = root / "solvability.json"
    if not path.exists():
        return {}, "no solvability.json", False
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        return {}, f"solvability.json unreadable ({err})", False

    total = report.get("est_steps_measured")

    structured = {d["id"]: int(d["est_steps_measured"])
                  for d in (report.get("degradations") or [])
                  if d.get("id") and d.get("est_steps_measured")}
    if not structured:
        structured = {k: int(v) for k, v in
                      (report.get("est_steps_by_deg") or {}).items() if v}
    if structured:
        missing = [d for d in declared if d not in structured]
        if missing:
            return (structured, f"solvability (structured, incomplete: no "
                                f"measurement for {missing})", False)
        breakdown = sum(structured.values())
        if not _agrees(breakdown, total):
            return (structured,
                    f"solvability (structured, {breakdown} steps does not "
                    f"agree with the probe's own total {total})", False)
        return ({d: structured[d] for d in declared},
                "solvability (structured)", True)

    blobs = [t for t in ([report.get("est_steps_note")]
                         + list(report.get("notes") or []))
             if isinstance(t, str) and "step" in t.lower()]
    best: dict[str, int] = {}
    why = "no per-degradation breakdown in solvability.json"
    for blob in blobs:
        found: dict[str, int] = {}
        marks = list(_STEP_DEG_RE.finditer(blob))
        for n, mark in enumerate(marks):
            end = marks[n + 1].start() if n + 1 < len(marks) else len(blob)
            number = _STEP_N_RE.search(blob, mark.end(), end)
            if number and mark.group(1) not in found:
                found[mark.group(1)] = int(next(g for g in number.groups() if g))
        found = {d: n for d, n in found.items() if d in declared}
        if len(found) <= len(best):
            continue
        best, why = found, "solvability (prose, incomplete)"
        if not declared or any(d not in found for d in declared):
            continue
        if not _agrees(sum(found.values()), total):
            why = (f"solvability (prose, {sum(found.values())} steps does not "
                   f"agree with the probe's own total {total})")
            continue
        return ({d: found[d] for d in declared},
                f"solvability (prose, {sum(found.values())} vs the probe's own "
                f"total {total})", True)
    return best, why, False


def _asset_digests(root: Path) -> list[str]:
    folder = root / "assets"
    if not folder.is_dir():
        return []
    return sorted({_sha(p.read_bytes()) for p in sorted(folder.rglob("*"))
                   if p.is_file()})


def _geometry_policies(root: Path) -> dict[str, dict]:
    """Policies measured by materialisation, keyed by stable component id.

    Materialisation precedes scoring, so the anchor audit is the authoritative
    record of whether a coordinate came from exact numbers, deck structure, or
    a finite-resolution reference render.  Missing/old manifests deliberately
    produce no policy and therefore retain the historical exact comparator.
    """
    path = root / "assets" / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for item in (manifest.get("anchors") or {}).get("anchored") or []:
        policy = item.get("geometry_policy")
        if item.get("id") and isinstance(policy, dict):
            out[item["id"]] = policy
    return out


def build_plan(deck, *, write: bool = True, init_slide_of=None) -> dict:
    """Turn one deck's record of damage into a scoring plan.

    Deterministic: the same files produce the same plan, byte for byte.
    """
    # `pathlib.Path` has a `.root` attribute of its own ("/"), so a plain
    # `getattr(deck, "root", deck)` turns every path argument into the
    # filesystem root and every read into a FileNotFoundError one frame away
    # from where the mistake was made.
    root = Path(deck) if isinstance(deck, (str, Path)) else Path(deck.root)
    delta = json.loads((root / "delta.json").read_text(encoding="utf-8"))
    task = json.loads((root / "task.json").read_text(encoding="utf-8"))
    steps = _est_steps(root, task)
    declared = [d["id"] for d in (task.get("degradations") or []) if d.get("id")]

    rejected: list[str] = []
    components: list[dict] = []
    # Every page and shape the recipe really damaged, recorded before anything
    # is dropped.  The gates ask "was this broken?", which is not the same
    # question as "is this scored?" — reading the damage off the surviving
    # components turned three genuinely damaged deck0004 pages into "untouched
    # pages the agent meddled with" and charged 0.30 for repairing them.
    damage: dict[str, Any] = {"slides": [], "paths": {}, "boxes": {}}
    for number, (index, entry) in enumerate(_entries(delta), start=1):
        spec = {k: v for k, v in entry.items() if k not in _BULK}
        if index not in damage["slides"]:
            damage["slides"].append(index)
        if entry.get("path") not in (None, "-"):
            damage["paths"].setdefault(str(index), []).append(entry["path"])
        if entry.get("box") and len(entry["box"]) == 4:
            damage["boxes"].setdefault(str(index), []).append(entry["box"])
        components.append({
            "id": f"c{number:03d}",
            "deg": entry.get("deg"),
            "op": entry["op"],
            "slide": index,
            "gt_path": entry.get("path") if entry.get("path") not in (None, "-") else None,
            "weight": 0.0,
            "spec": spec,
        })

    gt_inv = inventory_pptx(root / "source.pptx")
    init_inv = inventory_pptx(root / "input.pptx")
    geometry_policies = _geometry_policies(root)
    for component in components:
        if component["id"] in geometry_policies:
            component["geometry_policy"] = geometry_policies[component["id"]]

    # --- can the answer even satisfy it? ----------------------------------- #
    # A component the *ground truth itself* cannot pass is unsatisfiable, and
    # an unsatisfiable component is worse than a missing one: it silently
    # takes points off work that was done correctly.  So it is dropped and
    # recorded by name, not left in to punish.  The common cause is a run
    # property the answer inherits — see `_facet_run_props`.
    perfect = Scene(gt_inv, gt_inv)
    unscoreable = []
    for component in list(components):
        raw, why = _run_component(component, perfect)
        if raw < 1.0:
            unscoreable.append({"id": component["id"], "deg": component.get("deg"),
                                "op": component["op"], "slide": component["slide"] + 1,
                                "gt_scores": round(raw, 4), "why": why})
            components.remove(component)

    # --- traceability, both directions ------------------------------------- #
    missing_deg = [c["id"] for c in components if not c.get("deg")]
    if missing_deg:
        rejected.append(
            f"{len(missing_deg)} delta entr(ies) carry no `deg` "
            f"({missing_deg[0]}…): scoring work nobody asked for")
    unknown = sorted({c["deg"] for c in components
                      if c.get("deg") and c["deg"] not in declared})
    if unknown:
        rejected.append(f"delta names degradation(s) the task does not: {unknown}")
    owned = {c["deg"] for c in components if c.get("deg")}
    orphan = [d for d in declared if d not in owned]
    if orphan:
        rejected.append(f"degradation(s) with no scoreable component: {orphan} "
                        f"(asking for work nobody scores)")
    no_steps = [d for d in declared if not steps.get(d)]
    if no_steps and not missing_deg:
        rejected.append(f"no est_steps for degradation(s): {no_steps}")

    # --- weights ----------------------------------------------------------- #
    by_deg: dict[Any, list[dict]] = {}
    for component in components:
        by_deg.setdefault(component.get("deg"), []).append(component)

    # A degradation forfeits the share of its work that turned out to be
    # unscoreable.  It used to keep it: the share was divided by
    # `len(members)`, and `members` is the *survivors*, so every component
    # dropped into `unscoreable` above handed its weight to the siblings that
    # stayed.  On deck0004 six of `d5`'s nine components are dropped (the
    # ground truth inherits the fonts rather than stating them) and the three
    # that remain carried the whole 0.1250 — **an agent that fixed the three
    # fills and none of the six fonts scored 100% of d5**.  deck0006's `d5` is
    # the same shape, 7 of 56.
    #
    # Dropping the component is right (a rubric its own answer cannot satisfy
    # punishes correct work) and so is the rule it breaks: work nobody can earn
    # must not become free marks.  The share is scaled by the fraction of the
    # degradation's components that survive and the remainder falls to the
    # renormalisation below, where it is shared out over the *other*
    # degradations — legitimate work that is still asked for and still scored.
    # Components-surviving is the same proxy for "how much of this job"
    # already used to split a degradation's weight among its members, so
    # nothing new is being assumed.
    #
    # It cannot instead be left unreachable: `score_task` requires the ground
    # truth to total exactly 1.000, and a plan with a hole in it does not.
    dropped: dict[Any, int] = {}
    for item in unscoreable:
        if item.get("deg"):
            dropped[item["deg"]] = dropped.get(item["deg"], 0) + 1
    scoreable: dict[Any, float] = {
        deg: len(members) / float(len(members) + dropped.get(deg, 0))
        for deg, members in by_deg.items()}

    # Reward is distributed by how much work each degradation is, and until now
    # that was the proposer's *declaration* — a number nothing validated, and
    # one the pipeline's own solvability probe independently measures to be
    # wrong by up to 8x.  Where a measurement exists it wins; the declaration
    # is the fallback, not the source.  See `_measured_steps`.
    measured, measured_why, measured_ok = _measured_steps(root, declared)

    # The declaration is **two numbers**, and nothing has ever compared them.
    # `task.json`'s `est_steps` is the headline: it is what the probe is shown
    # and reports back as `est_steps_declared`, and it is what the gate prints
    # when it refuses. The per-degradation `est_steps` in `proposal.json` are the
    # breakdown, and they are what reward is actually apportioned by.
    # `check_proposal` computes the sum, records it as `sum_of_parts`, and
    # never compares it; `check_reconcile` does not look at `est_steps` at all,
    # so reconcile may rewrite the headline and leave the parts where they lay.
    # Measured on the ten-deck corpus: **9 of 10 have a headline that is not
    # the sum of its own parts** — deck0006 (parts 380, headline 285) and
    # deck0007 (390 / 280) both shipped with runtime declarations that
    # understated their own breakdown and the probe's measurement (310 / 330).
    #
    # deck0010 is the case that exposed it: the gate refused quoting "measured
    # 480, declared 280", and 280 was a headline the weights had never once
    # read — the parts summed to 255 at the time.  The ratio in the complaint
    # was 480/280 = 1.71x; the like-for-like ratio between the two breakdowns
    # was 480/255 = 1.88x.  A gate that reports a number nothing downstream
    # consumes is reporting about the wrong document.
    parts = sum(v for v in steps.values() if v) or None
    # Only meaningful against a breakdown: with no `proposal.json` to read,
    # `est_steps` is a lone number agreeing with nothing, not a contradiction.
    headline = _num_or_none(task.get("est_steps")) if parts else None
    weight_check: dict[str, Any] = {
        "declared": {d: steps.get(d) for d in declared},
        "declared_parts": parts,
        "declared_total": headline,
        "declared_split": (round(max(parts, headline) / float(min(parts, headline)), 3)
                           if parts and headline else None),
        "measured": measured or None,
        "measured_from": measured_why,
        "measured_usable": measured_ok,
        "disagreement": {}, "worst": None,
    }
    for deg in declared:
        a, b = steps.get(deg) or 0, measured.get(deg) or 0
        if a > 0 and b > 0:
            ratio = round(max(a, b) / float(min(a, b)), 3)
            weight_check["disagreement"][deg] = ratio
            if weight_check["worst"] is None or ratio > weight_check["worst"]:
                weight_check["worst"] = ratio

    usable = {deg: steps.get(deg, 0) for deg in by_deg if deg}
    if measured_ok and all(measured.get(deg) for deg in usable) and not missing_deg:
        usable = {deg: measured[deg] for deg in usable}
        source = "steps_measured"
    elif usable and all(v > 0 for v in usable.values()) and not missing_deg:
        source = "est_steps"
    else:
        usable = {}
    if usable:
        scale = float(sum(usable.values()))
        for deg, members in by_deg.items():
            share = usable[deg] / scale * scoreable[deg]
            for component in members:
                component["weight"] = share / len(members)
    else:
        source = "equal (est_steps unusable)"
        for component in components:
            component["weight"] = 1.0 / len(components) if components else 0.0
    drift = sum(c["weight"] for c in components)
    if drift:
        for component in components:
            component["weight"] = component["weight"] / drift
    weight_check["source"] = source

    # The backstop for the case the preference above cannot fix: a measurement
    # exists, it contradicts the declaration by more than a factor of
    # `STEP_DISAGREEMENT`, and it is not complete enough to weight by — so the
    # plan would be distributing reward by a number it has been told is wrong,
    # which is the whole defect.  Unreachable on the ten-deck corpus, where
    # every measurement that exists is complete and self-consistent; it is here
    # because a probe that emits a *partial* structured breakdown is the shape
    # this arrives in next, and it must not pass silently.
    if (source != "steps_measured" and weight_check["worst"]
            and weight_check["worst"] > STEP_DISAGREEMENT):
        rejected.append(
            f"weights come from the declared est_steps, and the solvability "
            f"probe measures the same work at up to {weight_check['worst']}x "
            f"different ({weight_check['disagreement']}) without measuring "
            f"enough of it to weight by ({measured_why}) — reward would be "
            f"distributed by a number the pipeline has already contradicted")

    # And the case no measurement can arbitrate: the weights come from the
    # breakdown, and the breakdown does not add up to the headline the same
    # declaration states.  There is then no third number to prefer, and the two
    # that exist cannot both be the size of this task — so the reward is being
    # split by a document that contradicts itself about how much work it is.
    # This does **not** fire when a measurement won: the weights are then the
    # probe's, the headline is merely stale, and refusing would send a deck
    # back to `recipe` to fix an arithmetic slip in a field the weights never
    # read.  That case is recorded in `weight_check` for `consistency` instead.
    if (source == "est_steps" and weight_check["declared_split"]
            and not _agrees(parts, headline)):
        rejected.append(
            f"weights come from the declared est_steps, whose parts sum to "
            f"{parts} while the same task declares a total of {headline} "
            f"({weight_check['declared_split']}x, past the "
            f"{DECLARATION_SPLIT:.0%} band) — the probe's "
            f"`est_steps_declared` is read off the total, the reward is split "
            f"by the parts, and nothing else compares them")

    # A mapping that cannot be replayed is not a mapping to fall back on the
    # identity for: the identity is a *claim* that no page moved, and the
    # record has just said one did.
    resolve_slide = init_slide_of or _init_slide_of
    try:
        slide_of = resolve_slide(delta, len(gt_inv["slides"]))
    except Unscorable as err:
        slide_of = None
        rejected.append(f"cannot map the broken file's pages back onto the "
                        f"answer's, so no floor can be measured: {err}")

    plan = {
        "format": PLAN_FORMAT,
        "deck": root.name,
        "task": task.get("name"),
        "pos_tol_emu": POS_TOL,
        "weight_source": source,
        "weight_check": weight_check,
        "assets_sha": _asset_digests(root),
        "init_slide_of": slide_of,
        "damage": damage,
        "degradations": [
            {"id": deg, "est_steps": steps.get(deg),
             "est_steps_measured": measured.get(deg),
             "weight": round(sum(c["weight"] for c in by_deg.get(deg, [])), 9),
             "components": [c["id"] for c in by_deg.get(deg, [])],
             "components_unscoreable": dropped.get(deg, 0),
             "share_forfeited": round(1.0 - scoreable.get(deg, 1.0), 6)}
            for deg in declared],
        "components": components,
        "unscoreable": unscoreable,
        "rejected": rejected,
    }

    # --- floors, measured, not assumed ------------------------------------- #
    broken = Scene(gt_inv, init_inv, plan.get("init_slide_of"))
    hot = []
    for component in components:
        floor, why = _run_component(component, broken)
        component["floor"] = round(floor, 6)
        component["floor_why"] = why
        if floor > FLOOR_LIMIT:
            hot.append(f"{component['id']}/{component['op']} floor={floor:.2f}")
    if hot:
        rejected.append(
            "component floor above %.2f — send the task back to `recipe`, do "
            "not widen a tolerance: %s" % (FLOOR_LIMIT, ", ".join(hot)))

    # --- duplicated components --------------------------------------------- #
    # Three duplicated components once carried 21% of one task's weight.  Two
    # entries for the same operator on the same shape with the same parameters
    # are one piece of work counted twice, and the weight follows the count.
    fingerprints: dict[str, str] = {}
    duplicates = []
    for component in components:
        spec = {k: v for k, v in component["spec"].items() if k != "deg"}
        key = json.dumps([component["op"], component["slide"],
                          component.get("gt_path"), spec],
                         sort_keys=True, ensure_ascii=False, default=str)
        if key in fingerprints:
            duplicates.append(f"{component['id']}=={fingerprints[key]}")
        else:
            fingerprints[key] = component["id"]
    if duplicates:
        rejected.append(f"duplicate component(s) counted twice: {duplicates}")

    # --- work the instruction excuses --------------------------------------- #
    # The rule is *the plan may not score something the instruction says is not
    # being asked for*.  It has to be refused here rather than reported
    # anywhere else, because `rejected` is the only channel that stops a deck:
    # `score`'s `plan_accepted` gate reads it and `pipeline.score_task` marks
    # the deck rejected on it.  Detection lives in `consistency`, which owns
    # instruction parsing, its lexicon and its sentence splitter.
    #
    # The import is function-local **because `emit` pastes this module verbatim
    # into the task file** and only `inventory` is embedded alongside it.  That
    # costs nothing: `build_plan` reads `delta.json`, `task.json`,
    # `proposal.json` and two `.pptx` off the disk, so it could never have run
    # in the emitted evaluator, which calls only `score`.
    from .consistency import excused_components
    for hit in excused_components(task.get("instruction") or "", components):
        where = ("the whole deck" if hit["slides"] is None else
                 "slide(s) " + ", ".join(str(s) for s in hit["slides"]))
        rejected.append(
            f"the instruction excuses {hit['bucket']} work on {where} "
            f"({hit['cue']!r}) and the plan scores {hit['components']} for it, "
            f"worth {hit['weight']:.4f} — an obedient agent tops out at "
            f"{1.0 - hit['weight']:.4f}; drop the components or drop the "
            f"sentence")

    # --- coherence: no gate may fire on work that a component rewards ------- #
    plan["coherence"] = _coherence(plan, gt_inv, init_inv)
    for failure in plan["coherence"]["failures"]:
        rejected.append(f"coherence: {failure}")

    if write:
        (root / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


# --------------------------------------------------------------------------- #
# coherence: a gate that fires on correct work is a defect in the task
# --------------------------------------------------------------------------- #
#
# Nothing in the pipeline caught an evaluator whose `gate_native_objects`
# zeroed a hand-built SmartArt while its own diagram component was awarding
# 0.7 for that same hand-built SmartArt.  A gate and a component in one file,
# disagreeing about whether the same state is correct, and the gate winning.
#
# So the plan is checked against states that are correct work by construction.
# A cheat gate that fires on any of them, or a scope penalty on the ground
# truth, is not a tolerance to tune: it **rejects the task**, exactly as a
# successful attack does.


_mock_shape = C._mock_shape
_state_half = C._state_half
_state_rebuilt = C._state_rebuilt
_state_over_eager = C._state_over_eager
_state_position_slip = C._state_position_slip

def _coherence(plan, gt_inv, init_inv) -> dict:
    """Every failure here is a work order somebody has to act on, so **one
    root cause is reported once**.

    deck0008 produced five lines from one event.  `media_not_pasted` fires on
    all four states for the same reason — the deck deliberately withholds two
    original bitmaps, so the *ground truth itself* holds blobs the gate calls
    intruders — and each state filed its own near-identical line.  The fifth
    was worse than redundant: it read *"over-eagerness alone zeroes the score;
    a scope violation must cost a fraction, never everything"*, which is false.
    Scope violations are penalties capped at `PENALTY_CAP` and cannot zero
    anything; `over_eager` scored 0 because the **gate** did, and the check was
    reading the post-gate `score`.  The work order a repairer read was four
    parts noise and one part misdirection.
    """
    states = [("ground_truth", gt_inv),
              ("half_restore", _state_half(plan, gt_inv, init_inv)),
              ("rebuilt_by_hand", _state_rebuilt(plan, gt_inv)),
              ("over_eager", _state_over_eager(plan, gt_inv)),
              # diagnostic, not correct work: see `_state_position_slip`
              ("position_slip", _state_position_slip(plan, gt_inv))]
    report: dict[str, Any] = {"states": {}, "failures": []}
    gated: dict[tuple[str, str], list[str]] = {}
    for name, inv in states:
        if inv is None:
            continue
        result = score({**plan, "rejected": []}, inv, gt_inv, init_inv)
        report["states"][name] = {"score": result["score"],
                                  "unweighted": result["unweighted"],
                                  "failed_gate": result["failed_gate"],
                                  "penalty": result["penalty"]}
        if name == "position_slip":
            continue
        if result["failed_gate"]:
            key = (result["failed_gate"],
                   result["gate_reasons"][result["failed_gate"]])
            gated.setdefault(key, []).append(name)
        if name == "ground_truth":
            if result["penalty"]:
                report["failures"].append(
                    f"the ground truth is itself penalised "
                    f"({result['scope_violations']})")
            if abs(result["unweighted"] - 1.0) > 1e-6:
                report["failures"].append(
                    f"the ground truth scores {result['unweighted']:.3f}, not 1.0")
        # only the *penalty* is on trial here: a gate that zeroed this state is
        # already reported above, once, and reporting it again as a scope
        # defect sends the reader to the wrong half of the module.
        if (name == "over_eager" and not result["failed_gate"]
                and result["score"] <= 0.0):
            report["failures"].append(
                "over-eagerness alone zeroes the score; a scope violation must "
                "cost a fraction, never everything")
    for (gate, why), names in gated.items():
        report["failures"].append(
            f"{gate} fires on {', '.join(f'`{n}`' for n in names)}, which is "
            f"correct work: {why}")
    return report


def _init_slide_of(delta: dict, n_pages: int) -> list[int | None] | None:
    """gt slide index -> the index the same page has in the broken file.

    `None` means the identity, which is what every deck that moves no page
    gets; `None` *inside* the list means the page is not in the broken file at
    all, which `Scene.slide` already reads as "absent".

    Only a deck-level `delete_slides` or `reorder_slides` can make this
    anything but the identity, and neither appears in the ten decks.  It is
    still measured rather than assumed, because the version that returned
    `None` after working the answer out handed a deck that *did* move a page
    the identity mapping anyway — and then every floor on that deck is measured
    against the wrong page, silently, with nothing in the plan to show for it.

    The mapping is **replayed in the order `degrade_exec.run` applies the
    edits** — every deletion first, each page number read against the deck as
    it stands at that moment, then the swaps against what deletion left.  Both
    are position-based and destructive, so reading them any other way gets the
    wrong page as soon as there are two of them: deleting pages 2 and 5 of six
    removes the original pages 2 and 6.
    """
    deleted = [int(p) for p in (delta.get("deleted_slides") or [])]
    swaps = ((delta.get("reorder_slides") or {}).get("swapped")) or []
    if not deleted and not swaps:
        return None
    order = list(range(n_pages))          # broken-file position -> gt index
    for page in deleted:
        if not 1 <= page <= len(order):
            raise Unscorable(f"the record deletes page {page} of a deck that "
                             f"has {len(order)} at that point")
        order.pop(page - 1)
    for pair in swaps:
        a, b = int(pair[0]), int(pair[1])
        if not (1 <= a <= len(order) and 1 <= b <= len(order)):
            raise Unscorable(f"the record swaps pages {a} and {b} of a deck "
                             f"that has {len(order)} at that point")
        order[a - 1], order[b - 1] = order[b - 1], order[a - 1]
    out: list[int | None] = [None] * n_pages
    for position, index in enumerate(order):
        out[index] = position
    return out


# --------------------------------------------------------------------------- #
