"""Publish approved works from the review workbench to the public catalogue (LIF-01 approved →
published), catalogue records first (owner decision, 2026-10-16).

Only the record is published: title, number, year, citation, legal status, issuer and the official
publication link, from the work's confirmed details. No text: republishing official texts waits for
the copyright check (Rwanda: Law N° 055/2024) and the public reader (MAC-01). A work is published
when the latest approval of at least one of its language versions still covers its current pages and
its confirmed details (the workbench's own test, workbench.review). A work whose approval lapses is
taken down on the next run. Each change is recorded in the change log (changes.record) by the same
write.

Nothing is published without the owner's collection decision for the jurisdiction
(ingestion/decisions/<jurisdiction>-catalogue.json, REV-01: once per collection, with evidence and
an expiry). One person may record it and approve the works (single-reviewer rule, migration 0019).

  python -m operations.publish decide --jurisdiction rw --by "Full Name" [--months 12] [--evidence URL]...
  python -m operations.publish records --jurisdiction rw [--dry-run]

`records` reads the review database (ALVARY_REVIEW_DATABASE_URL, read only), the collections
(ALVARY_REVIEW_COLLECTIONS) and writes the public catalogue (DATABASE_URL).
"""

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from api.store import Store, engine_for
from ingestion import settings
from schema.jurisdictions import registry
from schema.models import Authority, Citation, Corpus, Jurisdiction, Language, RightsRecord, Source
from workbench.content import Library
from workbench.review import Review
from workbench.store import Store as ReviewStore

DECISIONS = Path(__file__).resolve().parents[1] / "ingestion" / "decisions"
DEFAULT_BASIS = (
    "Independently authored bibliographic catalogue entry, from the reviewed details of the work, with "
    "a link to the official publication; no source text, headnotes or compilation copied."
)
DEFAULT_NOTE = {
    "rw": "Text not republished: whether Rwanda's official texts may be republished (Law N° 055/2024) "
    "is still being checked. Read the text in the Official Gazette.",
}
NAMES = {"rw": "Rwanda", "ng": "Nigeria"}
LANGUAGES = {"rw": "Kinyarwanda", "en": "English", "fr": "French"}
# Where a jurisdiction's issuing bodies are described, by kind of body; nothing is guessed per body.
OFFICIAL = {
    "rw": {"legislature": "https://www.parliament.gov.rw/", "*": "https://www.gov.rw/"},
}
LEGAL_STATUS = {
    "in_force": "current",
    "partly_in_force": "current",
    "repealed": "repealed",
    "superseded": "superseded",
    "historical": "historical",
    "expired": "historical",
    "lapsed": "historical",
}
AUTHORITY_KIND = {
    "legislature": "legislature",
    "chamber": "legislature",
    "constituent_assembly": "legislature",
    "court": "court",
    "tribunal": "court",
    "regulator": "regulator",
    "head_of_state": "government",
    "executive": "government",
    "minister": "government",
    "municipality": "government",
}


class Refused(RuntimeError):
    pass


# ---------- the owner's collection decision ----------
def decision_path(jurisdiction: str) -> Path:
    if not re.fullmatch(r"[a-z]{2}", jurisdiction):
        raise Refused("A jurisdiction is a two-letter country code.")
    return DECISIONS / f"{jurisdiction}-catalogue.json"


def decide(jurisdiction: str, by: str, months: int, evidence: list[str], basis: str, note: str) -> dict:
    """Record the owner's decision to publish this collection's catalogue records (not texts)."""
    if not by.strip() or "@" in by:
        raise Refused("Name the person deciding (a name, not an email address).")
    now = datetime.now(UTC).replace(microsecond=0)
    path = decision_path(jurisdiction)
    previous = json.loads(path.read_text()) if path.exists() else None
    decision = {
        "decision": "catalogue-records-1",
        "jurisdiction": jurisdiction,
        "operation": "publish_catalogue_records",
        "text": "not_included",
        "basis": basis.strip() or DEFAULT_BASIS,
        "legal_note": note.strip() or DEFAULT_NOTE.get(jurisdiction, "Text not republished."),
        "evidence": evidence,
        "decided_by": by.strip(),
        "decided_at": now.isoformat(),
        "expires_at": (now + timedelta(days=round(months * 30.44))).isoformat(),
        "revision": (previous or {}).get("revision", 0) + 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, indent=2, ensure_ascii=False) + "\n")
    return decision


