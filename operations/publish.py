"""Publish approved works from the review workbench to the public catalogue (LIF-01 approved →
published), catalogue records first (owner decision, 2026-10-16).

Only the record is published: title, number, year, citation, legal status, issuer, the official
publication link, what each language version is worth as law and the work's rule where they differ,
a regional instrument's legal effect and a treaty's parties and entry into force, from the work's
confirmed details. No text: republishing official texts waits for
the copyright check (Rwanda: Law N° 055/2024) and the public reader (MAC-01). A work is published
when the latest approval of at least one of its language versions still covers its current pages and
its confirmed details (the workbench's own test, workbench.review). A work whose approval lapses is
taken down on the next run. Each change is recorded in the change log (changes.record) by the same
write.

Nothing is published without the owner's collection decision for the jurisdiction
(ingestion/decisions/<jurisdiction>-catalogue.json, REV-01: once per collection, with evidence and
an expiry). One person may record it and approve the works (single-reviewer rule, migration 0019).

Texts follow their records under a separate decision (ingestion/decisions/<jurisdiction>-text.json,
TXP-07): the reviewed text of each approved language version, its legal structure and its rights,
exactly as its approval bound them, or nothing.

  python -m operations.publish decide --jurisdiction rw --by "Full Name" [--months 12] [--evidence URL]...
  python -m operations.publish records --jurisdiction rw [--dry-run]
  python -m operations.publish decide-text --jurisdiction ng --by "Full Name" --rights-holder … --licence …
      --basis … --evidence URL --privacy-checked [--types act constitution] [--attribution TEXT]
  python -m operations.publish texts --jurisdiction ng [--dry-run]

`records` and `texts` read the review database (ALVARY_REVIEW_DATABASE_URL, read only), the
collections (ALVARY_REVIEW_COLLECTIONS) and write the public catalogue (DATABASE_URL).
"""

import argparse
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, insert, select, update

from api.search import normalize_citation
from api.store import Store, engine_for
from ingestion import settings
from rights.policy import decide as rights_decision
from schema.jurisdictions import catalogue_entry, registry
from schema.models import (
    Amendment,
    Authority,
    Citation,
    Corpus,
    Edition,
    Language,
    LanguageRule,
    PartyStatus,
    Permission,
    RegionalEffect,
    RightsRecord,
    RightsStatus,
    Source,
    SourceVersion,
    StatedDate,
    StatedEvidence,
    StructureNode,
    TreatyStatus,
)
from workbench.content import Library
from workbench.review import Review
from workbench.store import Store as ReviewStore
from workbench.store import publication_run, publication_status

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


# Words that mark a value someone forgot to fill in ("Your Full Name", "https://LINK-TO-…").
PLACEHOLDER = re.compile(
    r"\b(?:your|full)[\s_-]+name\b|[<>…{}]|\bplaceholder\b|\btodo\b|\blink[\s_-]+to\b", re.I
)
RESERVED_HOSTS = re.compile(r"(?:^|\.)(?:example|test|invalid|localhost)(?:\.[a-z]+)?$")  # RFC 2606


def decider(by: str) -> str:
    """The person deciding: a name, not an email address or a placeholder."""
    name = (by or "").strip()
    if not name or "@" in name or PLACEHOLDER.search(name):
        raise Refused("Name the person deciding (a name, not an email address or a placeholder).")
    return name


def evidence_links(links: list[str]) -> list[str]:
    """Evidence is real web addresses: https, a public host name, no placeholder."""
    for link in links:
        parts = urlsplit(link or "")
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or "." not in host
            or RESERVED_HOSTS.search(host)
            or PLACEHOLDER.search(link)
        ):
            raise Refused(f"Evidence must be a real https address, not {link!r}.")
    return list(links)


def check_decision(decision: dict, path: Path) -> None:
    """A recorded decision is real: named, with real evidence (a file edited by hand included)."""
    try:
        decider(decision.get("decided_by", ""))
        evidence_links(decision.get("evidence") or [])
    except Refused as error:
        raise Refused(f"{path.name} is not a usable decision: {error} The owner records it again.") from None


def decide(jurisdiction: str, by: str, months: int, evidence: list[str], basis: str, note: str) -> dict:
    """Record the owner's decision to publish this collection's catalogue records (not texts)."""
    by, evidence = decider(by), evidence_links(evidence)
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
            f" `python -m operations.publish decide --jurisdiction {jurisdiction} --by` and their own name."
        )
    decision = json.loads(path.read_text())
    if (
        decision.get("operation") != "publish_catalogue_records"
        or decision.get("jurisdiction") != jurisdiction
    ):
        raise Refused(f"{path.name} is not a catalogue decision for {jurisdiction}.")
    check_decision(decision, path)
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


def from_workbench(review, library, store, jurisdiction: str, only=None) -> tuple[list[Approved], set[str]]:
    """Works with at least one approved language version (details confirmed and covered), and the
    works present in the collection that are not approved; of the documents in `only` when given (a
    run looks only at documents an approval names: a work never approved was never published)."""
    works: dict[str, list[dict]] = {}
    for listed in library.documents():
        if listed.get("jurisdiction") != jurisdiction or listed.get("part"):
            continue  # annexes follow their work, later
        if only is not None and listed["id"] not in only:
            continue
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
# The catalogue's document type of each record family (schema.record.FAMILIES); every other family is
# "other".
DOCUMENT_TYPES = {
    "constitution": "constitution",
    "legislation": "act",
    "subsidiary_legislation": "regulation",
    "executive_instrument": "regulation",
    "judgment": "judgment",
    "gazette": "gazette",
}


