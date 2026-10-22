"""Calibrate structure rules (TXP-03) on official texts as the pipeline reads them: the Layout Parser's
reading of a text, checked against Google's OCR of it and reconciled (ingestion.reconcile), made into the
workbench's review units (workbench.content.units_from), then read by a family's rules
(workbench.structure). Nothing has been reviewed by hand, so the report shows what a reviewer would
start from: what the rules find, and every issue they raise.

  python -m ingestion.calibrate SAMPLE [SAMPLE ...] --rules ohada-uniform-act-1 [--language fr]
      [--show 30] [--json REPORT.json]

A SAMPLE is a folder holding layout.json (ingestion.layout_parser.parse) and ocr.json
(ingestion.layout_parser.ocr) for one text, as private-staging/structure-samples/ holds them. The PDF
itself is never opened here: the page count is the OCR's. Several samples given together are language
versions of one text, each with its language (--language fr,en,pt in the same order, or one for all),
and the report compares the provisions each version finds: a version's structure is the same text's,
so the provisions should be the same.
"""

import argparse
import collections
import json
import sys
from pathlib import Path

from ingestion import gazette_layout, readers, reconcile
from workbench import content, serialize, structure

LINE = 0.6  # words whose middles lie within 0.6 of a word's height of each other are on one line


def ocr_text(page: dict) -> str:
    """A page's OCR words as lines of text, column by column and top to bottom."""
    placed = gazette_layout.from_ocr(page, {"width": float(page["width"]), "height": float(page["height"])})
    words = placed["words"]
    lines = []
    for x0, x1 in gazette_layout.columns(placed) or [(0.0, float(page["width"]) + 1)]:
        inside = sorted(
            (w for w in words if x0 <= (w["xmin"] + w["xmax"]) / 2 < x1),
            key=lambda w: ((w["ymin"] + w["ymax"]) / 2, w["xmin"]),
        )
        line, middle = [], None
        for word in inside:
            here, height = (word["ymin"] + word["ymax"]) / 2, max(word["ymax"] - word["ymin"], 1e-6)
            if line and abs(here - middle) > LINE * height:
                lines.append(" ".join(w["text"] for w in sorted(line, key=lambda w: w["xmin"])))
                line = []
            if not line:
                middle = here
            line.append(word)
        if line:
            lines.append(" ".join(w["text"] for w in sorted(line, key=lambda w: w["xmin"])))
    return "\n".join(lines)


def review_pages(sample: Path) -> tuple[list[dict], dict]:
    """A sample's pages of review units, as the workbench would hold them before any review, and the
    reconciliation's summary."""
    layout = json.loads((sample / "layout.json").read_text())
    ocr = json.loads((sample / "ocr.json").read_text())
    count = len(ocr["pages"])
    primary = {
        "input_sha256": None,
        "reader": "docai",
        "version": layout.get("processor"),
        "pages": readers.layout_pages(layout["nodes"], count),
    }
    check = {
        "version": ocr.get("processor"),
        "pages": [{"page": p["page"], "text": ocr_text(p)} for p in ocr["pages"]],
    }
    reconciled = reconcile.reconcile(primary, {"dococr": check})
    pages = [{"page": p["page"], "units": content.units_from(p)} for p in reconciled["pages"]]
    return pages, reconciled["summary"]


