"""Domain model and registries for evaluator hardening attacks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class Unconstructible(RuntimeError):
    """An applicable attack could not be built, leaving its gate unproven."""


class NoMaterial(Unconstructible):
    """The deck has no material on which a particular check can operate."""


class ScorerUnavailable(RuntimeError):
    """The comparison engine is absent or exposes no plan builder."""


@dataclass(frozen=True)
class Expect:
    kind: str
    lo: float = 0.0
    hi: float = 0.0
    tol: float = 0.0
    ref: str | None = None
    gap: float = 0.0

    def label(self) -> str:
        if self.kind == "exact":
            return f"= {self.lo:.3f}"
        if self.kind == "at_most":
            return f"<= {self.hi:.3f}"
        if self.kind == "between":
            return f"{self.lo:.2f}..{self.hi:.2f}"
        if self.kind == "costs":
            return f"<= {self.ref} − {self.gap:.2f}"
        if self.kind == "nothing_already_right":
            return f"= 0.000, pre-floor <= {self.hi:.3f}"
        return f"<= {self.ref}"

    def check(self, score: float, others: dict[str, float],
              result: dict | None = None) -> tuple[bool, str]:
        if self.kind == "exact":
            ok = abs(score - self.lo) <= self.tol
            return ok, "" if ok else f"{score:.3f} != {self.lo:.3f}"
        if self.kind == "at_most":
            ok = score <= self.hi + 1e-9
            return ok, "" if ok else f"{score:.3f} > {self.hi:.3f}"
        if self.kind == "between":
            ok = self.lo - 1e-9 <= score <= self.hi + 1e-9
            return ok, "" if ok else (
                f"{score:.3f} outside {self.lo:.2f}..{self.hi:.2f}")
        if self.kind == "no_gain":
            base = others.get(self.ref)
            if base is None:
                return False, f"reference attack {self.ref} did not produce a score"
            ok = score <= base + 1e-6
            return ok, "" if ok else f"{score:.3f} > {self.ref} {base:.3f}"
        if self.kind == "costs":
            base = others.get(self.ref)
            if base is None:
                return False, f"reference attack {self.ref} did not produce a score"
            if base < self.gap:
                return False, (
                    f"{self.ref} itself scored {base:.3f}, so nothing can cost "
                    f"{self.gap:.2f} against it — this row cannot be judged "
                    f"until {self.ref} is right, and it is not evidence that "
                    "collateral damage is punished")
            ok = score <= base - self.gap + 1e-6
            return ok, "" if ok else (
                f"{score:.3f} > {self.ref} {base:.3f} − {self.gap:.2f}: this cost "
                f"{base - score:.3f} of the reward and the battery requires at "
                f"least {self.gap:.2f} — the penalty policy it depends on has "
                "been turned off or turned down to nothing")
        if self.kind == "nothing_already_right":
            return self._nothing_already_right(score, result)
        raise ValueError(self.kind)

    def _nothing_already_right(self, score: float,
                               result: dict | None) -> tuple[bool, str]:
        if abs(score) > 1e-9:
            return False, (f"{score:.3f} != 0.000 — the broken file scores, "
                           "which the floor subtraction should have made "
                           "impossible")
        if result is None or result.get("components") is None:
            return False, ("scored 0.000, which is an identity here (the floor "
                           "is measured from this very candidate) — and no "
                           "per-component result came back, so there is nothing "
                           "underneath it to check")
        already = [component for component in result["components"]
                   if float(component.get("raw") or 0.0) > 0.0]
        earned = sum(float(component.get("weight") or 0.0)
                     * float(component.get("raw") or 0.0)
                     for component in already)
        if earned <= self.hi + 1e-9:
            return True, ""
        worst = sorted(
            already,
            key=lambda component: -float(component.get("weight") or 0.0)
            * float(component.get("raw") or 0.0),
        )[:3]
        return False, (
            f"the broken file already satisfies {earned:.3f} of the reward "
            f"before the floor subtracts it (limit {self.hi:.3f}): "
            + "; ".join(
                f"{component.get('deg') or component.get('id')}/"
                f"{component.get('op')} raw "
                f"{float(component.get('raw') or 0):.2f}×"
                f"{float(component.get('weight') or 0):.2f} "
                f"({str(component.get('why'))[:60]})"
                for component in worst)
            + " — doing nothing pays there, and `noop` scoring 0.000 hides it")


def Exact(value: float, tol: float = 1e-9) -> Expect:
    return Expect("exact", lo=value, tol=tol)


def AtMost(value: float) -> Expect:
    return Expect("at_most", hi=value)


def Between(lo: float, hi: float) -> Expect:
    return Expect("between", lo=lo, hi=hi)


def NoGain(ref: str) -> Expect:
    return Expect("no_gain", ref=ref)


def Costs(ref: str, gap: float) -> Expect:
    return Expect("costs", ref=ref, gap=gap)


def NothingAlreadyRight(limit: float = 0.0) -> Expect:
    return Expect("nothing_already_right", hi=limit)


@dataclass
class Attack:
    name: str
    what: str
    expect: Expect
    build: Callable[[Ctx, Path], "Built"]
    applies: Callable[[Ctx], str | None]
    order: int = 0


@dataclass
class Built:
    path: Path
    evidence: str
    facts: dict = field(default_factory=dict)


@dataclass
class LegitimateVariant:
    name: str
    what: str
    build: Callable[["Ctx", Path], "Built"]
    applies: Callable[["Ctx"], str | None]
    order: int = 0


ATTACKS: dict[str, Attack] = {}
LEGITIMATE_VARIANTS: dict[str, LegitimateVariant] = {}
_ATTACK_ORDER = [0]
_VARIANT_ORDER = [0]


def reset_registries() -> None:
    """Restore the same clean import state the former single module had."""
    ATTACKS.clear()
    LEGITIMATE_VARIANTS.clear()
    _ATTACK_ORDER[0] = 0
    _VARIANT_ORDER[0] = 0


def attack(name: str, what: str, expect: Expect, applies=lambda ctx: None):
    def wrap(function):
        _ATTACK_ORDER[0] += 1
        ATTACKS[name] = Attack(name, what, expect, function, applies,
                               _ATTACK_ORDER[0])
        return function
    return wrap


def legitimate_variant(name: str, what: str, applies=lambda ctx: None):
    def wrap(function):
        _VARIANT_ORDER[0] += 1
        LEGITIMATE_VARIANTS[name] = LegitimateVariant(
            name, what, function, applies, _VARIANT_ORDER[0])
        return function
    return wrap


@dataclass
class Row:
    attack: str
    what: str
    expect: str
    status: str
    score: float | None = None
    ok: bool | None = None
    note: str = ""
    evidence: str = ""
    detail: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)


@dataclass
class Report:
    deck: str
    components: list[str]
    rows: list[Row]
    plan_rejected: list[str] = field(default_factory=list)
    variants: list[Row] = field(default_factory=list)

    STATUSES = ("scored", "n/a", "no_material", "unconstructible", "error",
                "not_run", "built")

    @property
    def rejected(self) -> bool:
        return bool(self.reasons)

    def coverage(self) -> dict:
        def count(rows: list[Row]) -> dict:
            result = {status: 0 for status in self.STATUSES}
            for row in rows:
                result[row.status] = result.get(row.status, 0) + 1
            result["total"] = len(rows)
            return result

        attacks, variants = count(self.rows), count(self.variants)
        return {
            "attacks_total": attacks["total"],
            "attacks_scored": attacks["scored"],
            "attacks_na": attacks["n/a"],
            "attacks_no_material": attacks["no_material"],
            "attacks_unproven": attacks["unconstructible"] + attacks["error"]
            + attacks["not_run"],
            "attacks_not_scored": [f"{row.attack} ({row.status})"
                                    for row in self.rows
                                    if row.status != "scored"],
            "variants_total": variants["total"],
            "variants_scored": variants["scored"],
            "variants_na": variants["n/a"] + variants["unconstructible"]
            + variants["no_material"],
            "variants_error": variants["error"],
            "variants_not_scored": [f"{row.attack} ({row.status})"
                                     for row in self.variants
                                     if row.status != "scored"],
        }

    def coverage_line(self) -> str:
        coverage = self.coverage()
        missing = (coverage["attacks_not_scored"]
                   + coverage["variants_not_scored"])
        return (f"{coverage['attacks_scored']}/{coverage['attacks_total']} "
                "attacks and "
                f"{coverage['variants_scored']}/{coverage['variants_total']} "
                "legitimate variants were actually scored"
                + (f"; not scored: {', '.join(missing)}" if missing else ""))

    @property
    def reasons(self) -> list[str]:
        reasons = [f"the comparator rejects the plan: {why}"
                   for why in self.plan_rejected]
        if (not any(row.status == "scored" for row in self.rows)
                and any(row.status == "no_material" for row in self.rows)):
            reasons.append(
                "nothing in the battery could be scored on this deck: "
                + ", ".join(f"{row.attack} ({row.status})"
                            for row in self.rows[:6]))
        for row in self.rows:
            if (row.status == "scored" and row.ok is False
                    and row.attack != "half_restore"):
                reasons.append(f"{row.attack}: {row.note}")
        return reasons

    @property
    def warnings(self) -> list[str]:
        warnings = []
        for row in self.rows:
            if row.status == "no_material":
                continue
            if row.status == "unconstructible":
                warnings.append(f"{row.attack}: unproven gate — {row.note}")
            elif row.status == "not_run":
                warnings.append(f"{row.attack}: never fired — {row.note}")
            elif row.status == "error":
                warnings.append(f"{row.attack}: {row.note}")
            elif (row.status == "scored" and row.ok is False
                  and row.attack == "half_restore"):
                warnings.append(f"{row.attack}: {row.note}")
        for row in self.variants:
            if row.status == "error":
                warnings.append(f"variant {row.attack}: {row.note}")
            elif row.status == "scored" and row.ok is False:
                warnings.append(f"variant {row.attack}: {row.note}")
        return warnings