def _document_type(record) -> str:
    return DOCUMENT_TYPES.get(record.identity.family, "other")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "body"


# Names a reviewer withheld in a sensitive case (a child, a sexual offence: owner, 2026-10-27) are never
# published: each is given as NAME_WITHHELD, and a record whose details would still name one is refused.
NAME_WITHHELD = "[Withheld]"
NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{3,}")
NOT_A_NAME = {
    "limited", "company", "nigeria", "federal", "republic", "state", "government", "attorney", "general",
    "bank", "council", "commission", "university", "others", "industrial", "court", "national", "ministry",
}  # fmt: skip


def withheld_names(record) -> list[str]:
    """The party names a reviewer withheld: those marked Withheld, or every party when all names are."""
    profile = getattr(record, "profile", None)
    level = getattr(profile, "anonymisation", "none")
    if level == "none":
        return []
    return [
        p.name
        for p in profile.parties
        if (level == "full" or p.anonymised) and p.name.strip() not in ("", NAME_WITHHELD)
    ]


def withhold(text, names: list[str]):
    """The text with each withheld name given as NAME_WITHHELD (any case)."""
    if not text:
        return text
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name.strip()), NAME_WITHHELD, text, flags=re.I)
    return text


def still_named(texts, names: list[str]) -> bool:
    """Whether a word of a withheld name is left in what would be published (a name cited differently)."""
    words = {w.lower() for name in names for w in NAME_WORD.findall(name)} - NOT_A_NAME
    return any(w.lower() in words for text in texts for w in NAME_WORD.findall(text or ""))


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
    withheld = withheld_names(record)
    title = withhold(record.identity.title, withheld)
    citation = withhold(citation, withheld)
    summary = withhold(record.text.summary.text if record.text.summary else None, withheld)
    cited = aliases(item.work, citation, record.identity, withheld)
    if withheld and still_named([title, citation, summary, *(c.value for c in cited)], withheld):
        raise Refused(
            f"{item.work}: names are withheld in this case, but its title, citations or summary would still "
            "name a withheld party: edit them"
        )
    languages = [e.language for e in record.expressions]
    names = ", ".join(LANGUAGES.get(code, code) for code in languages)
    source = Source(
        id=item.work,
        jurisdiction_id=place,
        authority_id=authority.id,
        title=title,
        document_type=_document_type(record),
        canonical_url=url,
        url_kind="document",
        citation=citation,
        language="en" if "en" in languages else languages[0],
        legal_status=LEGAL_STATUS.get(record.status.legal, "unknown"),
        catalogue_note=f"Official texts: {names}. " + decision["legal_note"],
        summary=summary,
        year=record.identity.year,
        editions=editions(record),
        language_rule=language_rule(record),
        regional=regional_effect(record),
        treaty=treaty_status(record),
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
        "citations": cited,
        "authority": authority,
        "languages": languages,
    }


def editions(record) -> list[Edition]:
    """What each language version is worth as law, as the reviewed record states it, with where (API-02,
    DAT-07). A version the record states nothing of is left out: the catalogue answers it as unknown."""
    return [
        Edition(language=e.language, authority=e.authority, evidence=_stated(e.authority_evidence))
        for e in record.expressions
        if e.authority is not None
    ]


# Why a reviewer gave no language rule, as the catalogue says it; "not yet known" and the rest are unknown.
RULE_ABSENT = {"not_in_source": "not_stated", "not_applicable": "not_applicable"}


def language_rule(record) -> LanguageRule | None:
    """The work's own rule where its language versions differ (LNG-03), with where it is stated; or that
    the reviewed copy states none, or that none applies, as the reviewer said; None when not recorded."""
    rule = record.language_rule
    if rule is not None:
        return LanguageRule(
            status="stated",
            rule=rule.rule,
            prevails=rule.prevails,
            provision=rule.provision,
            evidence=_stated(rule.evidence),
        )
    absent = record.absent.get("language_rule")
    status = RULE_ABSENT.get(absent.reason) if absent else None
    return LanguageRule(status=status) if status else None


def _stated(evidence) -> StatedEvidence | None:
    return StatedEvidence.model_validate(evidence.model_dump(mode="json")) if evidence else None


def regional_effect(record) -> RegionalEffect | None:
    """A regional instrument's legal effect as its reviewed record states it (JUR-04): the adopting organ,
    the effect, the member states it binds (none listed: every member) and its transposition deadline."""
    profile = record.profile
    if profile.family != "regional_instrument":
        return None
    return RegionalEffect(
        organ=profile.organ.name,
        legal_effect=profile.legal_effect,
        member_states=list(profile.member_states),
        transposition_deadline=profile.transposition_deadline,
    )


def _date(dated) -> StatedDate | None:
    if dated is None:
        return None
    return StatedDate(value=dated.value, precision=dated.precision, printed=dated.printed)