def current_decision(jurisdiction: str, now: datetime | None = None) -> dict:
    path = decision_path(jurisdiction)
    if not path.exists():
        raise Refused(
            f"No catalogue decision for {jurisdiction}: nothing is published. The owner records one with"
            f' `python -m operations.publish decide --jurisdiction {jurisdiction} --by "Full Name"`.'
        )
    decision = json.loads(path.read_text())
    if (
        decision.get("operation") != "publish_catalogue_records"
        or decision.get("jurisdiction") != jurisdiction
    ):
        raise Refused(f"{path.name} is not a catalogue decision for {jurisdiction}.")
    if datetime.fromisoformat(decision["expires_at"]) <= (now or datetime.now(UTC)):
        raise Refused(f"The catalogue decision for {jurisdiction} expired: the owner records it again.")
    return decision


# ---------- what the workbench has approved ----------
@dataclass
class Approved:
    work: str
    record: object  # schema.record.DocumentRecord
    languages: list[str]
    approver: str
    approved_at: datetime


def from_workbench(review, library, store, jurisdiction: str) -> tuple[list[Approved], set[str]]:
    """Works with at least one approved language version (details confirmed and covered), and the
    works present in the collection that are not approved."""
    works: dict[str, list[dict]] = {}
    for listed in library.documents():
        if listed.get("jurisdiction") != jurisdiction or listed.get("part"):
            continue  # annexes follow their work, later
        works.setdefault(listed.get("work") or listed["id"], []).append(listed)
    approved, waiting = [], set()
    for work, documents in sorted(works.items()):
        found = None
        for listed in documents:
            act = library.act(listed["id"])
            now = review.state(act)
            if not now["approved"]:
                continue
            record = review.built(act, now["view"])
            if record is None:
                continue
            head = now["head"]
            found = found or {"record": record, "languages": [], "head": head}
            found["languages"].append(act.get("language") or record.expressions[0].language)
            if head["created_at"] > found["head"]["created_at"]:
                found["head"] = head
        if found is None:
            waiting.add(work)
            continue
        author = found["head"]["author"]
        approver = store.names([author]).get(author.lower(), "Open Alvary reviewer")
        when = found["head"]["created_at"]
        approved.append(
            Approved(
                work,
                found["record"],
                sorted(found["languages"]),
                approver,
                when if isinstance(when, datetime) else datetime.fromisoformat(str(when)),
            )
        )
    return approved, waiting


# ---------- into the public catalogue ----------
def _document_type(record) -> str:
    family = record.identity.family
    if family == "constitution":
        return "constitution"
    if family == "legislation":
        return "act"
    if family in ("subsidiary_legislation", "executive"):
        return "regulation"
    if family == "judgment":
        return "judgment"
    if family == "gazette":
        return "gazette"
    return "other"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "body"


def catalogue_entries(item: Approved, decision: dict) -> dict:
    """The legacy catalogue rows for one approved work: its source, rights record, citation and
    issuing authority. Raises Refused when the record lacks what a catalogue entry needs."""
    record, place = item.record, item.record.place.code
    url = next((str(p.url) for p in record.publications if p.url), None)
    if url is None:
        raise Refused(f"{item.work}: no official publication link in its details")
    body = record.issued_by[0] if record.issued_by else None
    kind = AUTHORITY_KIND.get(body.kind, "other") if body else "government"
    official = OFFICIAL.get(place, {})
    authority = Authority(
        id=f"{place}-{body.id or _slug(body.name)}" if body else f"{place}-government",
        jurisdiction_id=place,
        name=body.name if body else f"Government of {NAMES.get(place, place.upper())}",
        kind=kind,
        official_url=official.get(kind, official.get("*", url)),
    )
    citation = record.identity.citations[0].text if record.identity.citations else record.identity.title
    languages = [e.language for e in record.expressions]
    names = ", ".join(LANGUAGES.get(code, code) for code in languages)
    source = Source(
        id=item.work,
        jurisdiction_id=place,
        authority_id=authority.id,
        title=record.identity.title,
        document_type=_document_type(record),
        canonical_url=url,
        url_kind="document",
        citation=citation,
        language="en" if "en" in languages else languages[0],
        legal_status=LEGAL_STATUS.get(record.status.legal, "unknown"),
        catalogue_note=f"Official texts: {names}. " + decision["legal_note"],
        summary=record.text.summary.text if record.text.summary else None,
        year=record.identity.year,
    )
    rights = RightsRecord(
        id=f"rights-{item.work}",
        source_id=item.work,
        metadata_public=True,
        metadata_basis=decision["basis"],
        legal_note=decision["legal_note"],
        reviewer=item.approver,
        reviewed_at=item.approved_at if item.approved_at.tzinfo else item.approved_at.replace(tzinfo=UTC),
        expires_at=datetime.fromisoformat(decision["expires_at"]),
        evidence=decision["evidence"],
    )
    return {
        "source": source,
        "rights": rights,
        "citation": Citation(id=f"citation-{item.work}", source_id=item.work, value=citation),
        "authority": authority,
        "languages": languages,
    }