def calibrate(sample: Path, rules_id: str, language: str | None) -> dict:
    """What the rules find in a sample: the nodes by kind, the issues by code, and the provisions."""
    pages, summary = review_pages(sample)
    text = serialize.serialize(pages)
    found = structure.derive(text, pages, rules_id, language)
    nodes = [n for n in found.nodes if n.kind != "document"]
    provisions = [n.locator for n in nodes if n.kind in ("article", "section")]
    raw, nested = text.text.encode(), True
    for node in found.nodes:
        try:
            raw[node.start : node.end].decode()
        except UnicodeDecodeError:
            nested = False
        if node.parent and not node.parent.start <= node.start <= node.end <= node.parent.end:
            nested = False
    # The provisions' numbers in each run that numbers them: the text's own, and each annex's that
    # republishes a law (annex_1__art_1), whose numbering restarts.
    runs: dict[str, list[int]] = {}
    for locator in provisions:
        within, _, own = locator.rpartition("__")
        if own.split("_")[1].isdigit():
            runs.setdefault(within, []).append(int(own.split("_")[1]))
    numbers = runs.get("", [])
    codes = {i.code for i in found.issues}

    def missing(run: list[int]) -> list[int]:
        return sorted(set(range(1, max(run, default=0) + 1)) - set(run))

    checks = {
        # TXP-03's calibration: every provision found in order, locators unique, every leaf held once.
        "leaves_held_once": sorted(i for n in found.nodes for i in n.leaves) == list(range(len(text.leaves))),
        "spans_nest": nested,
        "locators_unique": "repeated_locator" not in codes,
        "in_order": "article_order" not in codes and all(run == sorted(run) for run in runs.values()),
        "missing_numbers": missing(numbers),
    }
    if set(runs) - {""}:
        checks["missing_in"] = {within: missing(run) for within, run in runs.items() if within}
    return {
        "sample": str(sample),
        "rules": rules_id,
        "language": language,
        "pages": len(pages),
        "units": sum(len(p["units"]) for p in pages),
        "reconciled": summary,
        "checks": checks,
        "kinds": dict(collections.Counter(n.kind for n in nodes).most_common()),
        "issues": dict(collections.Counter(f"{i.level}:{i.code}" for i in found.issues).most_common()),
        "issue_samples": [
            {"level": i.level, "code": i.code, "message": i.message} for i in found.issues[:40]
        ],
        "provisions": provisions,
        "outline": [
            {"kind": n.kind, "locator": n.locator, "heading": (n.heading or "")[:90]}
            for n in nodes
            if n.kind
            not in (
                "subsection",
                "paragraph",
                "subparagraph",
                "alinea",
                "item",
                "contents_entry",
                "definition",
            )
        ],
    }


def compare(reports: list[dict]) -> dict:
    """The provisions each language version finds that another does not, by number: one edition's
    "SECTION 7" is another's "ARTICLE 7" (Cameroon)."""
    found = {r["language"] or r["sample"]: {p.split("_", 1)[1] for p in r["provisions"]} for r in reports}
    every = set().union(*found.values())
    return {
        version: {"count": len(have), "missing": sorted(every - have)[:40]} for version, have in found.items()
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("samples", nargs="+", type=Path)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--language", default="", help="one for all samples, or one per sample: fr,en,pt")
    parser.add_argument("--show", type=int, default=30, help="outline lines and issues to print")
    parser.add_argument("--json", type=Path, help="write the full reports here")
    args = parser.parse_args(argv)
    languages = [code.strip() or None for code in args.language.split(",")] if args.language else [None]
    if len(languages) == 1:
        languages *= len(args.samples)
    if len(languages) != len(args.samples):
        parser.error("give one language for all samples, or one per sample")
    reports = [calibrate(s, args.rules, lang) for s, lang in zip(args.samples, languages, strict=True)]
    for report in reports:
        print(f"== {report['sample']} ({report['rules']}, {report['language']})")
        print(f"   {report['pages']} pages, {report['units']} units; flags {report['reconciled']['flags']}")
        print(f"   nodes: {report['kinds']}")
        print(f"   checks: {report['checks']}")
        print(f"   issues: {report['issues'] or 'none'}")
        for line in report["outline"][: args.show]:
            print(f"     {line['kind']:12} {line['locator']:40} {line['heading']}")
        for issue in report["issue_samples"][: args.show]:
            print(f"   ! {issue['level']} {issue['code']}: {issue['message'][:160]}")
    if len(reports) > 1:
        print("== versions compared:", json.dumps(compare(reports), ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
