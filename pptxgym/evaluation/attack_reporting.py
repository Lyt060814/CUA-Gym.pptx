"""Human-readable reports for hardening attack results."""

from __future__ import annotations

from .attack_model import Report, Row


def mark(row: Row) -> str:
    if row.status == "n/a":
        return "n/a"
    if row.status == "not_run":
        return "REJECT (never run)"
    if row.status in ("unconstructible", "error"):
        return "REJECT (unproven)"
    if row.status == "built":
        return "built"
    return "pass" if row.ok else "**FAIL**"


def variant_mark(row: Row) -> str:
    if row.status == "n/a":
        return "n/a"
    if row.status == "unconstructible":
        return "no material"
    if row.status == "error":
        return "**ERROR**"
    if row.status == "built":
        return "built"
    return "pass" if row.ok else "**FAIL (rejects the task)**"


def variant_table(report: Report, tolerance: float) -> list[str]:
    if not report.variants:
        return []
    lines = ["", "**legitimate variants** — the same answer reached another "
             "way.  Each must score within "
             f"{tolerance} of `gt` **and trip no hard gate**; a gate that "
             "fires here rejects the task exactly as a successful attack does.",
             "", "| variant | what it does | score | verdict | evidence |",
             "|---|---|---|---|---|"]
    for row in report.variants:
        score = "—" if row.score is None else f"{row.score:.3f}"
        detail = row.evidence or row.note
        if row.note and row.evidence:
            detail = f"{row.note} — {row.evidence}"
        lines.append(f"| `{row.attack}` | {row.what} | {score} | "
                     f"{variant_mark(row)} | {detail} |")
    return lines


def table(report: Report, tolerance: float) -> str:
    lines = [f"### {report.deck} — {len(report.components)} degradations "
             f"({', '.join(report.components)})", ""]
    if report.plan_rejected:
        lines += ["**The comparator rejects this task's plan outright** — "
                  "every candidate below, the ground truth included, scores "
                  "0.0 through the `plan_accepted` gate.  The table is "
                  "therefore produced with that one gate stood down, so the "
                  "attacks are actually exercised; the deck is rejected either "
                  "way.", ""]
        lines += [f"- {why}" for why in report.plan_rejected] + [""]
    lines += ["| attack | what it does | expect | score | verdict | evidence |",
              "|---|---|---|---|---|---|"]
    for row in report.rows:
        score = "—" if row.score is None else f"{row.score:.3f}"
        detail = row.evidence or row.note
        if row.note and row.evidence and row.status == "scored" and row.ok is False:
            detail = f"{row.note} — {row.evidence}"
        lines.append(f"| `{row.attack}` | {row.what} | {row.expect} | {score} "
                     f"| {mark(row)} | {detail} |")
    lines += variant_table(report, tolerance)
    lines += ["", f"coverage: {report.coverage_line()}", ""]
    if report.rejected:
        lines.append(f"**verdict: REJECT** — {'; '.join(report.reasons)}")
    else:
        lines.append("**verdict: survives the battery**")
        lines += [f"- warning: {warning}" for warning in report.warnings]
    lines.append("")
    return "\n".join(lines)


def summary(reports: list[Report]) -> str:
    kept = [report for report in reports if not report.rejected]
    counts: dict[str, int] = {}
    for report in reports:
        for row in report.rows:
            if (row.status in ("unconstructible", "error", "not_run")
                    or row.ok is False):
                counts[row.attack] = counts.get(row.attack, 0) + 1
    variant_counts: dict[str, int] = {}
    for report in reports:
        for row in report.variants:
            if row.status == "error" or row.ok is False:
                variant_counts[row.attack] = variant_counts.get(row.attack, 0) + 1
    coverage = [report.coverage() for report in reports]
    lines = [f"{len(kept)}/{len(reports)} decks survive the battery.", "",
             f"{sum(item['attacks_scored'] for item in coverage)}/"
             f"{sum(item['attacks_total'] for item in coverage)} attack cells and "
             f"{sum(item['variants_scored'] for item in coverage)}/"
             f"{sum(item['variants_total'] for item in coverage)} variant cells "
             "were actually scored — the rest found no material on their deck "
             "and say so per row.", "", "| attack | decks it rejects |",
             "|---|---|"]
    for name, count in sorted(counts.items(), key=lambda item: -item[1]):
        lines.append(f"| `{name}` | {count} |")
    if not counts:
        lines.append("| — | 0 |")
    lines += ["", "| legitimate variant | decks where correct work is not "
              "credited |", "|---|---|"]
    for name, count in sorted(variant_counts.items(), key=lambda item: -item[1]):
        lines.append(f"| `{name}` | {count} |")
    if not variant_counts:
        lines.append("| — | 0 |")
    return "\n".join(lines) + "\n"