def publish_records(
    store, jurisdiction: str, approved: list[Approved], waiting: set[str], decision: dict, dry_run=False
) -> dict:
    """Merge approved works into the catalogue and take down those no longer approved, in one write
    (the change log records what changed). Returns what was done."""
    corpus = store.load()
    by_id = {name: {m.id: m for m in getattr(corpus, name)} for name in Corpus.model_fields}
    report = {"published": [], "updated": [], "taken_down": [], "skipped": {}}
    if jurisdiction not in by_id["jurisdictions"]:
        profile = registry().get(jurisdiction)  # the project's researched jurisdiction profile
        languages = sorted(
            {code for item in approved for code in (e.language for e in item.record.expressions)}
        )
        corpus.jurisdictions.append(
            Jurisdiction(
                id=jurisdiction,
                name=profile.name if profile else NAMES.get(jurisdiction, jurisdiction.upper()),
                country_code=jurisdiction.upper(),
                legal_systems=[s.replace("_", " ") for s in profile.legal_systems] if profile else [],
                languages=languages,
            )
        )
    for item in approved:
        try:
            entry = catalogue_entries(item, decision)
        except (Refused, ValueError) as error:
            report["skipped"][item.work] = str(error)
            continue
        for code in entry["languages"]:
            if code not in by_id["languages"]:
                language = Language(id=code, name=LANGUAGES.get(code, code), tag=code)
                corpus.languages.append(language)
                by_id["languages"][code] = language
        if entry["authority"].id not in by_id["authorities"]:
            corpus.authorities.append(entry["authority"])
            by_id["authorities"][entry["authority"].id] = entry["authority"]
        _put(corpus, "sources", entry["source"])
        _put(corpus, "citations", entry["citation"])
        existing = by_id["rights"].get(entry["rights"].id)
        if existing is not None and existing.approved_content_hashes:
            # A later text decision owns this rights record; the records step never narrows it.
            entry["rights"] = existing.model_copy(update={"metadata_public": True})
        _put(corpus, "rights", entry["rights"])
        report["updated" if item.work in by_id["sources"] else "published"].append(item.work)
    for work in sorted(waiting):
        rights = by_id["rights"].get(f"rights-{work}")
        if work in by_id["sources"] and rights is not None and rights.metadata_public:
            _put(corpus, "rights", rights.model_copy(update={"metadata_public": False}))
            report["taken_down"].append(work)
    if not dry_run:
        store.save(corpus)
    return report


def _put(corpus, name, model):
    rows = getattr(corpus, name)
    for i, row in enumerate(rows):
        if row.id == model.id:
            rows[i] = model
            return
    rows.append(model)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    decide_cmd = commands.add_parser("decide")
    decide_cmd.add_argument("--jurisdiction", required=True)
    decide_cmd.add_argument("--by", required=True, help="Who decides: a full name")
    decide_cmd.add_argument("--months", type=int, default=12)
    decide_cmd.add_argument("--evidence", action="append", default=[])
    decide_cmd.add_argument("--basis", default="")
    decide_cmd.add_argument("--legal-note", default="")
    records = commands.add_parser("records")
    records.add_argument("--jurisdiction", required=True)
    records.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "decide":
            decision = decide(
                args.jurisdiction, args.by, args.months, args.evidence, args.basis, args.legal_note
            )
            print(
                f"Recorded {decision_path(args.jurisdiction).name}, revision {decision['revision']}, until {decision['expires_at'][:10]}."
            )
            print("Commit it: the decision is part of the public record of why these entries are published.")
            return 0
        decision = current_decision(args.jurisdiction)
        settings.load()
        review_store = ReviewStore(create_engine(os.environ["ALVARY_REVIEW_DATABASE_URL"]))  # read only
        library = Library()
        approved, waiting = from_workbench(
            Review(review_store, library, registry()), library, review_store, args.jurisdiction
        )
        report = publish_records(
            Store(engine_for()), args.jurisdiction, approved, waiting, decision, args.dry_run
        )
        print(json.dumps(report, indent=2))
        return 0
    except Refused as refused:
        raise SystemExit(str(refused)) from None


if __name__ == "__main__":
    raise SystemExit(main())
