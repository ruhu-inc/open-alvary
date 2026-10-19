"""Confidence-routed review (CPR-01): which pages a person must review before approval, which are
verified by the agreement of independent readings and an audit sample, and the calibration that says
whether that is safe.

Routes (route profile ROUTES), one per page:
  native    the page's own text layer passed the DIG-01 checks (ingestion/native.py: a clean, visible
            layer agreeing with the reading the units came from and with an independent reading,
            numbers and negations exactly) and reads as the units do at every flag of the page;
            nothing forces ``full``
  agreed    every reader agrees on every word, no flag of any severity, no table, and the structure
            deriver reports nothing on the page
  flagged   disagreements or flags, none of a risky kind and no table: the reviewer resolves each
            flag against the image; the rest of the page is not proofread
  full      a table; a flag whose words carry a number, an amount, a date, a cross-reference or a
            negation or exception; a high flag; the first page, a contents page or a page where a
            schedule starts; a structure error or warning on the page; a clean text layer that
            differs from the units where no flag says so: reviewed whole, as before

Routed readiness applies only under an owner's verification-routing policy (D28,
ingestion/decisions/verification-routing.json): without it, every page must be checked, as REV-02 and
TXP-06 require today. Under it, ``full`` and ``flagged`` pages and one audit sample drawn from the
``agreed`` and ``native`` pages together must be checked; a sampled page whose review changed its words
or structure (a material defect) escalates every ``agreed`` and ``native`` page of the document to
review.
"""

import hashlib
import json
import math
import re
from pathlib import Path

from workbench import content

ROUTES = "oa-route-pages-1"
UNREVIEWED = ("agreed", "native")  # verified without review, and covered by one audit sample
POLICY = Path(__file__).resolve().parents[1] / "ingestion" / "decisions" / "verification-routing.json"
# Words that make a disagreement risky (TXP-06's material classes).
RISKY = re.compile(
    r"\d|\b(?:not|no|nor|never|unless|except|without|notwithstanding|neither|cannot|shall\s+not|may\s+not)\b"
    r"|\b(?:section|sections|subsection|article|paragraph|schedule|part|naira|kobo|percent|per\s+cent)\b",
    re.IGNORECASE,
)
SCHEDULE_START = re.compile(r"^\s*(?:(?:first|second|third|fourth|fifth|sixth)\s+)?schedule\b", re.IGNORECASE)
CONTENTS = re.compile(
    r"^\s*(?:arrangement\s+of\s+(?:sections|clauses|articles)|table\s+of\s+contents)\b", re.I
)


def load_policy(path: Path | None = None) -> dict | None:
    """The owner's verification-routing decision, or None (every page is then checked)."""
    path = path or POLICY
    if not path.exists():
        return None
    policy = json.loads(path.read_text())
    for key in ("decision", "decided_by", "decided_at", "audit_rate", "minimum_per_document"):
        if key not in policy:
            raise ValueError(f"The verification-routing decision lacks {key}")
    if not 0 < policy["audit_rate"] <= 1 or policy["minimum_per_document"] < 1:
        raise ValueError("The audit rate must be in (0, 1] and the minimum at least 1")
    return policy


def _flag_words(flag: dict) -> str:
    return " ".join([str(flag.get("primary") or ""), *(str(v) for v in (flag.get("checks") or {}).values())])


def route(page: dict, *, first: bool, structure_issues: int) -> tuple[str, list[str]]:
    """One page's route and why."""
    reasons = []
    if first:
        reasons.append("the first page")
    if page.get("tables") or any(n.get("kind") == "table" for n in page.get("nodes", [])):
        reasons.append("a table")
    texts = [n.get("text", "") for n in page.get("nodes", [])]
    if any(CONTENTS.match(t) for t in texts):
        reasons.append("a contents page")
    if any(SCHEDULE_START.match(t) for t in texts):
        reasons.append("a schedule starts")
    if structure_issues:
        reasons.append(f"{structure_issues} structure issue(s)")
    flags = page.get("flags", [])
    if any(f.get("severity") == "high" for f in flags):
        reasons.append("a high flag")
    if any(RISKY.search(_flag_words(f)) for f in flags):
        reasons.append("a flag on a number, date, reference or negation")
    evidence = page.get("native") or {}
    against = evidence.get("flags") or {}
    if against.get("unflagged_differences"):
        reasons.append(
            f"the text layer differs from the units at {against['unflagged_differences']} unflagged word(s)"
        )
    if reasons:
        return "full", reasons
    if evidence.get("passed") and sorted(against.get("supported") or []) == list(range(len(flags))):
        held = f"; it reads as the units do at all {len(flags)} flag(s)" if flags else ""
        return "native", [
            f"the text layer passed the DIG-01 checks (agreement {evidence['agreement']:.1%}){held}"
        ]
    if flags:
        return "flagged", [f"{len(flags)} flag(s) to resolve"]
    return "agreed", ["every reader agrees and nothing is flagged"]


def routes(reading: dict, issues_by_page: dict[int, int] | None = None) -> dict[int, tuple[str, list[str]]]:
    issues_by_page = issues_by_page or {}
    pages = sorted(reading["pages"], key=lambda p: p["page"])
    return {
        p["page"]: route(p, first=n == 0, structure_issues=issues_by_page.get(p["page"], 0))
        for n, p in enumerate(pages)
    }


