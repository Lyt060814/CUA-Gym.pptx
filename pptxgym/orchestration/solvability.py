"""Mechanical half of the solvability witness contract."""

from __future__ import annotations


E_ITEMS = ("E1", "E2", "E3", "E4", "E5", "E6")
VERDICTS = ("solvable", "undetermined", "leaked", "ambiguous", "overdetermined")
REWORK_STAGES = ("proposed", "recipe", "materialise")
DEG_KEYS = ("id", "end_state", "checks", "determinate", "rivals",
            "undetermined", "tolerance", "est_steps_measured",
            "overdetermined")
OUT_OF_BUNDLE = frozenset({
    "source.pptx", "delta.json", "recipe.json", "proposal.json", "digest.json",
    "digest_min.json", "task.json", "plan.json", "solvability.json",
    "renders", "compare", "attempts",
})
STEP_BAND = 0.25


def _num(value):
    return (value if isinstance(value, (int, float))
            and not isinstance(value, bool) else None)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def verdict_from_findings(report: dict) -> tuple[str, int, str]:
    """Walk the first-match-wins Pass 5 table over the findings."""
    degradations = [item for item in (report.get("degradations") or [])
                    if isinstance(item, dict)]
    if report.get("leaks"):
        return "leaked", 1, "`leaks` is non-empty"
    for item in degradations:
        if not item.get("determinate") and not (item.get("rivals") or []):
            return ("undetermined", 2,
                    f"{item.get('id')} is `determinate: false` with no `rivals`")
    for item in degradations:
        if item.get("rivals"):
            return "ambiguous", 3, f"{item.get('id')} names rivals"
    for item in degradations:
        if item.get("overdetermined"):
            return ("overdetermined", 4,
                    f"{item.get('id')} is `overdetermined: true`")
    return ("solvable", 5,
            "no leak, no unenumerable gap, no rivals, nothing overdetermined")


def _leak_location_problem(where: str) -> str:
    token = _text(where).replace("\\", "/").split()
    if not token:
        return ""
    parts = [part for part in token[0].strip("`\"'(),.").split("/") if part]
    if parts and parts[0] == "bundle":
        parts = parts[1:]
    if parts and parts[0] == "assets":
        return ("points into `bundle/assets/`, which the skill says is never a "
                "leak — a shipped asset is either Pass 5's `overdetermined` "
                "question or a `rework` note against `materialise`")
    hit = next((part for part in parts if part.lower() in OUT_OF_BUNDLE), "")
    if hit:
        return (f"points at `{hit}`, which is outside `bundle/` — a solver "
                f"never sees it, so it cannot leak; that is a `rework` note")
    return ""


def _degradation_problems(degradation, index: int) -> list[str]:
    if not isinstance(degradation, dict):
        return [f"degradation #{index + 1} is not an object"]
    who = _text(degradation.get("id")) or f"degradation #{index + 1}"
    out: list[str] = []

    missing = [key for key in DEG_KEYS if key not in degradation]
    if missing:
        out.append(f"{who} has no {', '.join('`%s`' % key for key in missing)} — "
                   f"every degradation carries all of {', '.join(DEG_KEYS)}")

    if not _text(degradation.get("end_state")):
        out.append(f"{who} has no end_state")

    checks = degradation.get("checks")
    if "checks" in degradation:
        if not isinstance(checks, dict):
            out.append(f"{who}: `checks` is not an object")
        else:
            gone = [item for item in E_ITEMS if item not in checks]
            if gone:
                out.append(f"{who}: `checks` is missing {', '.join(gone)} — "
                           f"write all six, empty string for the ones that "
                           f"did not hit")
            bad = [key for key in checks if key not in E_ITEMS]
            if bad:
                out.append(f"{who}: `checks` has {', '.join(sorted(bad))}, "
                           f"which is not one of {', '.join(E_ITEMS)}")
            if any(not isinstance(value, str) for value in checks.values()):
                out.append(f"{who}: every `checks` value is a citation or an "
                           f"empty string")

    determinate = degradation.get("determinate")
    if "determinate" in degradation and not isinstance(determinate, bool):
        out.append(f"{who}: `determinate` is {determinate!r}, not true or false")
    rivals = degradation.get("rivals") or []
    if "rivals" in degradation and not isinstance(degradation.get("rivals"), list):
        out.append(f"{who}: `rivals` is not a list")
        rivals = []
    caveat = _text(degradation.get("undetermined"))

    if determinate is True:
        if caveat:
            out.append(f"{who} is `determinate: true` with a non-empty "
                       f"`undetermined` — that caveat is either a tolerance or a "
                       f"gap, and Pass 2 has a field for each")
        if rivals:
            out.append(f"{who} is `determinate: true` and names rivals — "
                       f"rivals are competing end states, so it is not "
                       f"determinate")
        if not _text(degradation.get("evidence")):
            out.append(f"{who} is called determinate with no `evidence` — "
                       f"that is a guess wearing a verdict")
        if isinstance(checks, dict) and not any(_text(value)
                                                for value in checks.values()):
            out.append(f"{who} is called determinate with every E1-E6 empty — "
                       f"nothing pins the end state")
    elif determinate is False and not rivals and not caveat:
        out.append(f"{who} is `determinate: false` with no `rivals` and an "
                   f"empty `undetermined` — say which part is not pinned and "
                   f"why")

    for number, tolerance in enumerate(degradation.get("tolerance") or []):
        if not isinstance(tolerance, dict):
            out.append(f"{who}: tolerance #{number + 1} is not an object")
            continue
        if _text(tolerance.get("rule")).upper() not in ("T1", "T2"):
            out.append(f"{who}: tolerance #{number + 1} names rule "
                       f"{tolerance.get('rule')!r} — a gap is a tolerance under "
                       f"T1 or T2 and under nothing else")
        if not _text(tolerance.get("what")) or not _text(tolerance.get("why")):
            out.append(f"{who}: tolerance #{number + 1} needs `what` and a `why` "
                       f"that cites what makes it one")

    steps = _num(degradation.get("est_steps_measured"))
    if "est_steps_measured" in degradation and (steps is None or steps <= 0):
        out.append(f"{who}: `est_steps_measured` is "
                   f"{degradation.get('est_steps_measured')!r} — the scoring "
                   f"stage reads that field and weights the reward by it")
    if "overdetermined" in degradation and not isinstance(
            degradation.get("overdetermined"), bool):
        out.append(f"{who}: `overdetermined` is "
                   f"{degradation.get('overdetermined')!r}, not true or false")
    return out


