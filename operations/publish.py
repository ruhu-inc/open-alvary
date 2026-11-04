"""Publish approved works from the review workbench to the public catalogue (LIF-01 approved →
published), catalogue records first (owner decision, 2026-10-16).

Only the record is published: title, number, year, citation, legal status, issuer, the official
publication link, what each language version is worth as law and the work's rule where they differ,
a regional instrument's legal effect and a treaty's parties and entry into force, from the work's
confirmed details. No text: republishing official texts waits for
the copyright check (Rwanda: Law N° 055/2024) and the public reader (MAC-01). A work is published
when the latest approval of at least one of its language versions still covers its current pages and
its confirmed details (the workbench's own test, workbench.review). Each change is recorded in the
change log (changes.record) by the same write.

Nothing is published without the owner's collection decision for the jurisdiction
(ingestion/decisions/<jurisdiction>-catalogue.json, REV-01: once per collection, with evidence and
an expiry). One person may record it and approve the works (single-reviewer rule, migration 0019).

Texts follow their records under a separate decision (ingestion/decisions/<jurisdiction>-text.json,
TXP-07): the reviewed text of each approved language version, its legal structure and its rights,
exactly as its approval bound them, or nothing.

What comes down. Each run works out, for each work and language, what current approvals bind (Texts),
and takes down every reviewed text they do not bind and every work with no current approval (take_down),
before it looks at a decision: a missing or expired decision stops new publication, never a take-down.
A correction read from the same official file replaces the text it corrects. A text read from an earlier
official file stays as history for citations to it (TXP-04) while its language has a current approval.
Names withheld in a sensitive case take down every text of the work, for a reason that is not told.

When it does nothing. A run reads everything before it writes anything: if it cannot read the approvals
it publishes nothing, takes nothing down and fails. It refuses, the same way, what looks like a wrong
collection or review database rather than a decision: an empty read of the approvals beside public
records or texts, a collection holding none of the approved documents, or more take-downs resting only on
not finding a current approval than check_take_downs allows. An operator who has looked lets the run
through with --allow-take-downs N. A revocation, withheld names, a held text and a replaced text are each
a decision someone recorded, so no count holds them back. A rights record whose privacy review is blocked
is an operator's hold: no run publishes over it.

  python -m operations.publish decide --jurisdiction rw --by "Full Name" [--months 12] [--evidence URL]...
  python -m operations.publish records --jurisdiction rw [--dry-run] [--allow-take-downs N]
  python -m operations.publish decide-text --jurisdiction ng --by "Full Name" --rights-holder … --licence …
      --basis … --evidence URL --privacy-checked [--types act constitution] [--attribution TEXT]
  python -m operations.publish texts --jurisdiction ng [--dry-run] [--allow-take-downs N]
  python -m operations.publish run [--allow-take-downs N]

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
from rights.policy import PRIVACY_CLEARED
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
    store,
    jurisdiction: str,
    approved: list[Approved],
    waiting: set[str],
    decision: dict,
    dry_run=False,
    confidential=(),
) -> dict:
    """Merge approved works into the catalogue and take down those no longer approved, in one write
    (the change log records what changed; a work in `confidential` that comes down is not named).
    Returns what was done."""
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
        existing = by_id["rights"].get(entry["rights"].id)
        if existing is not None and existing.privacy_status == "blocked" and not existing.metadata_public:
            report["skipped"][item.work] = (
                "an operator withdrew it for a confidential reason: only they lift it"
            )
            continue
        for code in entry["languages"]:
            if code not in by_id["languages"]:
                language = Language(id=code, name=LANGUAGES.get(code, code), tag=code)
                corpus.languages.append(language)
                by_id["languages"][code] = language
        if entry["authority"].id not in by_id["authorities"]:
            corpus.authorities.append(entry["authority"])
            by_id["authorities"][entry["authority"].id] = entry["authority"]
        for alias in entry["citations"]:
            _put(corpus, "citations", alias)
        if existing is not None and _owned_beyond_records(existing):
            # A later text decision or an operator's hold owns this rights record and the note on what
            # text there is, with or without a text public now; the records step never narrows either.
            entry["rights"] = existing.model_copy(update={"metadata_public": True})
            held = by_id["sources"].get(item.work)
            if held is not None:
                entry["source"] = entry["source"].model_copy(update={"catalogue_note": held.catalogue_note})
        _put(corpus, "sources", entry["source"])
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
    report["taken_down"] = _take_down_records(corpus, waiting)
    if not dry_run:
        store.save(corpus, confidential=confidential)
    return report


def _owned_beyond_records(rights: RightsRecord) -> bool:
    """Whether a text decision has spoken for this rights record, or an operator holds it."""
    return bool(
        rights.approved_content_hashes
        or rights.status in (RightsStatus.OPEN, RightsStatus.OPEN_WITH_CONDITIONS)
        or rights.privacy_status == "blocked"
    )


def _take_down_records(corpus: Corpus, waiting: set[str]) -> list[str]:
    """Stop showing each public work in `waiting`: kept, so the change log says so (a save never deletes
    a row) and an approval given again brings it back. Returns the works taken down."""
    sources = {s.id for s in corpus.sources}
    rights = {r.id: r for r in corpus.rights}
    down = []
    for work in sorted(waiting):
        record = rights.get(f"rights-{work}")
        if work in sources and record is not None and record.metadata_public:
            _put(corpus, "rights", record.model_copy(update={"metadata_public": False}))
            down.append(work)
    return down


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
class Bound:
    """What the current approval of one language version of a work binds: the exact texts, and the
    official files they were read from. Nothing, when no approval of it is current."""

    hashes: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


@dataclass
class Texts:
    approved: list[ApprovedText] = field(default_factory=list)
    revoked: set[tuple[str, str]] = field(default_factory=set)  # (work, language) whose approval was revoked
    skipped: dict[str, str] = field(default_factory=dict)
    # Each language version looked at: (work, language) -> what its current approval binds. It binds what it
    # bound whether or not this run could publish from it: rules that changed stop a new text, never one
    # that is public exactly as its approval bound it.
    bound: dict[tuple[str, str], Bound] = field(default_factory=dict)
    held: set[tuple[str, str]] = field(default_factory=set)  # approved on its details alone
    absent: set[tuple[str, str]] = field(default_factory=set)  # public, with no document here to say why
    withheld: set[str] = field(default_factory=set)  # works with names withheld: no text of them is public

    def binding(self) -> dict[tuple[str, str], Bound]:
        """What is known of each language version, the texts to publish now included."""
        found = {key: Bound(set(b.hashes), set(b.files)) for key, b in self.bound.items()}
        for key in self.revoked:
            found.setdefault(key, Bound())
        for item in self.approved:
            bound = found.setdefault((item.work, item.language), Bound())
            bound.hashes.add(item.text.text_sha256)
            if item.provenance.get("sha256"):
                bound.files.add(item.provenance["sha256"])
        return found


def names_withheld(now: dict) -> bool:
    """Whether a document's details, approved or not, withhold names in a sensitive case (owner,
    2026-10-27); read from what was saved, since an approval lapses the moment they are changed."""
    values = (now.get("view") or {}).get("values") or {}
    return (values.get("profile.anonymisation") or "none") != "none"


NAMES_WITHHELD = (
    "names are withheld in this case: its text is not published (names in a text are not yet masked)"
)


def texts_from_workbench(review, library, store, jurisdiction: str, only=None) -> Texts:
    """Each language version whose latest approval covers its current pages, details and text, built
    again from those pages; refused when the text or structure differ from what the approval bound.
    With them, what the current approval of each language version binds, publishable or not. Of the
    documents in `only` when given."""
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
        bound = found.bound.setdefault((work, language), Bound())
        if names_withheld(now):
            found.withheld.add(work)
        if head and head["decision"] == "revoked":
            found.revoked.add((work, language))
        if not now["approved"]:
            continue
        if act.get("text_held"):  # approved on its details alone (owner, 2026-10-25)
            found.held.add((work, language))
            found.skipped[act["id"]] = f"its text is held: {act['text_held'].get('note', 'not publishable')}"
            continue
        if work in found.withheld:
            found.skipped[act["id"]] = NAMES_WITHHELD
            continue
        provenance = library.provenance(act["id"])
        if head.get("text_sha256"):
            bound.hashes.add(head["text_sha256"])
        if provenance.get("sha256"):
            bound.files.add(provenance["sha256"])
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
    # Details are shared by a work's language versions, so one withheld is all of them; whichever order
    # they were read in, no text of the work goes out, and what they bound binds nothing.
    for item in found.approved:
        if item.work in found.withheld:
            found.skipped[item.document_id] = NAMES_WITHHELD
    found.approved = [item for item in found.approved if item.work not in found.withheld]
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


def _text_note(corpus: Corpus, record: RightsRecord) -> str:
    """What a record says of its texts: the languages public now, and why they may be republished."""
    names = sorted(
        {
            v.language
            for v in corpus.versions
            if v.source_id == record.source_id and v.content_hash in record.approved_content_hashes
        }
    )
    return (
        f"Text published from the official file after review "
        f"({', '.join(LANGUAGES.get(code, code) for code in names)}). {record.legal_note}"
    )


NO_TEXT_NOTE = "Text not published at present. Read the text at its official publication."
# Why a reviewed text comes down, where nothing but not finding a current approval says it should: what a
# wrong collection or review database would produce, so check_take_downs counts them. Every other reason is
# something recorded: "revoked", "held", "withheld", "replaced".
INFERRED = ("lapsed", "absent")


@dataclass
class Unbound:
    version: str
    work: str
    language: str
    content_hash: str
    why: str


def _why_unbound(version: SourceVersion, key, bound: Bound, corrected: set[str], texts: Texts) -> str | None:
    """Why a public reviewed text must come down, or None when it stays: exactly what a current approval
    binds, or the history of one (TXP-04), a text read from another official file that no later reading
    of that file corrected, still true of that file and still cited."""
    if key[0] in texts.withheld:
        return "withheld"
    if version.content_hash in bound.hashes:
        return None
    if bound.hashes:
        history = bound.files and version.raw_hash not in bound.files and version.id not in corrected
        return None if history else "replaced"
    if key in texts.revoked:
        return "revoked"
    if key in texts.held:
        return "held"
    return "absent" if key in texts.absent else "lapsed"


def _public_reviewed_texts(corpus: Corpus) -> list[SourceVersion]:
    """The texts published through review whose hash a rights record approves."""
    rights = {r.source_id: r for r in corpus.rights}
    return [
        v
        for v in corpus.versions
        if v.serialization_profile
        and v.source_id in rights
        and v.content_hash in rights[v.source_id].approved_content_hashes
    ]


def unbound_texts(corpus: Corpus, texts: Texts) -> list[Unbound]:
    """Every public reviewed text of the language versions in `texts` that must come down. A text not
    published through review has no approval to read and is left alone."""
    rights = {r.source_id: r for r in corpus.rights}
    known = texts.binding()
    reviewed: dict[tuple[str, str], list[SourceVersion]] = {}
    for version in corpus.versions:
        if version.serialization_profile:
            reviewed.setdefault((version.source_id, version.language), []).append(version)
    found = []
    for key, versions in sorted(reviewed.items()):
        record = rights.get(key[0])
        if record is None or (key not in known and key[0] not in texts.withheld):
            continue
        by_id = {v.id: v for v in versions}
        corrected = {
            v.prior_version_id
            for v in versions
            if v.prior_version_id in by_id and by_id[v.prior_version_id].raw_hash == v.raw_hash
        }
        for version in versions:
            if version.content_hash not in record.approved_content_hashes:
                continue
            why = _why_unbound(version, key, known.get(key) or Bound(), corrected, texts)
            if why:
                found.append(Unbound(version.id, key[0], key[1], version.content_hash, why))
    return found


def take_down_texts(corpus: Corpus, texts: Texts) -> dict:
    """Take every unbound text out of its rights record's approved hashes (Corpus edited, nothing written).
    Names withheld also leave the record's privacy review not cleared, so the change log does not tell why,
    and the record's note as it was, so nothing names the record beside a withdrawal that names nothing.
    Otherwise the note follows, except while a corrected text waits to replace the one taken down (what
    publishes it says so)."""
    report = {"withdrawn": [], "superseded": [], "unbound_hashes": []}
    rights = {r.source_id: r for r in corpus.rights}
    sources = {s.id: s for s in corpus.sources}
    known = texts.binding()
    unbound = unbound_texts(corpus, texts)
    leaving = {item.version for item in unbound}
    for work in sorted({key[0] for key in known} | {item.work for item in unbound} | texts.withheld):
        record = rights.get(work)
        if record is None:
            continue
        versions = [v for v in corpus.versions if v.source_id == work]
        kept = {v.content_hash for v in versions if v.id not in leaving}
        mine = [item for item in unbound if item.work == work]
        bound = {h for key, found in known.items() if key[0] == work for h in found.hashes}
        if work in texts.withheld:
            bound = set()
        gone = {item.content_hash for item in mine} - kept
        all_hashes = {v.content_hash for v in versions}
        stray = {h for h in record.approved_content_hashes if h not in all_hashes and h not in bound}
        hold = work in texts.withheld and record.privacy_status in PRIVACY_CLEARED
        if not gone and not stray and not hold:
            continue
        for item in mine:
            if item.content_hash not in gone:
                continue  # the same bytes are public in another version of the work
            if item.why == "replaced":
                report["superseded"].append(item.version)
            elif f"{work} ({item.language})" not in report["withdrawn"]:
                report["withdrawn"].append(f"{work} ({item.language})")
        report["unbound_hashes"].extend(sorted(stray))
        remaining = [h for h in record.approved_content_hashes if h not in gone | stray]
        record = record.model_copy(
            update={"approved_content_hashes": remaining} | ({"privacy_status": "pending"} if hold else {})
        )
        _put(corpus, "rights", record)
        source = sources.get(work)
        if source is None or not gone or work in texts.withheld:
            continue
        note = _text_note(corpus, record) if remaining else NO_TEXT_NOTE if not bound else None
        if note:
            _put(corpus, "sources", source.model_copy(update={"catalogue_note": note}))
    return report


def note_absent(corpus: Corpus, jurisdiction: str, texts: Texts) -> None:
    """Add to `texts` each public reviewed text of this jurisdiction that no document looked at accounts
    for: its document has left the collection, or no approval names it. No current approval can be shown
    to bind it, so it comes down, within what check_take_downs allows."""
    places = {s.id for s in corpus.sources if s.jurisdiction_id == jurisdiction}
    known = texts.binding()
    for version in _public_reviewed_texts(corpus):
        key = (version.source_id, version.language)
        if version.source_id in places and key not in known:
            texts.bound[key] = Bound()
            texts.absent.add(key)


def take_down(store, jurisdiction: str, waiting: set[str], texts: Texts, dry_run=False) -> dict:
    """Take down every work with no current approval and every reviewed text no current approval binds,
    in one write that needs no decision (the change log records what changed; a work whose names are
    withheld is not named). Writes nothing when there is nothing to do. Returns what was done, with what
    stays public after it."""
    corpus = store.load()
    note_absent(corpus, jurisdiction, texts)
    down = _take_down_records(corpus, waiting)
    report = take_down_texts(corpus, texts)
    if not dry_run and (down or any(report.values())):
        store.save(corpus, confidential=texts.withheld)
    rights = {r.source_id: r for r in corpus.rights}
    return report | {
        "taken_down": down,
        "records_public": {r.source_id for r in corpus.rights if rights_decision(r).metadata},
        "texts_public": {
            (v.source_id, v.language)
            for v in corpus.versions
            if rights_decision(rights.get(v.source_id), v).full_text
        },
    }


def planned_take_downs(corpus: Corpus, works: set[str], waiting: set[str], texts: Texts) -> tuple[int, int]:
    """Of the works a run looks at and those with a public reviewed text: how many it would take something
    down from on nothing but not finding a current approval, and how many are public."""
    rights = {r.source_id: r for r in corpus.rights}
    revoked = {key[0] for key in texts.revoked}
    records = {w for w in works if w in rights and rights[w].metadata_public}
    readable = {v.source_id for v in _public_reviewed_texts(corpus)}
    places = {key[0] for key in texts.binding()}
    inferred = {w for w in waiting if w in records and w not in revoked}
    inferred |= {item.work for item in unbound_texts(corpus, texts) if item.why in INFERRED}
    return len(inferred), len(records | (readable & places))


# More than this share of a country's public works, or this many at least, is not a day's lapses.
MASS_TAKE_DOWN_SHARE = 0.10
MASS_TAKE_DOWN_FLOOR = 5
WHAT_TO_DO = (
    "Nothing was published or taken down. Check ALVARY_REVIEW_COLLECTIONS and the review database; "
    "if this is right, run `python -m operations.publish {command} --allow-take-downs {count}`."
)


def check_take_downs(jurisdiction: str, inferred: int, public: int, allowed: int, command="run") -> None:
    """Refuse what looks like a wrong collection or review database rather than decisions: more take-downs
    resting only on not finding a current approval than a day's lapses, or every public work of a country
    at once, unless an operator has allowed that many."""
    limit = max(MASS_TAKE_DOWN_FLOOR, int(public * MASS_TAKE_DOWN_SHARE))
    if inferred <= allowed or not (inferred > limit or (inferred == public and public > 1)):
        return
    raise Refused(
        f"This run would take down {inferred} of the {public} works public in {jurisdiction} because no "
        "current approval could be found for them, which looks like a wrong collection or review database. "
        + WHAT_TO_DO.format(command=command, count=inferred)
    )


def publish_texts(store, jurisdiction: str, texts: Texts, decision: dict, dry_run=False) -> dict:
    """Write each approved text not yet published, with its structure and rights, and take down the
    texts no current approval binds, in one write (the change log records what changed), so a correction
    replaces what it corrects. A text already published with the same bytes and structure is left as it
    is. Returns what was done."""
    corpus = store.load()
    sources = {s.id: s for s in corpus.sources}
    rights = {r.source_id: r for r in corpus.rights}
    report = {"published": [], "unchanged": [], "skipped": dict(texts.skipped)}
    named = set()  # works whose note must say a text is back or new
    for item in texts.approved:
        if item.document_type not in decision["document_types"]:
            report["skipped"][item.document_id] = f"the text decision does not cover {item.document_type}s"
            continue
        if item.work not in sources or item.work not in rights:
            report["skipped"][item.document_id] = "its catalogue record is not published: run `records` first"
            continue
        if rights[item.work].privacy_status == "blocked":
            report["skipped"][item.document_id] = (
                "its record's privacy review is blocked: only an operator can clear it"
            )
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
            if same.content_hash not in record.approved_content_hashes:
                named.add(item.work)
            rights[item.work] = text_rights(
                record, decision, [*record.approved_content_hashes, same.content_hash]
            )
            report["unchanged"].append(item.document_id)
            continue
        prior = max(mine, key=lambda v: v.content_verified_at or v.retrieved_at, default=None)
        if record.privacy_status not in PRIVACY_CLEARED:
            prior = None  # a text withdrawn for a reason not told is not linked to the work by its successor
        version, nodes = version_from(item, sources[item.work], prior)
        corpus.versions.append(version)
        corpus.structure.extend(nodes)
        rights[item.work] = text_rights(
            record, decision, [*record.approved_content_hashes, version.content_hash]
        )
        named.add(item.work)
        report["published"].append({"document": item.document_id, "version": version.id, "nodes": len(nodes)})
    for record in rights.values():
        _put(corpus, "rights", record)
    for work in sorted(named):
        note = _text_note(corpus, rights[work])
        _put(corpus, "sources", sources[work].model_copy(update={"catalogue_note": note}))
    report |= take_down_texts(corpus, texts)
    corpus = Corpus.model_validate(corpus.model_dump(mode="json"))  # every span, hash and link checked
    rights = {r.source_id: r for r in corpus.rights}
    for entry in report["published"]:
        version = next(v for v in corpus.versions if v.id == entry["version"])
        if not rights_decision(rights[version.source_id], version).full_text:
            raise Refused(f"{entry['document']}: the rights record would not serve this text")
    if not dry_run:
        store.save(corpus, confidential=texts.withheld)
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


@dataclass
class Reading:
    """What one country's run needs from the review database and the collections: all of it read before
    anything is written, so a run that cannot read it changes nothing."""

    jurisdiction: str
    documents: dict[str, dict]  # document -> what the library says of it
    approved: list[Approved]
    waiting: set[str]
    texts: Texts

    def work(self, document: str) -> str:
        return self.documents[document].get("work") or document

    def key(self, document: str) -> tuple[str, str]:
        return self.work(document), self.documents[document].get("language") or "en"


def read_country(review, library, review_store, jurisdiction: str, ids: set[str]) -> Reading:
    approved, waiting = from_workbench(review, library, review_store, jurisdiction, only=ids)
    texts = texts_from_workbench(review, library, review_store, jurisdiction, only=ids)
    return Reading(jurisdiction, {i: library.act(i) for i in ids}, approved, waiting, texts)


def publish_country(reading: Reading, public_store, taken: dict, previous: dict[str, dict]):
    """One country's run, after its take-downs (`taken`): its details, then its texts, each under its
    decision; and where each of its documents stands afterwards, in words a reviewer reads."""
    jurisdiction = reading.jurisdiction
    withdrawn = set(taken["withdrawn"])
    rows = {
        i: {
            "document_id": i,
            "work": reading.work(i),
            "jurisdiction": jurisdiction,
            "details": "waiting",
            "text": "waiting",
            "reason": "",
        }
        for i in reading.documents
    }
    for i, row in rows.items():
        if reading.work(i) in taken["taken_down"]:
            row.update(details="taken_down", reason="its approval lapsed or was revoked")
        if f"{reading.work(i)} ({reading.key(i)[1]})" in withdrawn:
            row.update(text="withdrawn", reason=row["reason"] or "its approval lapsed or was revoked")

    def settled():
        """Each row, with what is true of its text whatever this run could do: a text still public is
        public, and one public before that this run did not publish has been withdrawn."""
        for i, row in rows.items():
            if row["text"] == "withdrawn":
                continue
            if row["text"] != "public" and reading.key(i) in taken["texts_public"]:
                row["text"] = "public"
            elif row["text"] == "waiting" and (previous.get(i) or {}).get("text") in ("public", "withdrawn"):
                row["text"] = "withdrawn"
        return list(rows.values())

    done = {"taken_down": len(taken["taken_down"])}
    undone = {"withdrawn": len(taken["withdrawn"]), "superseded": len(taken["superseded"])}
    try:
        decision = current_decision(jurisdiction)
    except Refused as refused:
        for i, row in rows.items():
            if row["details"] == "taken_down":
                continue
            still = reading.work(i) in taken["records_public"]
            row.update(details="public" if still else "blocked", reason=str(refused))
            if row["text"] == "waiting" and not still:
                row["text"] = "blocked"
        return {"blocked": str(refused), "records": done, "texts": undone}, settled()
    records = publish_records(public_store, jurisdiction, reading.approved, set(), decision)
    public = set(records["published"]) | set(records["updated"])
    for i, row in rows.items():
        work = reading.work(i)
        if work in public:
            row["details"] = "public"
        elif work in records["skipped"]:
            row.update(details="blocked", reason=records["skipped"][work])
        elif work in reading.waiting and row["details"] != "taken_down":
            row["reason"] = "its approval is not current: approve it again"
    summary = {
        "decision_until": decision["expires_at"][:10],
        "records": _count(records) | done,
        "records_skipped": _reasons(records["skipped"]),
    }
    try:
        text_decision = current_text_decision(jurisdiction)
    except Refused as refused:
        for i, row in rows.items():
            if row["details"] != "public" or row["text"] != "waiting":
                continue
            if reading.documents[i].get("text_held"):
                row["text"] = "held"
            elif reading.texts.bound[reading.key(i)].hashes:
                row.update(text="no_decision", reason=str(refused))
        summary["texts"] = {"blocked": str(refused)} | undone
        return summary, settled()
    report = publish_texts(public_store, jurisdiction, reading.texts, text_decision)
    published = {entry["document"] for entry in report["published"]} | set(report["unchanged"])
    for i, row in rows.items():
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
    summary["text_decision_until"] = text_decision["expires_at"][:10]
    summary["texts"] = _count(report)
    for key, count in undone.items():
        summary["texts"][key] += count
    summary["texts_skipped"] = _reasons(report["skipped"])
    return summary, settled()


def run(review, library, review_store, public_store, log: PublicationLog, *, allow_take_downs=0) -> dict:
    """A publishing run. Everything is read first, and a read that fails or looks wrong stops the run with
    nothing changed. Then every country's take-downs, which no decision can stop and no later failure can
    undo; then every country with an approved document, its details then its texts; and each document's
    standing recorded for the workbench. A failure is recorded and raised."""
    number, trigger, by = log.start()
    summary: dict = {"trigger": trigger, "requested_by": by, "countries": {}}
    try:
        named = review_store.approved_ids()
        documents = library.documents()
        listed = {d["id"]: d for d in documents if d["id"] in named and not d.get("part")}
        missing = named - {d["id"] for d in documents}
        if missing:  # what they published comes down, within check_take_downs: said on the run either way
            summary["not_in_collection"] = sorted(missing)[:50]
        previous = log.statuses()
        corpus = public_store.load()
        public_texts = _public_reviewed_texts(corpus)
        places = {s.id: s.jurisdiction_id for s in corpus.sources}
        countries = {d.get("jurisdiction") for d in listed.values() if d.get("jurisdiction")}
        countries |= {places[v.source_id] for v in public_texts}
        readings = []
        for country in sorted(countries):
            ids = {i for i, d in listed.items() if d.get("jurisdiction") == country}
            reading = read_country(review, library, review_store, country, ids)
            note_absent(corpus, country, reading.texts)
            readings.append(reading)
        planned = {
            reading.jurisdiction: planned_take_downs(
                corpus, {reading.work(i) for i in reading.documents}, reading.waiting, reading.texts
            )
            for reading in readings
        }
        total = sum(inferred for inferred, _ in planned.values())
        looks_empty = not named and (
            public_texts or any("public" in (s["details"], s["text"]) for s in previous.values())
        )
        if looks_empty and allow_take_downs < max(total, 1):
            raise Refused(
                "The review database names no approval, yet reviewed records or texts are public. "
                + WHAT_TO_DO.format(command="run", count=total)
            )
        if named and not listed and allow_take_downs < max(total, 1):
            raise Refused(
                f"The collection holds none of the {len(named)} documents the approvals name. "
                + WHAT_TO_DO.format(command="run", count=total)
            )
        for country, (inferred, public) in planned.items():
            check_take_downs(country, inferred, public, allow_take_downs)
        taken, summary["take_downs"] = {}, {}
        for reading in readings:
            done = take_down(public_store, reading.jurisdiction, reading.waiting, reading.texts)
            taken[reading.jurisdiction] = done
            summary["take_downs"][reading.jurisdiction] = {
                key: len(done[key]) for key in ("taken_down", "withdrawn", "superseded", "unbound_hashes")
            }
        rows: list[dict] = []
        for reading in readings:
            summary["countries"][reading.jurisdiction], found = publish_country(
                reading, public_store, taken[reading.jurisdiction], previous
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


def _here(listed: dict, jurisdiction: str) -> bool:
    return listed.get("jurisdiction") == jurisdiction and not listed.get("part")


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
    allowance = argparse.ArgumentParser(add_help=False)
    allowance.add_argument(
        "--allow-take-downs",
        type=int,
        default=0,
        metavar="N",
        help="Having looked: let through up to N take-downs in a country that rest only on not finding a "
        "current approval (check_take_downs)",
    )
    records = commands.add_parser("records", parents=[allowance])
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
    texts_cmd = commands.add_parser("texts", parents=[allowance])
    texts_cmd.add_argument("--jurisdiction", required=True)
    texts_cmd.add_argument("--dry-run", action="store_true")
    commands.add_parser(
        "run", parents=[allowance], help="every country's details and texts, as the publish job runs them"
    )
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
                allow_take_downs=args.allow_take_downs,
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
            store = Store(engine_for())
            corpus = store.load()
            note_absent(corpus, args.jurisdiction, texts)
            inferred, public = planned_take_downs(corpus, set(), set(), texts)
            check_take_downs(args.jurisdiction, inferred, public, args.allow_take_downs, command="texts")
            report = publish_texts(store, args.jurisdiction, texts, decision, args.dry_run)
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
        review = Review(review_store, library, registry())
        approved, waiting = from_workbench(review, library, review_store, args.jurisdiction)
        withheld = {
            act.get("work") or act["id"]
            for act in map(library.act, (d["id"] for d in library.documents() if _here(d, args.jurisdiction)))
            if names_withheld(review.state(act))
        }
        store = Store(engine_for())
        inferred, public = planned_take_downs(
            store.load(), {a.work for a in approved} | waiting, waiting, Texts()
        )
        check_take_downs(args.jurisdiction, inferred, public, args.allow_take_downs, command="records")
        report = publish_records(
            store, args.jurisdiction, approved, waiting, decision, args.dry_run, confidential=withheld
        )
        print(json.dumps(report, indent=2))
        return 0
    except Refused as refused:
        raise SystemExit(str(refused)) from None


if __name__ == "__main__":
    raise SystemExit(main())