def treaty_status(record) -> TreatyStatus | None:
    """A treaty's status as its reviewed record states it (JUR-04): each party's signature, ratification,
    accession and denunciation, dated, and its reservations; the date the party list is complete as of,
    with its evidence, when recorded; and when the treaty entered into force, from its one
    entry-into-force event for the whole treaty (none, or several dates: not recorded)."""
    profile = record.profile
    if profile.family != "treaty":
        return None
    entries = {
        (e.when.value, e.when.precision, e.when.printed)
        for e in record.events
        if e.kind == "entered_into_force" and e.scope is None
    }
    entered = next(iter(entries)) if len(entries) == 1 else None
    return TreatyStatus(
        adopted_by=profile.adopted_by.name if profile.adopted_by else None,
        depositary=profile.depositary,
        entered_into_force=StatedDate(value=entered[0], precision=entered[1], printed=entered[2])
        if entered
        else None,
        authentic_languages=list(profile.authentic_languages),
        parties=[
            PartyStatus(
                party=p.party,
                place=p.place,
                signed=_date(p.signed),
                ratified=_date(p.ratified),
                acceded=_date(p.acceded),
                denounced=_date(p.denounced),
                reservations=p.reservations,
            )
            for p in profile.parties
        ],
        parties_complete_as_of=profile.parties_complete_as_of,
        parties_complete_evidence=_stated(profile.parties_complete_evidence),
    )


def aliases(work: str, citation: str, identity, withheld: list[str] | None = None) -> list[Citation]:
    """Every form a work is cited by in its reviewed details, each a citation alias (API-04): the
    citation the record shows first (with the ID it always had), then every other citation, such as
    a popular one ("NDPA"), and every alternative title. One per form, compared as citations are; any
    name withheld given as NAME_WITHHELD."""
    forms = [citation, *(c.text for c in identity.citations), *identity.alternative_titles]
    forms = [withhold(form, withheld or []) for form in forms]
    found, seen = [], set()
    for form in forms:
        key = normalize_citation(form)
        if not key or key in seen:
            continue
        seen.add(key)
        suffix = "" if not found else f"-{_slug(form)}"
        found.append(Citation(id=f"citation-{work}{suffix}"[:160], source_id=work, value=form))
    return found


# A reviewed link, as the catalogue keeps it: from the asserting (newer) instrument to the one it
# concerns (EDT-01), whichever of the two records states it.
FORWARD = {
    kind: kind
    for kind in ("repeals", "amends", "supersedes", "made_under", "commences", "transposes", "domesticates",
                 "implements", "ratifies", "cites")
}  # fmt: skip
BACKWARD = {
    "repealed_by": "repeals",
    "amended_by": "amends",
    "superseded_by": "supersedes",
    "enables": "made_under",
    "commenced_by": "commences",
    "transposed_by": "transposes",
    "domesticated_by": "domesticates",
    "implemented_by": "implements",
}
# Relations a record may state that the catalogue does not publish from it, and why: each is reported,
# never dropped silently. Judicial treatment is published from a judgment's reviewed citations instead,
# each with its paragraph and a legally qualified reviewer; appeal history is not published yet.
TREATMENT = ("overrules", "overruled_by", "follows", "distinguishes")
APPEAL = ("appeal_from", "appealed_to", "affirms", "reverses")
WITHHELD = {
    **{
        kind: "judicial treatment is published from the judgment's reviewed citations, with the paragraph "
        "and a doctrinal review (operations.citations), not from its details (EDT-01)"
        for kind in TREATMENT
    },
    **{
        kind: "appeal history waits for its own review of the case's identity and chain (EDT-01)"
        for kind in APPEAL
    },
}
NOT_PUBLISHED = "not a relationship kind the catalogue publishes yet"


def catalogue_links(approved: list[Approved], sources: dict[str, Source]) -> list[Amendment]:
    """The links between catalogued works from their reviewed details: what each repeals, amends,
    supersedes, commences, transposes, domesticates, implements, ratifies, cites or is made under, and
    the same links stated from the other end. Each is checked (a reviewer confirmed the details) and
    cites the asserting instrument's official link; a link to a work the catalogue does not hold is left
    out, and so are the kinds withheld_relations reports."""
    links: dict[str, Amendment] = {}
    for item in approved:
        for relation in item.record.relations:
            other = relation.target.work_uri
            if not other or other == item.work or other not in sources or item.work not in sources:
                continue
            if relation.kind in FORWARD:
                newer, older, kind = item.work, other, FORWARD[relation.kind]
            elif relation.kind in BACKWARD:
                newer, older, kind = other, item.work, BACKWARD[relation.kind]
            else:
                continue
            link_id = f"link-{newer}-{kind}-{older}"
            links[link_id] = Amendment(
                id=link_id,
                source_id=newer,
                target_source_id=older,
                kind=kind,
                evidence_url=sources[newer].canonical_url,
                verified=True,
            )
    return sorted(links.values(), key=lambda link: link.id)


