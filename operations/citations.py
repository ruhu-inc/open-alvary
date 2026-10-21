"""A judgment's citations and judicial treatment (EDT-01, EDT-02, DAT-06): proposed from its published
paragraphs, reviewed in the workbench, published by the operator.

  python -m operations.citations propose --work ng-sc-2019-4 > proposals.json    proposals (no writes)
  python -m operations.citations publish ng-sc-2019-4.citations.json [--dry-run]

``propose`` reads each numbered paragraph of the judgment's public text and the citations written in it:
a provision's pinpoint with the work before or after it ("s. 2(1) of the Nigeria Data Protection Act
2023", "NDPA s. 23"), and a judgment's neutral citation with its paragraph ("[2019] NGSC 4 at [12]"). Each
is resolved with the catalogue's own citation resolver (api.catalogue, api.pinpoint), never guessed: a
proposal names the citing paragraph, the cited public work and, when the pinpoint resolves to one part of
its public text, that part. Proposals are imported into the judgment's review in the workbench
(workbench.citations); nothing proposed is published.

A citations file, exported from the workbench, holds the confirmed and withdrawn citations, each with its
structural review (who, in what role, when) and its judicial treatment, if a doctrinal review recorded
one with the reviewer's legal qualification, which a treatment is refused without. Publishing writes them
and announces each treatment in the change log (treatment_recorded), in one transaction. Published
citations and treatments are never edited: one is withdrawn, and a new one confirmed in its place.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import text

from api import pinpoint

UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
CITATION_ID = rf"^cit_{UUID4}$"
TREATMENT_ID = rf"^trt_{UUID4}$"
# How a paragraph treats the judgment it cites: our own vocabulary (EDT-02), never inferred from a citation.
TREATMENTS = Literal["follows", "applies", "distinguishes", "doubts", "overrules"]
# The opinion the treating paragraph is in, as the reviewer states it: a dissent's words are not a holding.
OPINIONS = Literal["majority", "concurrence", "dissent", "plurality", "order", "unknown"]
RULES = "citations-1"  # the rules propose_from reads citations by
MAX_QUOTE = 1000  # a proposal's passage: the paragraph's words, or the sentences around the citation
MAX_WORDS = 10  # words read before or after a pinpoint for the work it is in
_SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[\"“‘'(\[]?[A-Z0-9])")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TreatmentIn(Record):
    """How the citing paragraph treats the judgment it cites, from a doctrinal review."""

    id: str = Field(pattern=TREATMENT_ID)
    kind: TREATMENTS
    opinion: OPINIONS = "unknown"
    quote: str = Field(min_length=1, max_length=2000)  # the passage of the paragraph that does it
    review_level: Literal["doctrinal"] = "doctrinal"
    review_state: Literal["reviewed", "withdrawn"] = "reviewed"
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewer_role: str = Field(min_length=1, max_length=300)
    # The reviewer's legal qualification for the jurisdiction (EDT-01): a treatment has none without it.
    reviewer_qualification: str = Field(min_length=1, max_length=300)
    reviewed_at: datetime

    @model_validator(mode="after")
    def consistent(self):
        if self.reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at needs a time zone")
        if not self.quote.strip() or not self.reviewer_qualification.strip():
            raise ValueError("a treatment quotes its passage and states the reviewer's qualification")
        return self


class CitationIn(Record):
    """A citation a reviewer confirmed in a judgment's paragraph, or withdrew: a structural review."""

    id: str = Field(pattern=CITATION_ID)
    citing_work: str = Field(min_length=1, max_length=160)
    citing_locator: str = Field(min_length=1, max_length=300)
    cited_work: str = Field(min_length=1, max_length=160)
    cited_locator: str | None = Field(None, min_length=1, max_length=300)  # None: the work as a whole
    printed: str = Field(min_length=1, max_length=500)  # the citation as the paragraph writes it
    quote: str = Field(min_length=1, max_length=2000)  # the paragraph's words it is in
    evidence_url: HttpUrl
    extraction: Literal["citations-1", "manual"] = RULES
    review_level: Literal["structural"] = "structural"
    review_state: Literal["reviewed", "withdrawn"] = "reviewed"
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewer_role: str = Field(min_length=1, max_length=300)
    reviewed_at: datetime
    treatments: tuple[TreatmentIn, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at needs a time zone")
        if self.citing_work == self.cited_work:
            raise ValueError("a judgment's citation names another work")
        if not str(self.evidence_url).startswith("https://"):
            raise ValueError("evidence_url is an https address")
        live = [t for t in self.treatments if t.review_state == "reviewed"]
        if len(live) > 1:
            raise ValueError("a citation has one reviewed treatment at most: withdraw the other")
        if live and self.review_state == "withdrawn":
            raise ValueError("a withdrawn citation's treatment is withdrawn too")
        return self


class CitationFile(Record):
    citations: tuple[CitationIn, ...] = ()


class Refused(ValueError):
    pass


def words_of(value: str) -> str:
    """Words as a quote compares them: whitespace collapsed, nothing else changed."""
    return " ".join(value.split())


# --- proposals from published paragraphs ------------------------------------------------------------


def _passage(words: str, start: int, end: int) -> str:
    """The paragraph's words when they are short, else the whole sentences around [start, end) that fit
    MAX_QUOTE (or, in one longer sentence, the whole words around it): always the paragraph's own words."""
    if len(words) <= MAX_QUOTE:
        return words
    cuts = [0, *(m.end() for m in _SENTENCE.finditer(words)), len(words)]
    sentences = list(zip(cuts, cuts[1:], strict=False))  # each [a, b), with the space after it
    first = max(i for i, (a, _) in enumerate(sentences) if a <= start)
    last = max(first, max(i for i, (a, _) in enumerate(sentences) if a < end))
    lo, hi = sentences[first][0], sentences[last][1]
    if hi - lo > MAX_QUOTE:  # one long sentence: the whole words around the citation
        lo = max(lo, start - (MAX_QUOTE - (end - start)) // 2)
        hi = min(hi, lo + MAX_QUOTE)
        lo = max(sentences[first][0], hi - MAX_QUOTE)
        if lo > 0 and words[lo - 1] != " " and words.find(" ", lo, hi) != -1:
            lo = words.find(" ", lo, hi) + 1
        if hi < len(words) and words[hi] != " " and words.rfind(" ", lo, hi) != -1:
            hi = words.rfind(" ", lo, hi)
        return words[lo:hi].strip()
    while True:
        grown = False
        if first > 0 and hi - sentences[first - 1][0] <= MAX_QUOTE:
            first, lo, grown = first - 1, sentences[first - 1][0], True
        if last + 1 < len(sentences) and sentences[last + 1][1] - lo <= MAX_QUOTE:
            last, hi, grown = last + 1, sentences[last + 1][1], True
        if not grown:
            return words[lo:hi].strip()


def _trimmed(value: str) -> str:
    """A reading without the punctuation that ends its clause; a bracket only when nothing opened it."""
    value = value.strip().rstrip(" ,;:.")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1].rstrip(" ,;:.")
    return value


def _candidates(words: str) -> list[tuple[int, int, list[str]]]:
    """Each place a paragraph's words may cite a work, as (start, end, readings): the readings, longest
    first, to resolve until one names exactly one public work."""
    found = []
    for neutral in pinpoint.NEUTRAL.finditer(words):
        after = pinpoint.AFTER_NEUTRAL.match(words, neutral.end())
        end = after.end() if after else neutral.end()
        found.append(
            (neutral.start(), end, list(dict.fromkeys([words[neutral.start() : end], neutral.group(0)])))
        )
    neutral = [(a, b) for a, b, _ in found]
    for cited in pinpoint.IN_TEXT.finditer(words):
        pin = words[cited.start() : cited.end()].strip()
        if any(a <= cited.start() < b for a, b in neutral):
            continue  # "[2019] NGSC 4, para. 12" is a judgment's paragraph, read above
        # Clause boundaries: the work is read up to the end of the sentence or clause it is in.
        before = re.split(r"[;:.!?]\s", words[: cited.start()])[-1].split()[-MAX_WORDS:]
        after = re.split(r"[;:!?]\s|\.\s(?=[A-Z])", words[cited.end() :])[0].split()[:MAX_WORDS]
        readings = [_trimmed(f"{pin} {' '.join(after[:n])}") for n in range(len(after), 0, -1)]
        readings += [_trimmed(f"{' '.join(before[n:])} {pin}") for n in range(len(before))]
        end = cited.end() + len(" ".join(after)) + 1 if after else cited.end()
        found.append((cited.start(), min(end, len(words)), [r for r in readings if r != pin]))
    return found


def propose_from(text: bytes, nodes, work_id: str, url: str, resolve) -> list[dict]:
    """Citations a judgment's paragraphs write, for a reviewer to check. ``text`` is the canonical text's
    UTF-8 bytes and ``nodes`` its paragraphs and their opinions, each with ``kind``, ``locator``,
    ``start_byte``, ``end_byte`` and its printed ``pinpoint`` ("[12]", "op. 2 [4]"); ``url`` is the
    official text, the evidence; ``resolve`` reads a citation as the catalogue does (a CitationView). A
    reading is proposed only when it names exactly one public work other than this one, with the part its
    pinpoint names when that resolves to one; each (paragraph, work, part) once. Never counted until
    reviewed and published."""
    proposals, seen = [], set()
    for node in nodes:
        if node["kind"] != "paragraph":
            continue
        words = words_of(text[node["start_byte"] : node["end_byte"]].decode())
        for start, end, readings in _candidates(words):
            for reading in readings:
                view = resolve(reading)
                if view.status != "resolved" or view.matches[0].id == work_id:
                    continue
                work = view.matches[0]
                part = _part(view.provision)
                key = f"{node['locator']}|{work.id}|{part.locator if part else ''}"
                if key not in seen:
                    seen.add(key)
                    proposals.append(
                        {
                            "key": key,
                            "citing_locator": node["locator"],
                            "citing_pinpoint": node.get("pinpoint"),
                            "cited_work": work.id,
                            "cited_title": work.title,
                            "cited_citation": work.citation,
                            "cited_document_type": work.document_type,
                            "cited_locator": part.locator if part else None,
                            "cited_pinpoint": part.pinpoint if part else None,
                            # Why a pinpoint names no part: no public text, not found, or several.
                            "unresolved_pinpoint": None
                            if part or view.provision is None
                            else view.provision.reason or view.provision.status,
                            "printed": reading[:500],
                            "quote": _passage(words, start, end),
                            "evidence_url": url,
                            "extraction": RULES,
                        }
                    )
                break
    return proposals


def _part(provision):
    """The one part a resolved pinpoint names: one candidate, or the same part of the same work in each of
    its texts (language versions); else None."""
    if provision is None or not provision.candidates:
        return None
    if provision.status == "resolved":
        return provision.candidates[0]
    parts = {(c.source_id, c.locator) for c in provision.candidates}
    return provision.candidates[0] if provision.reason == "several_texts" and len(parts) == 1 else None


def _service(conn):
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository

    return LegacyCatalogue(LegacyRepository(conn))


def propose(conn, work_id: str) -> dict:
    """The citations a judgment's first public text writes (propose_from), as a proposals file for its
    review in the workbench."""
    service = _service(conn)
    source, rights = service._require(work_id)
    if source.document_type != "judgment":
        raise Refused(f"{work_id} is not a judgment")
    for version in service._approved(source, rights):
        ordered = service._ordered_nodes(version)
        by_id = {n.id: n for n in ordered}
        nodes = [
            {
                "kind": n.kind,
                "locator": n.locator,
                "start_byte": n.start_byte,
                "end_byte": n.end_byte,
                "pinpoint": service._pinpoint(n, source.document_type, by_id),
            }
            for n in ordered
        ]
        found = propose_from(
            version.canonical_text.encode(), nodes, work_id, str(version.canonical_url), service.citations
        )
        return {"work": work_id, "version_id": version.id, "rules": RULES, "proposals": found}
    raise Refused(f"{work_id} has no public text to read citations from")


# --- publishing ----------------------------------------------------------------------------------------


def _tables(conn) -> dict[str, str]:
    from changes.record import namespace

    ns = namespace(conn)
    return {name: ns.qualified(conn, "corpus", name) for name in ("citation", "judicial_treatment")}


def _parts(service, work_id: str) -> dict[str, dict]:
    """The parts of a work's public texts by locator: kind, words, printed pinpoint and document order
    (the first text that has the locator)."""
    source, rights = service._require(work_id)
    found: dict[str, dict] = {}
    for version in service._approved(source, rights):
        raw = version.canonical_text.encode()
        ordered = service._ordered_nodes(version)
        by_id = {n.id: n for n in ordered}
        for order, node in enumerate(ordered):
            found.setdefault(
                node.locator,
                {
                    "kind": node.kind,
                    "words": words_of(raw[node.start_byte : node.end_byte].decode()),
                    "pinpoint": service._pinpoint(node, source.document_type, by_id) or node.locator,
                    "order": order,
                },
            )
    return found


IMMUTABLE = ("citing_work", "citing_locator", "cited_work", "cited_locator", "printed", "quote")
TREATMENT_IMMUTABLE = ("citation_id", "treatment", "opinion", "quote", "reviewer_qualification")


def publish(engine, citations: CitationFile, *, dry_run: bool = False) -> dict:
    """Write the file's citations and treatments and announce the treatments, in one transaction; refuse
    the whole file on any problem. Returns what was (or, in a dry run, would be) written."""
    from api.store import check_legacy_authority
    from changes.record import current
    from changes.record import publish as record

    report = {
        "citations_added": 0, "citations_withdrawn": 0, "treatments_added": 0, "treatments_withdrawn": 0,
        "unchanged": 0, "not_published": 0, "announced": 0,
    }  # fmt: skip
    with engine.connect() as conn, conn.begin() as transaction:
        if conn.dialect.name != "postgresql":
            raise Refused("Citations are published to the PostgreSQL catalogue.")
        check_legacy_authority(conn, write=True)
        t = _tables(conn)
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": t["judicial_treatment"]}) is None:
            raise Refused("The catalogue has no citation tables yet: migrate it first.")
        held = {
            row.citation_id: row
            for row in conn.execute(
                text(f"SELECT * FROM {t['citation']} WHERE citation_id = ANY(:ids)"),
                {"ids": [c.id for c in citations.citations]},
            )
        }
        treatments = [(c, x) for c in citations.citations for x in c.treatments]
        held_treatments = {
            row.treatment_id: row
            for row in conn.execute(
                text(f"SELECT * FROM {t['judicial_treatment']} WHERE treatment_id = ANY(:ids)"),
                {"ids": [x.id for _, x in treatments]},
            )
        }
        new = [c for c in citations.citations if c.id not in held and c.review_state == "reviewed"]
        works = {w for c in new for w in (c.citing_work, c.cited_work)}
        works |= {c.citing_work for c, x in treatments if x.id not in held_treatments}
        works |= {c.cited_work for c, x in treatments if x.id not in held_treatments}
        public = current(conn, works) if works else {}
        service, parts, problems = _service(conn), {}, []

        def part_of(work: str, locator: str) -> dict | None:
            if work not in parts:
                parts[work] = _parts(service, work)
            return parts[work].get(locator)

        def kind_of(work: str) -> str | None:
            return (public.get(work) or {}).get("projection", {}).get("document_type")

        for citation in new:
            missing = [w for w in (citation.citing_work, citation.cited_work) if w not in public]
            if missing:
                problems.append(f"{citation.id}: {', '.join(missing)} is not a public record")
                continue
            if kind_of(citation.citing_work) != "judgment":
                problems.append(f"{citation.id}: {citation.citing_work} is not a judgment")
                continue
            paragraph = part_of(citation.citing_work, citation.citing_locator)
            if paragraph is None or paragraph["kind"] != "paragraph":
                problems.append(
                    f"{citation.id}: no public text of {citation.citing_work} has a paragraph "
                    f"{citation.citing_locator}"
                )
            elif words_of(citation.quote) not in paragraph["words"]:
                problems.append(f"{citation.id}: its quote is not the words of {citation.citing_locator}")
            if citation.cited_locator and part_of(citation.cited_work, citation.cited_locator) is None:
                problems.append(
                    f"{citation.id}: no public text of {citation.cited_work} has {citation.cited_locator}"
                )
        for citation, treatment in treatments:
            if treatment.id in held_treatments or treatment.review_state != "reviewed":
                continue
            missing = [w for w in (citation.citing_work, citation.cited_work) if w not in public]
            if citation.review_state != "reviewed":
                problems.append(f"{treatment.id}: its citation {citation.id} is withdrawn")
            elif missing:
                problems.append(f"{treatment.id}: {', '.join(missing)} is not a public record")
            elif kind_of(citation.cited_work) != "judgment":
                problems.append(
                    f"{treatment.id}: judicial treatment is of a judgment; {citation.cited_work} is not"
                )
            else:
                paragraph = part_of(citation.citing_work, citation.citing_locator) or {"words": ""}
                if words_of(treatment.quote) not in paragraph["words"]:
                    problems.append(
                        f"{treatment.id}: its passage is not the words of {citation.citing_locator}"
                    )
        for citation in citations.citations:
            was = held.get(citation.id)
            if was is not None and any(getattr(was, k) != getattr(citation, k) for k in IMMUTABLE):
                problems.append(
                    f"{citation.id}: published citations are not edited; withdraw it and confirm a new one"
                )
            elif was is not None and was.review_state == "withdrawn" and citation.review_state == "reviewed":
                problems.append(f"{citation.id}: a withdrawn citation is not reinstated; confirm a new one")
        for citation, treatment in treatments:
            was = held_treatments.get(treatment.id)
            values = {"citation_id": citation.id, "treatment": treatment.kind, "opinion": treatment.opinion,
                      "quote": treatment.quote, "reviewer_qualification": treatment.reviewer_qualification}  # fmt: skip
            if was is not None and any(getattr(was, k) != values[k] for k in TREATMENT_IMMUTABLE):
                problems.append(
                    f"{treatment.id}: published treatments are not edited; withdraw it and record a new one"
                )
            elif was is not None and was.review_state == "withdrawn" and treatment.review_state == "reviewed":
                problems.append(f"{treatment.id}: a withdrawn treatment is not reinstated; record a new one")
        if problems:
            raise Refused("; ".join(problems))
        for citation in citations.citations:
            was = held.get(citation.id)
            if was is None and citation.review_state != "reviewed":
                report["not_published"] += 1  # withdrawn before it was ever public
                continue
            if was is None:
                paragraph = part_of(citation.citing_work, citation.citing_locator)
                cited = (
                    part_of(citation.cited_work, citation.cited_locator) if citation.cited_locator else None
                )
                conn.execute(
                    text(
                        f"INSERT INTO {t['citation']} (citation_id, citing_work, citing_locator, citing_pinpoint,"
                        " citing_order, cited_work, cited_locator, cited_pinpoint, cited_order, printed, quote,"
                        " evidence_url, extraction, review_state, reviewed_by, reviewer_role, reviewed_at)"
                        " VALUES (:id, :citing_work, :citing_locator, :citing_pinpoint, :citing_order, :cited_work,"
                        " :cited_locator, :cited_pinpoint, :cited_order, :printed, :quote, :url, :extraction,"
                        " 'reviewed', :by, :role, :at)"
                    ),
                    {
                        "id": citation.id, "citing_work": citation.citing_work,
                        "citing_locator": citation.citing_locator, "citing_pinpoint": paragraph["pinpoint"][:100],
                        "citing_order": paragraph["order"], "cited_work": citation.cited_work,
                        "cited_locator": citation.cited_locator,
                        "cited_pinpoint": cited["pinpoint"][:300] if cited else None,
                        "cited_order": cited["order"] if cited else None, "printed": citation.printed,
                        "quote": citation.quote, "url": str(citation.evidence_url),
                        "extraction": citation.extraction, "by": citation.reviewed_by,
                        "role": citation.reviewer_role, "at": citation.reviewed_at,
                    },
                )  # fmt: skip
                report["citations_added"] += 1
            elif was.review_state != citation.review_state:  # reviewed → withdrawn
                conn.execute(
                    text(f"UPDATE {t['citation']} SET review_state = 'withdrawn' WHERE citation_id = :id"),
                    {"id": citation.id},
                )
                report["citations_withdrawn"] += 1
            else:
                report["unchanged"] += 1
        # A treatment withdrawn goes first, so the one recorded in its place is the citation's only one.
        for citation, treatment in sorted(treatments, key=lambda pair: pair[1].review_state != "withdrawn"):
            was = held_treatments.get(treatment.id)
            if was is None and (treatment.review_state != "reviewed" or citation.review_state != "reviewed"):
                report["not_published"] += 1
                continue
            if was is None:
                conn.execute(
                    text(
                        f"INSERT INTO {t['judicial_treatment']} (treatment_id, citation_id, treatment, opinion,"
                        " quote, review_state, reviewed_by, reviewer_role, reviewer_qualification, reviewed_at)"
                        " VALUES (:id, :citation, :kind, :opinion, :quote, 'reviewed', :by, :role, :qualification,"
                        " :at)"
                    ),
                    {
                        "id": treatment.id, "citation": citation.id, "kind": treatment.kind,
                        "opinion": treatment.opinion, "quote": treatment.quote, "by": treatment.reviewed_by,
                        "role": treatment.reviewer_role, "qualification": treatment.reviewer_qualification,
                        "at": treatment.reviewed_at,
                    },
                )  # fmt: skip
                report["treatments_added"] += 1
            elif was.review_state == "reviewed" and (
                treatment.review_state == "withdrawn" or citation.review_state == "withdrawn"
            ):
                conn.execute(
                    text(
                        f"UPDATE {t['judicial_treatment']} SET review_state = 'withdrawn' WHERE treatment_id = :id"
                    ),
                    {"id": treatment.id},
                )
                report["treatments_withdrawn"] += 1
            else:
                report["unchanged"] += 1
        # A withdrawn citation takes any published treatment of it out of public view too.
        conn.execute(
            text(
                f"UPDATE {t['judicial_treatment']} SET review_state = 'withdrawn' WHERE review_state = 'reviewed'"
                f" AND citation_id IN (SELECT citation_id FROM {t['citation']} WHERE review_state = 'withdrawn'"
                " AND citation_id = ANY(:ids))"
            ),
            {"ids": [c.id for c in citations.citations]},
        )
        touched = {w for c in citations.citations for w in (c.citing_work, c.cited_work)}
        report["announced"] = record(conn, touched) if touched else 0
        if dry_run:
            transaction.rollback()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    proposing = commands.add_parser("propose")
    proposing.add_argument("--work", required=True)
    publishing = commands.add_parser("publish")
    publishing.add_argument("file")
    publishing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from api.store import engine_for

    engine = engine_for()
    try:
        if args.command == "propose":
            with engine.begin() as conn:
                found = propose(conn, args.work)
            print(json.dumps(found, indent=2, ensure_ascii=False))
            print(
                f"{len(found['proposals'])} proposals only: import them into the judgment's review in the "
                "workbench, where each is confirmed or rejected.",
                file=sys.stderr,
            )
            return 0
        with open(args.file, encoding="utf-8") as handle:
            report = publish(engine, CitationFile.model_validate_json(handle.read()), dry_run=args.dry_run)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report | {"dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