def sample(document_id: str, agreed: list[int], policy: dict) -> list[int]:
    """The audit sample of a document's unreviewed (agreed and native) pages: deterministic for the
    document and the policy's decision, so it cannot be chosen after the fact; at least the policy's
    minimum (or all of them)."""
    if not agreed:
        return []
    size = min(
        len(agreed), max(policy["minimum_per_document"], math.ceil(policy["audit_rate"] * len(agreed)))
    )
    seed = f"{policy['decision']}:{document_id}"
    ranked = sorted(agreed, key=lambda page: hashlib.sha256(f"{seed}:{page}".encode()).hexdigest())
    return sorted(ranked[:size])


def _units(units: list[dict]) -> tuple[list[str], list[tuple]]:
    words = [" ".join((u.get("text") or "").split()) for u in units]
    shape = [(u.get("kind"), u.get("level"), u.get("marker"), tuple(u.get("notes") or ())) for u in units]
    return words, shape


def change(machine: list[dict], reviewed: list[dict]) -> str:
    """What a review changed on a page: ``none``, ``layout`` (kinds, levels, markers, margin notes) or
    ``material`` (any word, number or cell)."""
    if machine == reviewed:
        return "none"
    machine_words, machine_shape = _units(machine)
    reviewed_words, reviewed_shape = _units(reviewed)
    machine_cells = [c for u in machine if u.get("kind") == "table" for row in u.get("rows", []) for c in row]
    reviewed_cells = [
        c for u in reviewed if u.get("kind") == "table" for row in u.get("rows", []) for c in row
    ]
    if " ".join(machine_words).split() != " ".join(reviewed_words).split() or machine_cells != reviewed_cells:
        return "material"
    if machine_shape != reviewed_shape or len(machine) != len(reviewed):
        return "layout"
    return "none"


def requirement(
    document_id: str, reading: dict, latest: dict[int, dict], policy: dict, issues_by_page=None
) -> dict:
    """Under the policy: the pages that must be checked, the audit sample, and any escalation."""
    found = routes(reading, issues_by_page)
    unreviewed = [page for page, (name, _) in found.items() if name in UNREVIEWED]
    audited = sample(document_id, unreviewed, policy)
    machine = {p["page"]: content.units_from(p) for p in reading["pages"]}
    defects = [
        page
        for page in audited
        if (latest.get(page) or {}).get("status") == "checked"
        and change(machine[page], latest[page].get("units") or []) == "material"
    ]
    escalated = bool(defects)
    required = sorted(
        page
        for page, (name, _) in found.items()
        if name in ("full", "flagged") or page in audited or escalated
    )
    return {
        "profile": ROUTES,
        "decision": policy["decision"],
        "routes": {page: name for page, (name, _) in found.items()},
        "reasons": {page: why for page, (_, why) in found.items()},
        "audit_sample": audited,
        "audit_defects": defects,
        "escalated": escalated,
        "required_pages": required,
    }


def calibrate(library, store, document_ids: list[str]) -> dict:
    """For documents already reviewed page by page: each page's route, and what its review changed. The
    material-change rate of the routes that would skip review is the evidence D28 needs (CPR-01)."""
    from workbench import publication

    totals: dict[str, dict[str, int]] = {}
    per_document = {}
    for document_id in document_ids:
        reading, latest = library.reading(document_id), store.latest_pages(document_id)
        final = content.final_pages(reading, latest)
        built = publication.build({"id": document_id, "jurisdiction": document_id[:2]}, final, reading)
        issues: dict[int, int] = {}
        for issue in [*built.errors, *built.warnings]:
            if issue.page is not None:
                issues[issue.page] = issues.get(issue.page, 0) + 1
        found = routes(reading, issues)
        counts: dict[str, dict[str, int]] = {}
        for page in reading["pages"]:
            revision = latest.get(page["page"])
            if not revision or revision.get("status") != "checked":
                continue
            name = found[page["page"]][0]
            what = change(content.units_from(page), revision.get("units") or [])
            for bucket in (counts, totals):
                bucket.setdefault(name, {"pages": 0, "none": 0, "layout": 0, "material": 0})
                bucket[name]["pages"] += 1
                bucket[name][what] += 1
        per_document[document_id] = counts
    rates = {
        name: {**c, "material_rate": round(c["material"] / c["pages"], 4) if c["pages"] else None,
               "material_upper_95": _upper(c["material"], c["pages"])}
        for name, c in totals.items()
    }  # fmt: skip
    return {"profile": ROUTES, "routes": rates, "documents": per_document}


def _upper(failures: int, trials: int) -> float | None:
    """The 95% upper confidence bound of a rate (Wilson score); with no failures, the rule of three
    gives about 3/n."""
    if not trials:
        return None
    z, p = 1.96, failures / trials
    centre = p + z * z / (2 * trials)
    spread = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return round(min(1.0, (centre + spread) / (1 + z * z / trials)), 4)


def main(argv=None) -> int:
    """python -m workbench.routing calibrate --database URL DOCUMENT... : the calibration report."""
    import argparse
    import sys

    from workbench.store import Store, connect

    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("calibrate")
    run.add_argument("--database", required=True, help="The workbench database holding the reviews")
    run.add_argument("documents", nargs="+")
    args = parser.parse_args(argv)
    report = calibrate(content.Library(), Store(connect(args.database)), args.documents)
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