def withheld_relations(approved: list[Approved]) -> list[dict]:
    """The relations in approved works' reviewed details that the catalogue does not publish, each with
    the work, the other work (or its citation) and why."""
    return [
        {
            "work": item.work,
            "kind": relation.kind,
            "other": relation.target.work_uri or relation.target.citation,
            "reason": WITHHELD.get(relation.kind, NOT_PUBLISHED),
        }
        for item in approved
        for relation in item.record.relations
        if relation.kind not in FORWARD and relation.kind not in BACKWARD
    ]


def publish_records(
    store, jurisdiction: str, approved: list[Approved], waiting: set[str], decision: dict, dry_run=False
) -> dict:
    """Merge approved works into the catalogue and take down those no longer approved, in one write
    (the change log records what changed). Returns what was done."""
    corpus = store.load()
    by_id = {name: {m.id: m for m in getattr(corpus, name)} for name in Corpus.model_fields}
    report = {"published": [], "updated": [], "taken_down": [], "skipped": {}}
    if jurisdiction not in by_id["jurisdictions"]:
        # From the project's jurisdiction profile; a regional body carries no country code (DAT-02).
        languages = {code for item in approved for code in (e.language for e in item.record.expressions)}
        corpus.jurisdictions.append(
            catalogue_entry(jurisdiction, name=NAMES.get(jurisdiction), languages=languages)
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
        for alias in entry["citations"]:
            _put(corpus, "citations", alias)
        existing = by_id["rights"].get(entry["rights"].id)
        if existing is not None and existing.approved_content_hashes:
            # A later text decision owns this rights record and the note on what text there is; the
            # records step never narrows either.
            entry["rights"] = existing.model_copy(update={"metadata_public": True})
            held = by_id["sources"].get(item.work)
            if held is not None:
                entry["source"] = entry["source"].model_copy(update={"catalogue_note": held.catalogue_note})
        _put(corpus, "rights", entry["rights"])
        report["updated" if item.work in by_id["sources"] else "published"].append(item.work)
    # The reviewed links of the works published now replace the links they had. A link between two of
    # them that neither states any more is withdrawn: kept unchecked, so it leaves public view and the
    # change log says so (a save never deletes a row). A link to a work outside this run is left as it is.
    works = {item.work for item in approved}
    links = catalogue_links(approved, {s.id: s for s in corpus.sources})
    stated, kept, withdrawn = {link.id for link in links}, [], []
    for link in corpus.amendments:
        if link.id in stated:
            continue
        if (
            link.id.startswith("link-")
            and link.verified
            and link.source_id in works
            and link.target_source_id in works
        ):
            link = link.model_copy(update={"verified": False})
            withdrawn.append(link.id)
        kept.append(link)
    corpus.amendments = kept + links
    report["links"] = len(links)
    report["links_withdrawn"] = withdrawn
    report["withheld"] = withheld_relations(approved)
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


# ---------- the owner's text decision (TXP-07) ----------
TEXT_TYPES = ("constitution", "act", "regulation", "judgment", "gazette", "guidance", "other")


def text_decision_path(jurisdiction: str) -> Path:
    return decision_path(jurisdiction).with_name(f"{jurisdiction}-text.json")


def decide_text(
    jurisdiction: str,
    by: str,
    months: int,
    evidence: list[str],
    *,
    types: list[str],
    rights_holder: str,
    licence: str,
    licence_url: str | None,
    attribution: str | None,
    database_rights: str,
    basis: str,
    privacy_checked: bool,
) -> dict:
    """Record the owner's decision to publish this collection's reviewed texts: what the texts are,
    whose they are, under what terms, why they may be republished, and that the kind of document
    was screened for personal data. One decision for the collection (REV-01), with an expiry."""
    by, evidence = decider(by), evidence_links(evidence)
    if licence_url:
        evidence_links([licence_url])
    stated = (("--rights-holder", rights_holder), ("--licence", licence), ("--basis", basis))
    missing = [name for name, value in stated if not value.strip() or PLACEHOLDER.search(value)]
    if missing or not evidence:
        raise Refused(f"A text decision needs {', '.join(missing + ([] if evidence else ['--evidence']))}.")
    if not privacy_checked:
        raise Refused("Say that this kind of document was screened for personal data (--privacy-checked).")
    unknown = sorted(set(types) - set(TEXT_TYPES))
    if not types or unknown:
        raise Refused(f"Name the kinds of document the decision covers, among {', '.join(TEXT_TYPES)}.")
    now = datetime.now(UTC).replace(microsecond=0)
    path = text_decision_path(jurisdiction)
    previous = json.loads(path.read_text()) if path.exists() else None
    decision = {
        "decision": "full-text-1",
        "jurisdiction": jurisdiction,
        "operation": "publish_full_text",
        "document_types": sorted(set(types)),
        "rights_holder": rights_holder.strip(),
        "licence": licence.strip(),
        "licence_url": licence_url or None,
        "attribution_required": bool(attribution),
        "attribution_text": attribution or None,
        "database_rights_notes": database_rights.strip() or "No database right is claimed in the texts.",
        "privacy_status": "cleared",
        "basis": basis.strip(),
        "evidence": evidence,
        "decided_by": by,
        "decided_at": now.isoformat(),
        "expires_at": (now + timedelta(days=round(months * 30.44))).isoformat(),
        "revision": (previous or {}).get("revision", 0) + 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, indent=2, ensure_ascii=False) + "\n")
    return decision