def solvability_rubric_problems(report: dict) -> list[str]:
    """Return every machine-decidable breach of the witness contract."""
    if not isinstance(report, dict):
        return ["solvability.json is not an object"]

    shape: list[str] = []
    rules: list[str] = []
    verdict = report.get("verdict")
    if verdict not in VERDICTS:
        return [f"unknown verdict {verdict!r}"]
    if not _text(report.get("verdict_reason")):
        shape.append("no `verdict_reason` — one sentence, naming the table "
                     "line that decided it")

    degradations = report.get("degradations")
    if not degradations or not isinstance(degradations, list):
        return ["no per-degradation findings"]
    for index, degradation in enumerate(degradations):
        shape += _degradation_problems(degradation, index)

    for index, leak in enumerate(report.get("leaks") or []):
        who = f"leak #{index + 1}"
        if not isinstance(leak, dict):
            shape.append(f"{who} is not an object")
            continue
        if not _text(leak.get("what")) or not _text(leak.get("where")):
            shape.append(f"{who} needs `what` and `where`")
        if not _text(leak.get("load_bearing")):
            shape.append(f"{who} states no `load_bearing` reason — a finding "
                         f"that closes no gap and eliminates no rival is "
                         f"`residue`, not a leak")
        if problem := _leak_location_problem(leak.get("where")):
            rules.append(f"{who} {problem}")

    for index, residue in enumerate(report.get("residue") or []):
        if not isinstance(residue, dict) or not _text(residue.get("what")) \
                or not _text(residue.get("why_not_a_leak")):
            shape.append(f"residue #{index + 1} needs `what` and "
                         f"`why_not_a_leak` — the second is what makes it "
                         f"residue and not a leak nobody wrote up")

    rework = report.get("rework") or []
    for index, item in enumerate(rework):
        if not isinstance(item, dict):
            shape.append(f"rework #{index + 1} is not an object")
            continue
        if item.get("stage") not in REWORK_STAGES:
            shape.append(f"rework #{index + 1} targets "
                         f"{item.get('stage')!r} — the pipeline re-runs "
                         f"one of {', '.join(REWORK_STAGES)}")
        elif not _text(item.get("what")):
            shape.append(f"rework #{index + 1} says nothing in `what`")

    measured = _num(report.get("est_steps_measured"))
    declared = _num(report.get("est_steps_declared"))
    if measured is None:
        shape.append("no top-level `est_steps_measured`")
    if declared is None:
        shape.append("no `est_steps_declared`")
    per_degradation = [_num(item.get("est_steps_measured"))
                       for item in degradations if isinstance(item, dict)]
    if measured is not None and len(per_degradation) == len(degradations) \
            and all(value is not None for value in per_degradation):
        total = sum(per_degradation)
        if abs(total - measured) > 0.5:
            rules.append(f"top-level `est_steps_measured` is {measured} but "
                         f"the degradations add up to {total} — the top-level "
                         f"value is the sum of them")
    if measured is not None and declared not in (None, 0):
        off = abs(measured - declared) / abs(declared)
        told = any(isinstance(item, dict) and item.get("stage") == "proposed"
                   for item in rework)
        if off > STEP_BAND and not told:
            rules.append(f"measured {measured} steps against a declared "
                         f"{declared} — {off:.0%} out, past the "
                         f"{STEP_BAND:.0%} band, and no `rework` entry against "
                         f"`proposed` says so")

    if verdict != "solvable" and not rework:
        rules.append(f"verdict {verdict!r} with no `rework` — the pipeline "
                     f"uses it to decide what to re-run")
    wanted, line, why = verdict_from_findings(report)
    if wanted != verdict:
        rules.append(f"verdict is {verdict!r}, but line {line} of the Pass 5 "
                     f"table matches these findings and says {wanted!r}: "
                     f"{why}. First line that matches wins; if the verdict is "
                     f"the right one then a finding is misfiled")
    return shape + rules
