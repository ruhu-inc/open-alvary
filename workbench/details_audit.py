"""How good each document's first draft of details is, before anyone reviews it (owner, 2026-10-29:
measure before fixing).

For every work in a collection (a law's language versions share one record, so each work is counted
once) this builds the first draft exactly as the Details screen does (workbench.review.described:
record_view.base_values for a document whose collection's reader suggested its details) and says,
field by field, where each value came from:

  printed     the reader's suggestion, with the words and page that state it
  catalogue   the reader's suggestion, from the source's listing (evidence without a page)
  reader      the reader's suggestion, without evidence shown to the reviewer
  file_name   a title that is the source file's name
  fingerprint a number that is the source file's fingerprint
  collection  the kind, title or year the collection's manifest gives, the reader's suggestions unused
  default     filled in by the Details screen itself (a family, a hierarchy, "unknown")
  missing     nothing

It also says whether the reader's suggestions were used at all, and whether their family had to be
put right from their kind (record_view.usable_suggestions), which required fields the reviewer
starts without, and, where a second source states the same fact (an RLRC file's name gives the kind,
number and year of the text inside it), whether the two agree. Nothing is written but the reports.
"""

import json
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import unquote

from schema.jurisdictions import registry
from workbench import record_view

KEY_FIELDS = (
    "identity.title",
    "identity.full_title",
    "identity.number.value",
    "identity.year",
    "publication.name",
    "publication.number",
    "publication.date",
    "publication.pages",
)
DEFAULTED = {
    "classification.hierarchy",
    "status.legal",
    "text.version.label",
}


def _plain(value) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value).casefold())


def _sha(document: dict) -> str:
    return (document.get("source_object") or "").rsplit("/", 1)[-1].split(".")[0]


def _issue_name(document: dict) -> str | None:
    issue = document.get("issue")
    return issue.get("name") if isinstance(issue, dict) else None


def from_file_name(document: dict, value) -> bool:
    from ingestion.rwanda import listing_title

    name = _issue_name(document)
    return bool(name and value and _plain(listing_title(name)) == _plain(value))


def from_fingerprint(document: dict, value) -> bool:
    sha = _sha(document)
    text = str(value or "").casefold()
    return bool(sha and text) and (text == sha[:12] or bool(re.fullmatch(rf"\d{{4}}-{sha[:6]}", text)))


def evidence_of(document: dict, key: str) -> dict | None:
    for group in ("suggested_evidence", "suggested_legal"):
        found = (document.get(group) or {}).get(key)
        if isinstance(found, dict):
            return found
    return None


def source_of(key: str, value, document: dict, used: dict) -> str:
    if record_view.empty(value):
        return "missing"
    if key in ("identity.title", "identity.full_title", "identity.citation") and from_file_name(
        document, value
    ):
        return "file_name"
    if key == "identity.number.value" and from_fingerprint(document, value):
        return "fingerprint"
    if key in used and used[key] == value:
        evidence = evidence_of(document, key)
        if evidence is None:
            return "reader"
        return "printed" if evidence.get("page") is not None else "catalogue"
    if key in DEFAULTED:
        return "default"
    return "collection"  # the collection's type, title or year