def current_text_decision(jurisdiction: str, now: datetime | None = None) -> dict:
    path = text_decision_path(jurisdiction)
    if not path.exists():
        raise Refused(
            f"No text decision for {jurisdiction}: no text is published. The owner records one with"
            f" `python -m operations.publish decide-text --jurisdiction {jurisdiction}` and their own name,"
            " evidence and terms."
        )
    decision = json.loads(path.read_text())
    if decision.get("operation") != "publish_full_text" or decision.get("jurisdiction") != jurisdiction:
        raise Refused(f"{path.name} is not a text decision for {jurisdiction}.")
    check_decision(decision, path)
    if datetime.fromisoformat(decision["expires_at"]) <= (now or datetime.now(UTC)):
        raise Refused(f"The text decision for {jurisdiction} expired: the owner records it again.")
    return decision


# ---------- approved texts, exactly as their approval bound them ----------
@dataclass
class ApprovedText:
    document_id: str
    work: str
    language: str
    document_type: str
    text: object  # workbench.publication.Text
    approver: str
    approved_at: datetime
    provenance: dict
    effective_from: date | None = None


@dataclass
class Texts:
    approved: list[ApprovedText] = field(default_factory=list)
    revoked: set[tuple[str, str]] = field(default_factory=set)  # (work, language) whose approval was revoked
    skipped: dict[str, str] = field(default_factory=dict)


def texts_from_workbench(review, library, store, jurisdiction: str, only=None) -> Texts:
    """Each language version whose latest approval covers its current pages, details and text, built
    again from those pages; refused when the text or structure differ from what the approval bound.
    Of the documents in `only` when given."""
    found = Texts()
    for listed in library.documents():
        if listed.get("jurisdiction") != jurisdiction or listed.get("part"):
            continue
        if only is not None and listed["id"] not in only:
            continue
        act = library.act(listed["id"])
        now = review.state(act, text=True)
        head, built = now["head"], now["text"]
        work = act.get("work") or act["id"]
        language = act.get("language") or "en"
        if head and head["decision"] == "revoked":
            found.revoked.add((work, language))
        if not now["approved"]:
            continue
        if act.get("text_held"):  # approved on its details alone (owner, 2026-10-25)
            found.skipped[act["id"]] = f"its text is held: {act['text_held'].get('note', 'not publishable')}"
            continue
        # Errors before the bound hashes, as approving again cannot cure them: a document approved
        # before its kind had to have structure rules says so, not "approve it again".
        if built.errors:
            found.skipped[act["id"]] = (
                f"{len(built.errors)} problem(s) in its text's structure, first: {built.errors[0].message}"
            )
            continue
        bound = (head.get("text_sha256"), head.get("structure_sha256"))
        made = (head.get("serialization_profile"), head.get("structure_rules"))
        if bound != (built.text_sha256, built.structure_sha256) or made != (built.profile_id, built.rules_id):
            found.skipped[act["id"]] = (
                "its approval does not cover this text and structure (approved before they were bound, "
                "or the rules changed): approve it again"
            )
            continue
        record = review.built(act, now["view"])
        if (
            record is not None
            and getattr(getattr(record, "profile", None), "anonymisation", "none") != "none"
        ):
            found.skipped[act["id"]] = (
                "names are withheld in this case: its text is not published (names in a text are not yet masked)"
            )
            continue
        provenance = library.provenance(act["id"])
        if record is None or not provenance.get("sha256") or not provenance.get("retrieved_at"):
            found.skipped[act["id"]] = "no record of where and when its original file was retrieved"
            continue
        author = head["author"]
        when = head["created_at"]
        when = when if isinstance(when, datetime) else datetime.fromisoformat(str(when))
        found.approved.append(
            ApprovedText(
                document_id=act["id"],
                work=work,
                language=language,
                document_type=_document_type(record),
                text=built,
                approver=store.names([author]).get(author.lower(), "Open Alvary reviewer"),
                approved_at=when if when.tzinfo else when.replace(tzinfo=UTC),
                provenance=provenance,
            )
        )
    return found


def version_from(item: ApprovedText, source: Source, prior: SourceVersion | None) -> tuple:
    """A new text version of the work and its structure nodes, with IDs minted now (IDT-01)."""
    text, built = item.text.serialized.text, item.text
    version = SourceVersion(
        id=f"ver_{uuid.uuid4()}",
        source_id=item.work,
        canonical_url=item.provenance.get("url") or source.canonical_url,
        retrieved_at=datetime.fromisoformat(item.provenance["retrieved_at"]),
        raw_hash=item.provenance["sha256"],
        content_hash=built.text_sha256,
        canonical_text=text,
        parser=f"Open Alvary review workbench ({built.rules_id})",
        language=item.language,
        effective_from=item.effective_from,
        verification_status="verified",
        content_verifier=item.approver,
        content_verified_at=item.approved_at,
        serialization_profile=built.profile_id,
        prior_version_id=prior.id if prior else None,
    )
    raw = text.encode()
    nodes = built.structure.nodes
    ids = {id(node): f"nod_{uuid.uuid4()}" for node in nodes}
    by_locator = {node.locator: ids[id(node)] for node in nodes}
    rows = [
        StructureNode(
            id=ids[id(node)],
            source_id=item.work,
            version_id=version.id,
            parent_id=ids[id(node.parent)] if node.parent else None,
            kind=node.kind,
            locator=node.locator,
            order=node.order,
            start_byte=node.start,
            end_byte=node.end,
            page=node.page_start,
            page_end=node.page_end,
            source_hash=built.text_sha256,
            label=node.label or None,
            heading=node.heading or None,
            span_hash=hashlib.sha256(raw[node.start : node.end]).hexdigest(),
            target_node_id=by_locator.get(node.target) if node.target else None,
            cell_states=node.cell_states or None,
        )
        for node in nodes
    ]
    return version, rows


