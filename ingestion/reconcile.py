"""Reconcile the primary reading with independent check readings, page by page.

The primary (Document AI) supplies the structure and the words. Check readings never change a
word; they only (1) supply list markers the primary dropped, when the checks that see a marker
agree on it, and (2) raise flags wherever they do not support the primary's words.
"""

import copy
from dataclasses import asdict

from ingestion import layout_parser, structure
from ingestion.compare import Flag, compare


def reconcile_page(primary: dict, checks: dict[str, dict]) -> dict:
    page = primary["page"]
    # Margin notes out of the body and merged units apart first: section numbers then line up.
    nodes = structure.restructure(copy.deepcopy(primary["nodes"]), checks)
    proposals = {}
    for name, reading in checks.items():
        proposal = copy.deepcopy(nodes)
        layout_parser.restore_markers(proposal, page, reading["text"], source=name)
        proposals[name] = proposal
    conflicts = []
    for index, node in enumerate(nodes):
        if node.get("marker"):
            continue
        seen = {
            name: proposal[index]["marker"]
            for name, proposal in proposals.items()
            if proposal[index].get("marker")
        }
        if len(set(seen.values())) == 1:
            node["marker"], node["marker_source"] = next(iter(seen.values())), sorted(seen)
        elif seen:
            conflicts.append(Flag("high", "marker_conflict", node["text"][:120], seen, (0, 0), sorted(seen)))
    text = layout_parser.page_text(nodes, page)
    flags = conflicts + compare(text, {name: reading["text"] for name, reading in checks.items()})
    return {
        "page": page,
        "nodes": nodes,
        "text": text,
        "tables": primary.get("tables", []),
        "furniture": primary.get("furniture", []),
        "flags": [asdict(flag) for flag in flags],
        "checks": {name: reading["text"] for name, reading in checks.items()},
    }


def reconcile(primary: dict, checks: dict[str, dict]) -> dict:
    """Primary and check readings as stored by the batch runner; returns the reconciled reading."""
    by_page = {name: {p["page"]: p for p in reading["pages"]} for name, reading in checks.items()}
    pages = [
        reconcile_page(page, {name: pages.get(page["page"], {"text": ""}) for name, pages in by_page.items()})
        for page in primary["pages"]
    ]
    counts = {"high": 0, "medium": 0, "low": 0}
    for page in pages:
        for flag in page["flags"]:
            counts[flag["severity"]] += 1
    restored = sum(
        1 for page in pages for node in page["nodes"] if isinstance(node.get("marker_source"), list)
    )
    return {
        "input_sha256": primary["input_sha256"],
        "primary": {"reader": primary["reader"], "version": primary["version"]},
        "checks": {name: reading["version"] for name, reading in checks.items()},
        "summary": {"pages": len(pages), "flags": counts, "restored_markers": restored},
        "pages": pages,
    }