def _number(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold().replace("°", "").replace("º", "")


def _head(number: str) -> str:
    return number.split("/")[0].lstrip("0") or "0"


def numbers_agree(text_value, name_value) -> bool:
    """ "055/2024" printed and "055" in a file's name are one number; so are "01" and "1"."""
    a, b = _number(text_value), _number(name_value)
    return a == b or _head(a) == _head(b)


def second_source(document: dict, used: dict) -> dict:
    """An RLRC file's name states the kind, number and year of the text in it: do they agree with what
    the reader suggested from the text? {field: agree | disagree | text_only | name_only}."""
    url = document.get("url") or ""
    name = _issue_name(document)
    if "rlrc.gov.rw" not in url or not name:
        return {}
    from ingestion.rwanda import parse_rlrc

    if from_file_name(document, used.get("identity.title") or document.get("title")):
        # The text's own heading was not found: its kind and number were taken from the file's name,
        # so the name is no second source for them.
        return {"heading": "not_found"}
    listed = parse_rlrc(name, unquote(url))
    out = {}
    for field, text_value, name_value in (
        ("number", used.get("identity.number.value"), listed.get("number")),
        ("year", used.get("identity.year"), listed.get("year")),
        ("kind", used.get("identity.type") or document.get("type"), listed.get("kind")),
    ):
        if field == "number" and from_fingerprint(document, text_value):
            text_value = None
        if text_value and name_value:
            same = (
                numbers_agree(text_value, name_value)
                if field == "number"
                else str(text_value) == str(name_value)
            )
            out[field] = "agree" if same else "disagree"
        elif text_value:
            out[field] = "text_only"
        elif name_value:
            out[field] = "name_only"
    return out


def audit_work(document: dict, manifest: dict, profiles: dict) -> dict:
    """One work's first draft, field by field."""
    place, profile = record_view.place_for(document, manifest, profiles)
    suggested = {k: v for k, v in (document.get("suggested") or {}).items() if v not in (None, "", [])}
    if place.code == "ng" and not record_view.reader_suggested(document):
        # A Nigerian Act is drafted from its reading (workbench.details): not measured here.
        return {"id": document["id"], "drafted_from_reading": True}
    values = record_view.base_values(document, manifest)
    family, kind = values["identity.family"], values["identity.type"]
    specs = record_view.fields(family, kind, profile)
    required = [spec.key for spec in specs if spec.required]
    used, notices = record_view.usable_suggestions(document)
    return {
        "id": document["id"],
        "work": document.get("work") or document["id"],
        "jurisdiction": place.code,
        "source": next(iter((document.get("source_object") or "").split("/")[2:3]), None),
        "type": document.get("type"),
        "suggested_family": suggested.get("identity.family"),
        "family": family,
        "kind": kind,
        "suggestions": len(suggested),
        "suggestions_used": bool(used),
        "suggestions_repaired": bool(notices),
        "required": len(required),
        "missing_required": record_view.missing(values, {}, specs),
        "fields": {
            key: source_of(key, values.get(key), document, used) for key in sorted({*required, *KEY_FIELDS})
        },
        "second_source": second_source(document, used),
    }


def works(documents: list[dict]) -> list[dict]:
    """One document per work (its English version where there is one); annexes go with their law."""
    chosen: dict[str, dict] = {}
    for document in documents:
        if document.get("part"):
            continue
        key = document.get("work") or document["id"]
        if key not in chosen or (document.get("language") == "en" and chosen[key].get("language") != "en"):
            chosen[key] = document
    return [chosen[key] for key in sorted(chosen)]


def audit_collection(store, country: str, task: int = 0, tasks: int = 1, limit: int | None = None) -> dict:
    """This task's share of a collection's works, each read with its .document.json."""
    prefix = f"collections/{country}/"
    manifest = json.loads(store.get(f"{prefix}manifest.json"))
    chosen = [w for k, w in enumerate(works(manifest["documents"])) if k % tasks == task]
    if limit:
        chosen = chosen[:limit]
    profiles = registry()
    reports, failed = [], []
    for document in chosen:
        try:
            name = f"{prefix}{document.get('objects', '')}{document['id']}.document.json"
            details = json.loads(store.get(name)) if store.exists(name) else {}
            reports.append(audit_work({**document, **details}, manifest, profiles))
        except Exception as error:  # noqa: BLE001 - noted, the rest go on
            failed.append({"id": document["id"], "error": f"{type(error).__name__}: {str(error)[:300]}"})
    return {"country": country, "kind": "details", "reports": reports, "failed": failed}


def summarise(reports: list[dict]) -> dict:
    measured = [r for r in reports if not r.get("drafted_from_reading")]
    by_group: dict[str, Counter] = defaultdict(Counter)
    sources: dict[str, Counter] = defaultdict(Counter)
    missing = Counter()
    second: dict[str, Counter] = defaultdict(Counter)
    disagreements = []
    for report in measured:
        group = f"{report['jurisdiction']}/{report['source']}/{report['type']}"
        counts = by_group[group]
        counts["works"] += 1
        counts["suggestions_dropped"] += report["suggestions"] > 0 and not report["suggestions_used"]
        counts["suggestions_repaired"] += bool(report.get("suggestions_repaired"))
        counts["missing_any_required"] += bool(report["missing_required"])
        counts["title_from_file_name"] += report["fields"].get("identity.title") == "file_name"
        counts["number_from_fingerprint"] += report["fields"].get("identity.number.value") == "fingerprint"
        for key, origin in report["fields"].items():
            sources[key][origin] += 1
        missing.update(report["missing_required"])
        for field, verdict in report["second_source"].items():
            second[field][verdict] += 1
            if verdict == "disagree" and len(disagreements) < 15:
                disagreements.append({"id": report["id"], "field": field})
    total = Counter()
    for counts in by_group.values():
        total.update(counts)
    return {
        "works": len(measured),
        "drafted_from_reading": len(reports) - len(measured),
        "totals": dict(total),
        "by_source_and_type": {
            group: dict(counts) for group, counts in sorted(by_group.items(), key=lambda kv: -kv[1]["works"])
        },
        "where_values_come_from": {
            key: dict(counts.most_common()) for key, counts in sorted(sources.items())
        },
        "required_fields_missing": dict(missing.most_common(25)),
        "second_source": {field: dict(counts) for field, counts in second.items()},
        "disagreements": disagreements,
    }


def main(argv: list[str] | None = None) -> int:
    """python -m workbench.details_audit summary <folder> | run <country> (here, from the bucket)."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="python -m workbench.details_audit")
    sub = parser.add_subparsers(dest="action", required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("folder", type=Path)
    run = sub.add_parser("run")
    run.add_argument("country")
    run.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.action == "run":
        from ingestion import corpus_store

        payload = audit_collection(corpus_store.open_store(), args.country, limit=args.limit)
        print(
            json.dumps(
                summarise(payload["reports"]) | {"failed": payload["failed"]}, ensure_ascii=False, indent=1
            )
        )
        return 0
    reports, failed = [], []
    for path in sorted(args.folder.glob("task-*.json")):
        task = json.loads(path.read_text())
        for report in task.get("reports", []):
            # A line audit's file reports carry their works' details (rwanda.AuditCollection).
            reports += report["details"] if "details" in report else [report]
        failed += task.get("failed", [])
    print(json.dumps(summarise(reports) | {"failed": len(failed)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