def text_rights(rights: RightsRecord, decision: dict, hashes: list[str]) -> RightsRecord:
    """The work's rights record under the text decision: open, for any reuse, these exact texts."""
    return RightsRecord.model_validate(  # validated: a malformed evidence link fails here, not later
        rights.model_dump()
        | {
            "status": RightsStatus.OPEN,
            "rights_holder": decision["rights_holder"],
            "licence": decision["licence"],
            "licence_url": decision["licence_url"],
            "redistribution_allowed": Permission.YES,
            "commercial_reuse_allowed": Permission.YES,
            "derivatives_allowed": Permission.YES,
            "attribution_required": decision["attribution_required"],
            "attribution_text": decision["attribution_text"],
            "database_rights_notes": decision["database_rights_notes"],
            "privacy_status": decision["privacy_status"],
            "reviewer": decision["decided_by"],
            "reviewed_at": datetime.fromisoformat(decision["decided_at"]),
            "expires_at": datetime.fromisoformat(decision["expires_at"]),
            "evidence": decision["evidence"],
            "legal_note": decision["basis"],
            "approved_content_hashes": sorted(set(hashes)),
        }
    )


def publish_texts(store, jurisdiction: str, texts: Texts, decision: dict, dry_run=False) -> dict:
    """Write each approved text not yet published, with its structure and rights, and withdraw the
    texts whose approval was revoked, in one write (the change log records what changed). A text
    already published with the same bytes and structure is left as it is. Returns what was done."""
    corpus = store.load()
    sources = {s.id: s for s in corpus.sources}
    rights = {r.source_id: r for r in corpus.rights}
    report = {"published": [], "unchanged": [], "withdrawn": [], "skipped": dict(texts.skipped)}
    for item in texts.approved:
        if item.document_type not in decision["document_types"]:
            report["skipped"][item.document_id] = f"the text decision does not cover {item.document_type}s"
            continue
        if item.work not in sources or item.work not in rights:
            report["skipped"][item.document_id] = "its catalogue record is not published: run `records` first"
            continue
        mine = [v for v in corpus.versions if v.source_id == item.work and v.language == item.language]
        same = next(
            (
                v
                for v in mine
                if v.content_hash == item.text.text_sha256
                and v.serialization_profile == item.text.profile_id
                and _structure_of(corpus, v) == _canonical_nodes(item.text)
            ),
            None,
        )
        record = rights[item.work]
        if same is not None:
            rights[item.work] = text_rights(
                record, decision, [*record.approved_content_hashes, same.content_hash]
            )
            report["unchanged"].append(item.document_id)
            continue
        prior = max(mine, key=lambda v: v.content_verified_at or v.retrieved_at, default=None)
        version, nodes = version_from(item, sources[item.work], prior)
        corpus.versions.append(version)
        corpus.structure.extend(nodes)
        rights[item.work] = text_rights(
            record, decision, [*record.approved_content_hashes, version.content_hash]
        )
        names = sorted({v.language for v in corpus.versions if v.source_id == item.work})
        _put(
            corpus,
            "sources",
            sources[item.work].model_copy(
                update={
                    "catalogue_note": f"Text published from the official file after review "
                    f"({', '.join(LANGUAGES.get(code, code) for code in names)}). {decision['basis']}"
                }
            ),
        )
        report["published"].append({"document": item.document_id, "version": version.id, "nodes": len(nodes)})
    for work, language in sorted(texts.revoked):
        record = rights.get(work)
        withdrawn = {
            v.content_hash
            for v in corpus.versions
            if v.source_id == work and v.language == language and v.serialization_profile
        }
        if record is not None and withdrawn & set(record.approved_content_hashes):
            kept = [h for h in record.approved_content_hashes if h not in withdrawn]
            rights[work] = record.model_copy(update={"approved_content_hashes": kept})
            report["withdrawn"].append(f"{work} ({language})")
    for record in rights.values():
        _put(corpus, "rights", record)
    corpus = Corpus.model_validate(corpus.model_dump(mode="json"))  # every span, hash and link checked
    for entry in report["published"]:
        version = next(v for v in corpus.versions if v.id == entry["version"])
        if not rights_decision(rights[version.source_id], version).full_text:
            raise Refused(f"{entry['document']}: the rights record would not serve this text")
    if not dry_run:
        store.save(corpus)
    return report


# ---------- a run: nightly, or asked for from the workbench (the publish job) ----------
class PublicationLog:
    """A run and where each document it looked at stands in public view, in the review database's
    publishing tables (workbench.store), written with the publisher's own login, which can change
    nothing else there."""

    def __init__(self, engine):
        self.engine = engine

    def start(self) -> tuple[int, str, str | None]:
        """Takes up the runs asked for from the workbench (the oldest is this run; those asked for
        since are covered by it), else starts a scheduled one: (run, trigger, who asked)."""
        now = datetime.now(UTC)
        with self.engine.begin() as db:
            asked = db.execute(
                select(publication_run)
                .where(publication_run.c.status == "requested")
                .order_by(publication_run.c.id)
            ).all()
            if asked:
                run = asked[0]
                db.execute(
                    update(publication_run)
                    .where(publication_run.c.id == run.id)
                    .values(status="running", started_at=now)
                )
                for other in asked[1:]:
                    db.execute(
                        update(publication_run)
                        .where(publication_run.c.id == other.id)
                        .values(
                            status="covered", started_at=now, finished_at=now, summary={"covered_by": run.id}
                        )
                    )
                return run.id, "manual", run.requested_by
            run = db.execute(
                insert(publication_run).values(
                    requested_at=now, trigger="schedule", status="running", started_at=now, summary={}
                )
            ).inserted_primary_key[0]
        return run, "schedule", None

    def finish(self, run: int, status: str, summary: dict, error: str | None = None) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(publication_run)
                .where(publication_run.c.id == run)
                .values(status=status, finished_at=datetime.now(UTC), summary=summary, error=error)
            )

    def statuses(self) -> dict[str, dict]:
        with self.engine.connect() as db:
            return {row.document_id: dict(row._mapping) for row in db.execute(select(publication_status))}

    def record(self, run: int, rows: list[dict], previous: dict[str, dict]) -> None:
        """Each document's standing after this run, keeping when it first became public."""
        now = datetime.now(UTC)
        with self.engine.begin() as db:
            for row in rows:
                before = previous.get(row["document_id"]) or {}
                values = {
                    **row,
                    "details_since": (before.get("details_since") or now)
                    if row["details"] == "public"
                    else None,
                    "text_since": (before.get("text_since") or now) if row["text"] == "public" else None,
                    "run_id": run,
                    "updated_at": now,
                }
                done = db.execute(
                    update(publication_status)
                    .where(publication_status.c.document_id == row["document_id"])
                    .values(**values)
                )
                if not done.rowcount:
                    db.execute(insert(publication_status).values(**values))


def _reasons(skipped: dict, most: int = 50) -> dict:
    """Why each document was not published, for the run's report (the first `most`)."""
    return {key: str(why)[:300] for key, why in list(skipped.items())[:most]}


def _count(report: dict) -> dict:
    return {
        key: len(value) if isinstance(value, (list, dict, set)) else value for key, value in report.items()
    }


def publish_country(review, library, review_store, public_store, jurisdiction: str, ids: set[str]):
    """One country's run: its details, then its texts, each under its decision; and where each of its
    documents stands afterwards, in words a reviewer reads."""
    works = {i: library.act(i).get("work") or i for i in ids}
    rows = {
        i: {
            "document_id": i,
            "work": works[i],
            "jurisdiction": jurisdiction,
            "details": "waiting",
            "text": "waiting",
            "reason": "",
        }
        for i in ids
    }
    try:
        decision = current_decision(jurisdiction)
    except Refused as refused:
        for row in rows.values():
            row.update(details="blocked", text="blocked", reason=str(refused))
        return {"blocked": str(refused)}, list(rows.values())
    approved, waiting = from_workbench(review, library, review_store, jurisdiction, only=ids)
    records = publish_records(public_store, jurisdiction, approved, waiting, decision)
    public = set(records["published"]) | set(records["updated"])
    for i, row in rows.items():
        work = works[i]
        if work in public:
            row["details"] = "public"
        elif work in records["skipped"]:
            row.update(details="blocked", reason=records["skipped"][work])
        elif work in records["taken_down"]:
            row.update(details="taken_down", reason="its approval lapsed or was revoked")
        elif work in waiting:
            row["reason"] = "its approval is not current: approve it again"
    summary = {
        "decision_until": decision["expires_at"][:10],
        "records": _count(records),
        "records_skipped": _reasons(records["skipped"]),
    }
    try:
        text_decision = current_text_decision(jurisdiction)
    except Refused as refused:
        for i, row in rows.items():
            if row["details"] == "public":
                held = library.act(i).get("text_held")
                row.update(text="held" if held else "no_decision", reason="" if held else str(refused))
        summary["texts"] = {"blocked": str(refused)}
        return summary, list(rows.values())
    texts = texts_from_workbench(review, library, review_store, jurisdiction, only=ids)
    report = publish_texts(public_store, jurisdiction, texts, text_decision)
    published = {entry["document"] for entry in report["published"]} | set(report["unchanged"])
    withdrawn = set(report["withdrawn"])
    for i, row in rows.items():
        act = library.act(i)
        if i in published:
            row["text"] = "public"
        elif i in report["skipped"]:
            why = report["skipped"][i]
            kind = (
                "held"
                if why.startswith("its text is held")
                else "not_covered"
                if "does not cover" in why
                else "blocked"
            )
            row["text"] = kind
            row["reason"] = row["reason"] or why
        elif f"{works[i]} ({act.get('language') or 'en'})" in withdrawn:
            row["text"] = "withdrawn"
    summary["text_decision_until"] = text_decision["expires_at"][:10]
    summary["texts"] = _count(report)
    summary["texts_skipped"] = _reasons(report["skipped"])
    return summary, list(rows.values())


def run(review, library, review_store, public_store, log: PublicationLog) -> dict:
    """A publishing run: every country with an approved document, its details then its texts, and
    each document's standing recorded for the workbench. A failure is recorded and raised."""
    number, trigger, by = log.start()
    summary: dict = {"trigger": trigger, "requested_by": by, "countries": {}}
    try:
        named = review_store.approved_ids()
        listed = {d["id"]: d for d in library.documents() if d["id"] in named and not d.get("part")}
        previous = log.statuses()
        rows: list[dict] = []
        for country in sorted({d.get("jurisdiction") for d in listed.values() if d.get("jurisdiction")}):
            ids = {i for i, d in listed.items() if d.get("jurisdiction") == country}
            summary["countries"][country], found = publish_country(
                review, library, review_store, public_store, country, ids
            )
            rows += found
        log.record(number, rows, previous)
    except Exception as error:
        log.finish(number, "failed", summary, f"{type(error).__name__}: {error}"[:2000])
        raise
    log.finish(number, "succeeded", summary)
    return {"run": number, **summary}


def _canonical_nodes(text) -> list[tuple]:
    return [
        (n.kind, n.locator, n.label or None, n.heading or None, n.start, n.end, n.cell_states or None)
        for n in text.structure.nodes
    ]


def _structure_of(corpus: Corpus, version: SourceVersion) -> list[tuple]:
    rows = [n for n in corpus.structure if n.version_id == version.id]
    by_id = {n.id: n for n in rows}

    def depth_first(node):
        yield node
        for child in sorted((n for n in rows if n.parent_id == node.id), key=lambda n: n.order):
            yield from depth_first(child)

    roots = [n for n in rows if n.parent_id not in by_id]
    ordered = [n for root in sorted(roots, key=lambda n: n.order) for n in depth_first(root)]
    return [(n.kind, n.locator, n.label, n.heading, n.start_byte, n.end_byte, n.cell_states) for n in ordered]


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
    decide_text_cmd = commands.add_parser("decide-text")
    decide_text_cmd.add_argument("--jurisdiction", required=True)
    decide_text_cmd.add_argument("--by", required=True, help="Who decides: a full name")
    decide_text_cmd.add_argument("--months", type=int, default=12)
    decide_text_cmd.add_argument("--evidence", action="append", default=[])
    decide_text_cmd.add_argument("--types", nargs="+", default=["act", "constitution"])
    decide_text_cmd.add_argument("--rights-holder", required=True)
    decide_text_cmd.add_argument("--licence", required=True, help="The terms, or why none are needed")
    decide_text_cmd.add_argument("--licence-url")
    decide_text_cmd.add_argument("--attribution", help="Attribution readers must give, if any")
    decide_text_cmd.add_argument("--database-rights", default="")
    decide_text_cmd.add_argument("--basis", required=True, help="Why these texts may be republished")
    decide_text_cmd.add_argument("--privacy-checked", action="store_true")
    texts_cmd = commands.add_parser("texts")
    texts_cmd.add_argument("--jurisdiction", required=True)
    texts_cmd.add_argument("--dry-run", action="store_true")
    commands.add_parser("run", help="every country's details and texts, as the publish job runs them")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            settings.load()
            engine = create_engine(os.environ["ALVARY_REVIEW_DATABASE_URL"])  # the publisher's login
            review_store = ReviewStore(engine)
            library = Library()
            summary = run(
                Review(review_store, library, registry()),
                library,
                review_store,
                Store(engine_for()),
                PublicationLog(engine),
            )
            print(json.dumps(summary, indent=2, default=str))
            return 0
        if args.command == "decide-text":
            decision = decide_text(
                args.jurisdiction,
                args.by,
                args.months,
                args.evidence,
                types=args.types,
                rights_holder=args.rights_holder,
                licence=args.licence,
                licence_url=args.licence_url,
                attribution=args.attribution,
                database_rights=args.database_rights,
                basis=args.basis,
                privacy_checked=args.privacy_checked,
            )
            print(
                f"Recorded {text_decision_path(args.jurisdiction).name}, revision {decision['revision']}, "
                f"until {decision['expires_at'][:10]}. Commit it with the texts it publishes."
            )
            return 0
        if args.command == "texts":
            decision = current_text_decision(args.jurisdiction)
            current_decision(args.jurisdiction)  # a text is published only beside its catalogue record
            settings.load()
            review_store = ReviewStore(create_engine(os.environ["ALVARY_REVIEW_DATABASE_URL"]))  # read only
            library = Library()
            texts = texts_from_workbench(
                Review(review_store, library, registry()), library, review_store, args.jurisdiction
            )
            report = publish_texts(Store(engine_for()), args.jurisdiction, texts, decision, args.dry_run)
            print(json.dumps(report, indent=2))
            return 0
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
