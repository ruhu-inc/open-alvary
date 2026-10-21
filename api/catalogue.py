"""Catalogue domain services; injected repositories own no commits or HTTP behaviour."""

import base64
import hashlib
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import cache
from typing import Protocol

from sqlalchemy import text

from api import applicable, pinpoint, text_index
from api.reach import country
from api.repositories import (
    EDGE_ORDER,
    MAX_BUFFER_BYTES,
    MAX_STRUCTURE_ROWS,
    MAX_UNPAGED_ROWS,
    LegacyRepository,
    PublicUnavailable,
    ResultTooLarge,
)
from api.search import normalize_citation as normalize_citation
from api.search import words
from changes.record import namespace
from rights.policy import decide
from schema.dates import within
from schema.jurisdictions import catalogue_entry, registry
from schema.models import Citation, Corpus
from schema.policy import MetadataPage, PublicMetadata
from schema.public import (
    AnchorView,
    AuthorityEvidence,
    CitationView,
    CoverageView,
    CurrentSource,
    DecisionView,
    ExpressionPage,
    ExpressionView,
    JurisdictionDetail,
    JurisdictionView,
    LanguageRuleView,
    LinkedAct,
    MembershipBasis,
    MembershipList,
    MembershipView,
    NodeViewRecord,
    ProvisionLookup,
    ProvisionMatch,
    RelatedSource,
    RightsHistory,
    RightsRevision,
    RightsView,
    RightsViewRecord,
    SearchCoverage,
    SearchPage,
    SourcePage,
    SourceRelationships,
    SourceView,
    StructurePage,
    SuspensionView,
    TextHit,
    TextSearchPage,
    VersionPage,
    VersionSelection,
    VersionViewRecord,
)


class SourceUnavailable(LookupError):
    pass


_profiles = cache(registry)
MEMBERSHIP_NOTICE = {
    "reviewed": "Reviewed memberships only. A membership not yet reviewed is not listed, so a body or member "
    "missing here is not proof of non-membership.",
    "unknown": "No membership here has been reviewed yet: unknown, not none.",
}


def _on(membership: MembershipView, as_of: date) -> MembershipView:
    """A membership with each suspension's ``in_effect`` on the date: context, never whether it held."""
    if not membership.suspensions:
        return membership
    return membership.model_copy(
        update={
            "suspensions": tuple(
                s.model_copy(update={"in_effect": within(as_of, s.valid_from, s.valid_to)})
                for s in membership.suspensions
            )
        }
    )


def membership_list(
    held: list[MembershipView], jurisdiction_id: str, side: str, as_of: date
) -> MembershipList:
    """The reviewed memberships on one side of a jurisdiction on a date: as ``member`` (the bodies it
    belongs to) or as ``body`` (its members). Dates are inclusive: held (``items``), ``uncertain`` when the
    date falls inside an uncertain start or end, or ``ended`` before it; none reviewed is ``unknown``.
    Suspensions are context and never move a membership between them."""
    mine = [_on(m, as_of) for m in held if getattr(m, f"{side}_id") == jurisdiction_id]
    if not mine:
        return MembershipList(status="unknown", notice=MEMBERSHIP_NOTICE["unknown"])
    answers = [(m, within(as_of, m.valid_from, m.valid_to)) for m in mine]
    return MembershipList(
        status="reviewed",
        items=tuple(m for m, held_then in answers if held_then is True),
        uncertain=tuple(m for m, held_then in answers if held_then is None),
        ended=tuple(m for m, _ in answers if m.valid_to is not None and m.valid_to.latest < as_of),
        notice=MEMBERSHIP_NOTICE["reviewed"],
    )


def _held_on(membership: MembershipView, day: date) -> bool | None:
    """Whether a reviewed membership held on a day: True, False, or None when the day falls inside an
    uncertain window of its dates (schema.dates; inclusive)."""
    return within(day, membership.valid_from, membership.valid_to)


def bodies_on(
    held: list[MembershipView], member_id: str, day: date
) -> dict[str, tuple[MembershipBasis, ...]]:
    """The bodies a jurisdiction has a reviewed membership of, each with whether it was a member on ``day``
    and the memberships that rests on: ``held`` (the membership that held), ``unknown`` (the day falls in an
    uncertain window of one) or ``not_held`` (none held: every one of them). A body with no reviewed
    membership is absent, which is unknown, never "not a member". Applicable law reads membership dates
    here only (api.applicable). A suspension from participation never ends membership: only membership
    rows count."""
    rows: dict[str, list[tuple[MembershipView, bool | None]]] = {}
    for m in held:
        if m.member_id == member_id and getattr(m, "kind", "member_of") == "member_of":
            rows.setdefault(m.body_id, []).append((m, _held_on(m, day)))
    found = {}
    for body, mine in sorted(rows.items()):
        state, relied = "not_held", [m for m, _ in mine]
        if any(on is True for _, on in mine):
            state, relied = "held", [m for m, on in mine if on is True]
        elif any(on is None for _, on in mine):
            state, relied = "unknown", [m for m, on in mine if on is None]
        found[body] = tuple(MembershipBasis(id=m.id, body_id=body, held=state) for m in relied)
    return found


def _cursor(offset: int, kind: str = "edges") -> str:
    return base64.urlsafe_b64encode(f"{kind}:{offset}".encode()).decode().rstrip("=")


def _offset(cursor: str | None, expected: str = "edges") -> int:
    """Where a page starts: an opaque cursor from the page before (links or nodes)."""
    if cursor is None:
        return 0
    try:
        kind, _, offset = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().partition(":")
        if kind == expected and offset.isdigit() and int(offset) <= 100_000:
            return int(offset)
    except (ValueError, UnicodeDecodeError):
        pass
    raise InvalidSearch("Invalid cursor")


# Graph bounds (API-04, BUD-01): links one step away, a page of 25 by default and 100 at most, and
# at most 300 links examined in a request (the walk to the source in force now, three steps at most).
EDGES_PER_PAGE, MAX_EDGES_PER_PAGE, EDGE_BUDGET, MAX_SUCCESSION = 25, 100, 300, 3
# An anchor answers with its whole span, never silently shortened (BUD-01), inside the 1 MiB JSON cap.
MAX_ANCHOR_BYTES = 900 * 1024
# A text search serves its first 1,000 hits, 25 a page by default and 100 at most (API-03).
MAX_HIT_OFFSET = 1000
# A catalogue search finds at most this many records by their published text (else 413, as before).
MAX_TEXT_WORKS = 10_000
NO_WORDS = "The search has no letters or digits to match. Search for words, such as a title or a citation."
# Why a catalogue search found records by their details only (API-04: told apart from no result).
TEXT_NOT_SEARCHED = {
    "unavailable": "Published texts were not searched: this deployment has no section index. Records were "
    "matched by their title, citation and summary only.",
    "building": "Published texts were not searched: the section index is being built. Records were matched "
    "by their title, citation and summary only; try again in a few minutes.",
    "timeout": "Published texts were not searched: that took longer than ten seconds. Records were matched "
    "by their title, citation and summary only.",
}


def expression_id(version) -> str:
    """A text's language edition (API-02): its record and language, `{source_id}:{language}`."""
    return f"{version.source_id}:{version.language}"


def edition_authority(source, language: str) -> tuple[str, AuthorityEvidence | None]:
    """What a language edition is worth as law, as the record states it, and where (API-02, DAT-07):
    ``unknown`` when it states nothing. Never inferred from a preferred or selected text, the record's
    own language or another edition; the language matches exactly (BCP 47 ignores case)."""
    for edition in source.editions:
        if edition.language.casefold() == language.casefold():
            evidence = edition.evidence
            return edition.authority, (
                AuthorityEvidence.model_validate(evidence.model_dump(mode="json", exclude_none=True))
                if evidence
                else None
            )
    return "unknown", None


def language_rule(source) -> LanguageRuleView:
    """The record's own rule for divergence between its language versions (LNG-03), or ``unknown``."""
    if source.language_rule is None:
        return LanguageRuleView(status="unknown")
    return LanguageRuleView.model_validate(source.language_rule.model_dump(mode="json", exclude_none=True))


def _speaks(version, language: str | None) -> bool:
    """RFC 4647 basic filtering: "en" matches a text in "en" or "en-NG"."""
    if language is None:
        return True
    tag, wanted = version.language.casefold(), language.casefold()
    return tag == wanted or tag.startswith(wanted + "-")


def _in_force(version, as_of) -> bool:
    return bool(
        version.effective_from
        and version.effective_from <= as_of
        and (version.effective_until is None or as_of < version.effective_until)
    )


def _latest_readings(versions: list) -> list:
    """Without the texts a later reading of the same official file corrects (TXP-04): they are the
    same text read again, not alternatives."""
    corrected = {
        v.prior_version_id
        for v in versions
        if v.prior_version_id
        and any(
            w.id == v.prior_version_id and w.raw_hash == v.raw_hash and w.language == v.language
            for w in versions
        )
    }
    return [v for v in versions if v.id not in corrected]


def select_versions(versions: list, as_of=None, language=None) -> tuple[VersionSelection, list]:
    """The text a date and a language select among public ones (API-02, OA-T3), or the alternatives and
    why. A text with no effective date is never taken to be in force on a date: it stays an alternative."""
    asked = {"as_of": as_of, "language": language}
    candidates = _latest_readings([v for v in versions if _speaks(v, language)])
    if not candidates:
        return VersionSelection(status="none", reason="none_in_language", **asked), []
    if as_of is None:
        chosen, unknown = candidates, []
    else:
        chosen = [v for v in candidates if _in_force(v, as_of)]
        unknown = [
            v
            for v in candidates
            if v.effective_from is None and not (v.effective_until and v.effective_until <= as_of)
        ]
    if len(chosen) == 1 and not unknown:
        return VersionSelection(status="selected", version_id=chosen[0].id, **asked), chosen
    if not chosen and not unknown:
        return VersionSelection(status="none", reason="none_in_force", **asked), []
    alternatives = chosen + unknown
    reason = (
        "effective_dates_unknown"
        if unknown
        else "several_languages"
        if len({v.language for v in chosen}) > 1
        else "several_texts"
    )
    selection = VersionSelection(
        status="ambiguous", alternatives=tuple(v.id for v in alternatives), reason=reason, **asked
    )
    return selection, alternatives


# The website's filters (MAC-01): each is a source field, except access, which follows the rights
# decision. Values are counted over at most the pilot's bound of examined sources.
FACETS = ("jurisdiction", "type", "language", "status", "year", "access")
SORTS = ("relevance", "newest", "oldest", "title")


class InvalidSearch(ValueError):
    pass


class MetadataReadPort(Protocol):
    def metadata(self, work_id: str) -> PublicMetadata | None: ...
    def metadata_page(self, *, limit: int = 50, after: str | None = None) -> MetadataPage: ...


class TargetCatalogue:
    """Target service kept separate from pilot HTTP and authority cutover."""

    def __init__(self, repository: MetadataReadPort):
        self.repository = repository

    def metadata(self, work_id: str) -> PublicMetadata | None:
        return self.repository.metadata(work_id)

    def page(self, *, limit: int = 50, after: str | None = None) -> MetadataPage:
        return self.repository.metadata_page(limit=limit, after=after)


class LegacyCatalogue:
    """Compatibility domain adapter over legacy authority, never over policy shadows.

    These pilot routes keep their wire fields. Unpaged results have explicit development
    bounds until versioned pagination/delivery is accepted; no partial text is returned.
    """

    def __init__(self, repository: LegacyRepository):
        self.repository = repository
        self.now = datetime.now(UTC)
        self._linked: dict[tuple[str, str], tuple[LinkedAct, ...]] = {}

    def _require(self, source_id):
        pair = self.repository.source(source_id)
        if pair is None or not decide(pair[1], now=self.now).metadata:
            raise SourceUnavailable("Source not available")
        return pair

    def _approved(self, source, rights):
        for gate in self.repository.version_gates(source.id, rights.approved_content_hashes):
            if decide(rights, gate, now=self.now).full_text:
                version = self.repository.version(gate.id, source.id)
                if not decide(rights, version, now=self.now).full_text:
                    raise PublicUnavailable("Version policy changed during lookup")
                yield version

    def _view(self, pair):
        source, rights = pair
        has_text = next(self._approved(source, rights), None) is not None
        return SourceView(
            **source.model_dump(mode="json", exclude={"treaty"}),
            treaty=applicable.treaty_view(source, self._acts),
            has_full_text=has_text,
            access_mode="open_full_text" if has_text else "metadata_link_only",
            rights_status=rights.status,
        )

    def source(self, source_id):
        return self._view(self._require(source_id))

    def _access(self, access, jurisdiction):
        if access not in {"all", "open", "metadata"}:
            raise InvalidSearch("Invalid access filter")
        if access == "all":
            return None
        open_ids = []
        for source, rights in self.repository.sources(jurisdiction, where=self.repository.potential_text()):
            if any(
                decide(rights, gate, now=self.now).full_text
                for gate in self.repository.version_gates(source.id, rights.approved_content_hashes)
            ):
                open_ids.append(source.id)
                if len(open_ids) > 10000:
                    raise ResultTooLarge("Pilot access filtering exceeds declared bound")
        return self.repository.access_predicate(open_ids, access)

    def sources(self, *, jurisdiction=None, limit=50, offset=0, access="all"):
        total, pairs = self.repository.source_page(
            limit=limit, offset=offset, jurisdiction=jurisdiction, where=self._access(access, jurisdiction)
        )
        return SourcePage(total=total, items=tuple(self._view(pair) for pair in pairs))

    def _public(self, ids) -> dict:
        return {
            source_id: pair
            for source_id, pair in self.repository.visible(ids).items()
            if decide(pair[1], now=self.now).metadata
        }

    def _current(self, source):
        """The source in force at the end of this one's line of repeals and replacements, if any
        within three steps and the request's budget of examined links."""
        seen, at, found, examined = {source.id}, source.id, None, 0
        for _ in range(MAX_SUCCESSION):
            replacing = self.repository.replaced(at)
            examined += len(replacing)
            later = self._public({a.source_id for a in replacing})
            if not later or examined > EDGE_BUDGET:
                break
            successor = max((pair[0] for pair in later.values()), key=lambda s: (s.year or 0, s.id))
            if successor.id in seen:
                break
            seen.add(successor.id)
            at, found = successor.id, successor
        return (
            CurrentSource(source_id=found.id, title=found.title, citation=found.citation) if found else None
        )

    def _acts(self, source_id: str, kind: str) -> tuple[LinkedAct, ...]:
        """The public records a checked link records as ``kind`` of this one (``domesticated_by``,
        ``transposed_by``): the national acts that domesticate a treaty or transpose a directive, a page
        at most."""
        if (source_id, kind) not in self._linked:
            _, edges = self.repository.edges(source_id, {kind}, limit=MAX_EDGES_PER_PAGE, offset=0)
            public = self._public({other for _, other, _ in edges})
            self._linked[(source_id, kind)] = tuple(
                LinkedAct(
                    source_id=other, jurisdiction_id=public[other][0].jurisdiction_id,
                    title=public[other][0].title, citation=public[other][0].citation,
                    effective_date=link.effective_date,
                )
                for _, other, link in edges
                if other in public
            )  # fmt: skip
        return self._linked[(source_id, kind)]

    def labeller(self, jurisdiction: str, as_of: date) -> applicable.Labeller:
        """Records' relation to a country's law on a date (api.applicable), from its reviewed memberships."""
        if country(jurisdiction) is None:
            raise InvalidSearch(
                f"Applicable law is the law of a country or a place within one; {jurisdiction} is the "
                "continent or a regional body."
            )
        return applicable.Labeller(
            jurisdiction, as_of, bodies_on(self.memberships(jurisdiction), jurisdiction, as_of), self._acts
        )

    def applicable_scope(
        self, jurisdiction: str, as_of: date | None, *, named: str | None = None
    ) -> applicable.Scope:
        """A search's applicable-law scope (scope=applicable): the jurisdiction's own law, and the public
        records of the bodies it belonged to on the date (reviewed memberships only) and the public treaties
        whose party list names it, those that bind there or whose relation is unknown (api.applicable).
        Without a date, today's; ``named`` is the record a request names, answered whatever its relation."""
        labeller = self.labeller(jurisdiction, as_of or self.now.date())
        bodies = [body for body, basis in labeller.bodies.items() if basis[0].held == "held"]
        state, found = country(jurisdiction), {}
        for source, rights in self.repository.sources(bodies) if bodies else ():
            if decide(rights, now=self.now).metadata:
                found[source.id] = source
        for source, rights in self.repository.treaties():
            if any(p.place == state for p in source.treaty.parties) and decide(rights, now=self.now).metadata:
                found.setdefault(source.id, source)
        if len(found) > applicable.MAX_CANDIDATES:
            raise ResultTooLarge("Applicable-law scope exceeds declared bound")
        pair = self._public({named}).get(named) if named else None
        return applicable.scope(
            labeller,
            [found[key] for key in sorted(found)],
            date_basis="as_of" if as_of else "today",
            named=pair[0] if pair else None,
        )

    def relationships(self, source_id, *, kinds=None, limit=EDGES_PER_PAGE, cursor=None, current=True):
        """A page of a source's checked links to other public sources, as seen from its side: what it
        repeals, amends, commences, gives effect to or is made under, what repealed, amended, commenced
        or gave effect to it, what it cites and is cited by, the instruments made under it; and, when it
        has been repealed or replaced, the source in force in its place now."""
        source, _ = self._require(source_id)
        kinds = set(EDGE_ORDER) if kinds is None else set(kinds)
        if not kinds <= set(EDGE_ORDER) or not 1 <= limit <= MAX_EDGES_PER_PAGE:
            raise InvalidSearch("Invalid relationship filter or page size")
        offset = _offset(cursor)
        total, edges = self.repository.edges(source_id, kinds, limit=limit, offset=offset)
        public = self._public({other for _, other, _ in edges})
        items = tuple(
            RelatedSource(
                kind=seen,
                source_id=other,
                title=public[other][0].title,
                citation=public[other][0].citation,
                legal_status=public[other][0].legal_status,
                year=public[other][0].year,
                effective_date=link.effective_date,
                evidence_url=link.evidence_url,
                verified=link.verified,
            )
            for seen, other, link in edges
            if other in public
        )
        return SourceRelationships(
            source_id=source.id,
            legal_status=source.legal_status,
            current=self._current(source) if current else None,
            total=total,
            items=items,
            next_cursor=_cursor(offset + limit) if offset + limit < total else None,
        )

    def versions(self, source_id, *, as_of=None, language=None, expression=None, editions=False):
        """A source's public texts with their structure; given a date, a language or an edition
        (``expression``, `{source_id}:{language}`), only the text they select, or the alternatives and
        why (``selection``). ``editions`` labels each text with its edition's ID and authority."""
        source, rights = self._require(source_id)
        if expression is not None:
            owner, _, edition = expression.rpartition(":")
            if owner != source.id or not edition or (language is not None and language != edition):
                raise InvalidSearch("That expression_id is not an edition of this record")
            language = edition
        versions = []
        size = 0
        for version in self._approved(source, rights):
            size += len(version.canonical_text.encode())
            if len(versions) == MAX_UNPAGED_ROWS or size > MAX_BUFFER_BYTES:
                raise ResultTooLarge("Pilot unpaged text result exceeds bound")
            versions.append(version)
        selection = None
        if as_of is not None or language is not None:
            selection, versions = select_versions(versions, as_of, language)
        nodes = tuple(
            NodeViewRecord.model_validate(row)
            for row in self.repository.rows(
                "structure", version_ids=[v.id for v in versions], bound=MAX_STRUCTURE_ROWS
            )
        )
        # Reuse the canonical multibyte/hash/hierarchy validator on one approved source.
        # Reference context is scoped and bounded; unrelated private corpus is never read.
        if versions:
            jurisdictions = self.repository.rows("jurisdictions", ids=[source.jurisdiction_id])
            visited = {source.jurisdiction_id}
            current = jurisdictions
            while current:
                parents = {j.get("parent_id") for j in current if j.get("parent_id")}
                if parents & visited:
                    raise PublicUnavailable("Invalid jurisdiction hierarchy")
                if len(visited | parents) > MAX_UNPAGED_ROWS:
                    raise ResultTooLarge("Reference context exceeds bound")
                current = self.repository.rows("jurisdictions", ids=parents)
                jurisdictions.extend(current)
                visited.update(parents)
            languages = {
                source.language,
                *(v.language for v in versions),
                *(language for j in jurisdictions for language in j.get("languages", [])),
            }
            Corpus.model_validate(
                dict(
                    sources=[source.model_dump(mode="json")],
                    rights=[rights.model_dump(mode="json")],
                    versions=[v.model_dump(mode="json") for v in versions],
                    structure=[n.model_dump(mode="json") for n in nodes],
                    jurisdictions=jurisdictions,
                    authorities=self.repository.rows("authorities", ids=[source.authority_id]),
                    languages=self.repository.rows("languages", ids=languages),
                )
            )
        notice = None
        if not versions:
            notice = (
                "No version has passed both rights and content review."
                if selection is None
                else "No public text matches that date and language."
            )
        if editions:
            labelled = []
            for v in versions:
                authority, evidence = edition_authority(source, v.language)
                labelled.append(
                    v.model_copy(
                        update={
                            "expression_id": expression_id(v),
                            "expression_authority": authority,
                            "expression_authority_evidence": evidence,
                        }
                    )
                )
            versions = labelled
        return VersionPage(
            items=tuple(versions), rights=rights, structure=nodes, notice=notice, selection=selection
        )

    def expressions(self, source_id) -> ExpressionPage:
        """A source's language editions with public texts (API-02): each edition's texts, the one to read
        when no date is given (when one can be named without guessing) and its authority as the record
        states it; and the record's own rule where its language versions differ."""
        source, rights = self._require(source_id)
        editions: dict[str, list] = {}
        for version in self._approved(source, rights):
            editions.setdefault(version.language, []).append(version)
        today = datetime.now(UTC).date()
        items = []
        for language, versions in sorted(editions.items()):
            readings = _latest_readings(versions)
            in_force = [v for v in readings if _in_force(v, today)]
            if len(readings) == 1:
                preferred, reason = readings[0].id, "only_text"
            elif len(in_force) == 1 and all(v.effective_from for v in readings):
                preferred, reason = in_force[0].id, "in_force_today"
            else:
                undated = any(v.effective_from is None for v in readings)
                preferred, reason = None, "dates_unknown" if undated else "several_texts"
            authority, evidence = edition_authority(source, language)
            items.append(
                ExpressionView(
                    expression_id=f"{source.id}:{language}",
                    source_id=source.id,
                    language=language,
                    authority=authority,
                    authority_evidence=evidence,
                    version_ids=tuple(v.id for v in versions),
                    preferred_version_id=preferred,
                    preferred_reason=reason,
                )
            )
        return ExpressionPage(source_id=source.id, language_rule=language_rule(source), items=tuple(items))

    # --- one version, its structure and its anchors (API-02), each rechecked for eligibility ---------

    def version(self, version_id: str) -> tuple[VersionViewRecord, object, object]:
        """A version that is public now, with its source and rights record. A hidden version and one
        that does not exist are the same answer: unavailable."""
        source_id = self.repository.version_owner(version_id)
        if source_id is None:
            raise SourceUnavailable("Version not available")
        source, rights = self._require(source_id)
        for version in self._approved(source, rights):
            if version.id == version_id:
                return version, source, rights
        raise SourceUnavailable("Version not available")

    def _ordered_nodes(self, version: VersionViewRecord) -> list[NodeViewRecord]:
        """A version's nodes in document order: by where they start, the enclosing node first."""
        nodes = [
            NodeViewRecord.model_validate(row)
            for row in self.repository.rows("structure", version_ids=[version.id], bound=MAX_STRUCTURE_ROWS)
        ]
        parents = {n.id: n.parent_id for n in nodes}

        def depth(node):
            steps, at = 0, node.parent_id
            while at is not None and steps < 64:
                steps, at = steps + 1, parents.get(at)
            return steps

        return sorted(nodes, key=lambda n: (n.start_byte, -n.end_byte, depth(n), n.order))

    def structure(self, version_id: str, *, limit: int = 100, cursor: str | None = None) -> StructurePage:
        version, _, _ = self.version(version_id)
        nodes = self._ordered_nodes(version)
        offset = _offset(cursor, "nodes")
        return StructurePage(
            version_id=version.id,
            content_hash=version.content_hash,
            total=len(nodes),
            items=tuple(nodes[offset : offset + limit]),
            next_cursor=_cursor(offset + limit, "nodes") if offset + limit < len(nodes) else None,
        )

    def _chain(self, node: NodeViewRecord, by_id: dict | None = None) -> list[NodeViewRecord]:
        """A part and the parts enclosing it, outermost first: from ``by_id`` when the version's parts
        are already read, else one lookup each."""
        chain, at = [node], node
        while at.parent_id and len(chain) < 64:
            if by_id is not None:
                parent = by_id.get(at.parent_id)
            else:
                row = self.repository.node(at.parent_id)
                parent = NodeViewRecord.model_validate(row) if row is not None else None
            if parent is None:
                break
            chain.append(parent)
            at = parent
        return chain[::-1]

    def _pinpoint(
        self, node: NodeViewRecord, document_type: str | None = None, by_id: dict | None = None
    ) -> str:
        """How a part is cited within its text, from its printed numbers (api.pinpoint): "s. 23(1)(a)",
        "art. 12", "First Schedule, para. 3", a definition with its section; else its heading or label."""
        cited = pinpoint.cite(self._chain(node, by_id), document_type)
        if cited is not None:
            return cited[1]
        if node.kind == "document":
            return ""
        return node.heading or node.label or node.locator

    def _span(self, node: NodeViewRecord, version: VersionViewRecord) -> tuple[bytes, str]:
        """A part's exact bytes and their hash, checked against the hash it was published with."""
        if node.source_id != version.source_id or node.source_hash != version.content_hash:
            raise PublicUnavailable("Invalid anchor provenance")
        span = version.canonical_text.encode()[node.start_byte : node.end_byte]
        span_hash = hashlib.sha256(span).hexdigest()
        if node.span_hash and node.span_hash != span_hash:
            raise PublicUnavailable("Anchor span hash mismatch")
        return span, span_hash

    def anchor(self, anchor_id: str) -> AnchorView:
        """One part of a public text exactly. Each node of a published version is an anchor (DAT-06)."""
        row = self.repository.node(anchor_id)
        if row is None:
            raise SourceUnavailable("Anchor not available")
        node = NodeViewRecord.model_validate(row)
        version, source, rights = self.version(node.version_id)
        span, span_hash = self._span(node, version)
        if len(span) > MAX_ANCHOR_BYTES:
            raise ResultTooLarge("This part is larger than one response may be; read it from the text")
        cited = self._pinpoint(node, source.document_type)
        return AnchorView(
            anchor_id=node.id,
            source_id=node.source_id,
            version_id=node.version_id,
            kind=node.kind,
            locator=node.locator,
            label=node.label,
            heading=node.heading,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            span_hash=span_hash,
            content_hash=version.content_hash,
            serialization_profile=version.serialization_profile,
            page=node.page,
            page_end=node.page_end,
            citation=f"{source.citation}, {cited}" if cited else source.citation,
            official_url=str(version.canonical_url),
            licence=rights.licence,
            attribution_required=rights.attribution_required,
            attribution_text=rights.attribution_text,
            regional=applicable.regional_view(source),
            treaty=applicable.treaty_view(source, self._acts),
            text=span.decode("utf-8"),
        )

    def rights(self, source_id):
        source, rights = self._require(source_id)
        version = next(self._approved(source, rights), None)
        decision = decide(rights, version, now=self.now)
        latest = self._rights_revisions(source_id, limit=1)
        return RightsView(
            record=rights,
            decision=DecisionView.model_validate(decision.model_dump(mode="json")),
            revision=latest[0]["revision"] if latest else None,
        )

    def rights_history(self, source_id) -> RightsHistory:
        """Every revision of a public record's rights record, newest first (OA-R1). A hidden record
        and one that does not exist are the same answer: unavailable."""
        self._require(source_id)
        rows = self._rights_revisions(source_id, limit=MAX_UNPAGED_ROWS)
        return RightsHistory(
            source_id=source_id,
            revisions=tuple(
                RightsRevision(
                    revision=row["revision"],
                    recorded_at=row["committed_at"],
                    publication_revision=row["publication_revision"],
                    record=RightsViewRecord.model_validate(row["record"]),
                )
                for row in rows
            ),
        )

    def _rights_revisions(self, source_id: str, *, limit: int) -> list[dict]:
        """The recorded revisions, newest first; none where no history is kept (SQLite, or a database
        before migration 0029)."""
        conn = self.repository.connection
        if conn.dialect.name != "postgresql":
            return []
        table = namespace(conn).qualified(conn, "changes", "rights_revision")
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None:
            return []
        return [
            dict(row)
            for row in conn.execute(
                text(
                    f"SELECT revision, committed_at, publication_revision, record FROM {table}"
                    " WHERE work_id = :w ORDER BY revision DESC LIMIT :n"
                ),
                {"w": source_id, "n": limit},
            ).mappings()
        ]

    def _by_text(self, query: str, jurisdiction) -> tuple[set[str], tuple[str, ...]]:
        """The records whose published text has every word of ``query`` in one part (the section index,
        api.text_index), and why none were looked for when the index could not answer. SQLite, the
        metadata-only pilot, has no section index and no published texts to look in."""
        if self.repository.connection.dialect.name != "postgresql":
            return set(), ()
        scope = (
            None
            if jurisdiction is None
            else (jurisdiction,)
            if isinstance(jurisdiction, str)
            else tuple(sorted(jurisdiction))
        )
        found = text_index.works(
            self.repository.connection, query, text_index.Filters(jurisdictions=scope), bound=MAX_TEXT_WORKS
        )
        if found.state in TEXT_NOT_SEARCHED:
            # Nothing to say where no record has a text that may be published.
            return set(), (TEXT_NOT_SEARCHED[found.state],) if self.repository.any_potential_text() else ()
        if len(found.rows) > MAX_TEXT_WORKS:
            raise ResultTooLarge("Pilot full-text matching exceeds declared bound")
        return set(found.rows), ()

    def search(self, query, *, jurisdiction=None, limit=25, offset=0, access="all"):
        """Records with every word of ``query`` in their title, citation and summary (a word index,
        api.search) or in one part of a published text (the section index), in ID order. Nothing is
        found by part of a word, and nothing is scanned for a substring (G11)."""
        if not normalize_citation(query):
            raise InvalidSearch("Search term must not be blank")
        if not 1 <= limit <= 100 or offset < 0:
            raise InvalidSearch("Invalid page bounds")
        access_where = self._access(access, jurisdiction)
        if not words(query):
            return SearchPage(total=0, items=(), notices=(NO_WORDS,))
        by_text, notices = self._by_text(query, jurisdiction)
        total, pairs = self.repository.search_page(
            query,
            by_text,
            jurisdiction=jurisdiction,
            limit=limit,
            offset=offset,
            access_where=access_where,
        )
        return SearchPage(total=total, items=tuple(self._view(pair) for pair in pairs), notices=notices)

    def _open(self, pairs) -> set[str]:
        """Which of these sources have a text cleared for reuse now."""
        return {
            source.id for source, rights in pairs if next(self._approved(source, rights), None) is not None
        }

    def browse(self, *, query=None, filters=None, sort="relevance", limit=20, offset=0):
        """The website's catalogue (MAC-01): an optional search, filters and sorting, with a count per
        filter value. Each count applies every other selected filter, so choosing a value never hides
        the alternatives. A search matches words as search does; the database ranks each match (every
        word in the title 3, in the citation 2, otherwise 1) and returns a compact row for each; only
        the page's sources are read in full. Bounded: at most BROWSE_BOUND public sources."""
        filters = {k: v for k, v in (filters or {}).items() if v is not None}
        unknown = set(filters) - set(FACETS)
        if unknown or sort not in SORTS:
            raise InvalidSearch("Invalid filter or sort")
        if query is not None and not normalize_citation(query):
            raise InvalidSearch("Search term must not be blank")
        rows, notices = [], ()
        if query is None:
            rows = self.repository.browse_rows(None)
        elif words(query):
            by_text, notices = self._by_text(query, None)
            rows = self.repository.browse_rows(query, by_text)
        else:
            notices = (NO_WORDS,)
        # The few sources that may have a cleared text are read in full to tell open from metadata.
        opened = self._open(self.repository.visible([row.id for row in rows if row.may_have_text]).values())
        candidates = []
        for row in rows:
            values = {
                "jurisdiction": row.jurisdiction_id,
                "type": row.type,
                "language": row.language,
                "status": row.status,
                "year": str(row.year) if row.year else None,
                "access": "open" if row.id in opened else "metadata",
            }
            candidates.append((row.rank, row, values))

        def selected(values, skip=None):
            return all(values[k] == v for k, v in filters.items() if k != skip)

        results = [c for c in candidates if selected(c[2])]
        facets = {
            name: Counter(c[2][name] for c in candidates if selected(c[2], skip=name) and c[2][name])
            for name in FACETS
        }
        by_title = lambda c: c[1].title.casefold()  # noqa: E731
        if sort == "relevance":
            results.sort(key=lambda c: (-c[0], by_title(c)))
        elif sort == "title":
            results.sort(key=by_title)
        else:
            dated = [c for c in results if c[1].year]
            results = sorted(
                dated, key=lambda c: (c[1].year, by_title(c)), reverse=sort == "newest"
            ) + sorted((c for c in results if not c[1].year), key=by_title)
        page = [c[1].id for c in results[offset : offset + limit]]
        pairs = self.repository.visible(page)
        return (
            # A source withdrawn since its row was read is left out of the page.
            SearchPage(
                total=len(results),
                items=tuple(self._view(pairs[i]) for i in page if i in pairs),
                notices=notices,
            ),
            {name: dict(counts.most_common()) for name, counts in facets.items()},
        )

    def _named_by_form(self, value: str) -> list[tuple[tuple, None, None]]:
        """The public sources one of the search index's name forms names, whole: "Nigeria Data
        Protection Act" without its year, "NDPA" by its initials (text_index.name_forms)."""
        folded = text_index.fold(value)
        if len(folded) < text_index.MIN_NAME or not text_index.available(self.repository.connection):
            return []
        works = text_index.named_works(self.repository.connection, value, text_index.Filters())
        pairs = self._public({work for work, name in works.items() if name == folded})
        return [(pairs[i], None, None) for i in sorted(pairs)]

    def _named(self, value: str) -> list[tuple[tuple, NodeViewRecord | None, VersionViewRecord | None]]:
        """The public sources a citation names exactly, by an alias, their citation or their title, each
        with the part and text an alias names (None when it names the whole source)."""
        term = normalize_citation(value)
        if not term:
            return []
        candidates = self.repository.citation_candidates(term)
        pairs = self._public({source_id for source_id, _ in candidates})
        found: dict[str, tuple] = {}
        for source_id, row in candidates:
            if source_id not in pairs or source_id in found and found[source_id][1] is not None:
                continue
            source, rights = pairs[source_id]
            if row is None:
                if term in (normalize_citation(source.citation), normalize_citation(source.title)):
                    found.setdefault(source_id, (pairs[source_id], None, None))
                continue
            alias = Citation.model_validate(row)
            if normalize_citation(alias.value) != term:
                continue
            if alias.node_id and not alias.version_id:
                raise PublicUnavailable("Invalid citation provenance")
            if not alias.version_id:
                found.setdefault(source_id, (pairs[source_id], None, None))
                continue
            version = next((v for v in self._approved(source, rights) if v.id == alias.version_id), None)
            if version is None:
                continue
            node = None
            if alias.node_id:
                row = self.repository.node(alias.node_id)
                if row is None:
                    continue
                node = NodeViewRecord.model_validate(row)
                if node.version_id != alias.version_id:
                    raise PublicUnavailable("Invalid citation provenance")
            found[source_id] = (pairs[source_id], node, version if node else None)
        if len(found) > MAX_UNPAGED_ROWS:
            raise ResultTooLarge("Citation alternatives exceed pilot bound")
        return [found[i] for i in sorted(found)]

    def _match(self, node, version, source, by_id=None) -> ProvisionMatch:
        _, span_hash = self._span(node, version)
        cited = self._pinpoint(node, source.document_type, by_id)
        return ProvisionMatch(
            anchor_id=node.id,
            source_id=source.id,
            version_id=version.id,
            language=version.language,
            kind=node.kind,
            locator=node.locator,
            label=node.label,
            heading=node.heading,
            pinpoint=cited,
            citation=f"{source.citation}, {cited}" if cited else source.citation,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            span_hash=span_hash,
            content_hash=version.content_hash,
            effective_from=version.effective_from,
            effective_until=version.effective_until,
        )

    def _provision(self, pairs, shown: str, key: tuple, *, as_of=None, language=None) -> ProvisionLookup:
        """The parts ``key`` names in the public texts of these sources: chosen by date and language when
        asked, a later reading of the same official file in place of the one it corrects."""
        candidates, nearest, texts, in_scope = [], [], 0, 0
        for source, rights in pairs:
            versions = list(self._approved(source, rights))
            texts += len(versions)
            versions = _latest_readings([v for v in versions if _speaks(v, language)])
            if as_of is not None:
                versions = [v for v in versions if _in_force(v, as_of)]
            in_scope += len(versions)
            for version in versions:
                nodes = self._ordered_nodes(version)
                by_id = {n.id: n for n in nodes}
                keyed: dict[tuple, list] = {}
                for node in nodes:
                    cited = pinpoint.cite(self._chain(node, by_id), source.document_type)
                    if cited is not None:
                        keyed.setdefault(cited[0], []).append(node)
                found = keyed.get(key, [])
                if not found and key[-1].startswith("def:"):  # "s. 2, “Fund”" for a definition in s. 2(1)
                    found = [n for k, ns in keyed.items() if k[0] == key[0] and k[-1] == key[-1] for n in ns]
                if not found and len(key) == 1 and key[0].startswith("para:"):
                    # "[112]" of a judgment numbered through its opinions: in a later opinion.
                    found = [
                        n for k, ns in keyed.items() if k[0].startswith("op:") and k[1:] == key for n in ns
                    ]
                candidates += [(source, version, self._match(n, version, source, by_id)) for n in found]
                if not found:
                    for size in range(len(key) - 1, 0, -1):
                        enclosing = keyed.get(key[:size], [])
                        if len(enclosing) == 1:
                            nearest.append(self._match(enclosing[0], version, source, by_id))
                            break
        if len(candidates) == 1:
            return ProvisionLookup(pinpoint=shown, status="resolved", candidates=(candidates[0][2],))
        if candidates:
            reason = (
                "several_works"
                if len({s.id for s, _, _ in candidates}) > 1
                else "several_texts"
                if len({v.id for _, v, _ in candidates}) > 1
                else "repeated_number"
            )
            return ProvisionLookup(
                pinpoint=shown,
                status="ambiguous",
                reason=reason,
                candidates=tuple(c for _, _, c in candidates),
            )
        reason = "no_public_text" if not texts else "no_text_in_scope" if not in_scope else "not_found"
        return ProvisionLookup(
            pinpoint=shown,
            status="unresolved",
            reason=reason,
            nearest=nearest[0] if len(nearest) == 1 else None,
        )

    def citations(self, value, *, as_of=None, language=None, within=None):
        """A citation resolved to the public sources it names (API-04): exactly, by an alias, citation
        or title, else by a form the search index names a work by (without its closing year, or by its
        initials); or as a source and a pinpoint within it ("NDPA s. 23(1)(a)", "s. 23 NDPA", "Part V
        of the NDPA"), when ``provision`` says which part of which public text it names. A pinpoint
        alone ("section 23") is read within the source ``within``, when a request names one.
        Alternatives are listed, never chosen between."""
        named = self._named(value) or self._named_by_form(value)
        provision = None
        if not named and within and pinpoint.read(value):
            pairs = self._public({within})
            if pairs:
                named = [(pairs[within], None, None)]
                shown = " ".join(value.split())
                provision = self._provision(
                    [pairs[within]], shown, pinpoint.read(value), as_of=as_of, language=language
                )
        elif named:
            parts = [(pair, node, version) for pair, node, version in named if node is not None]
            if parts:
                matches = [self._match(node, version, pair[0]) for pair, node, version in parts]
                provision = ProvisionLookup(
                    pinpoint=matches[0].pinpoint if len(matches) == 1 else " ".join(value.split()),
                    status="resolved" if len(matches) == 1 else "ambiguous",
                    reason=None if len(matches) == 1 else "several_works",
                    candidates=tuple(matches),
                )
        else:
            for work, shown, key in pinpoint.split(value):
                named = self._named(work) or self._named_by_form(work)
                if named:
                    pairs = [pair for pair, _, _ in named]
                    provision = self._provision(pairs, shown, key, as_of=as_of, language=language)
                    break
        matches = tuple(self._view(pair) for pair, _, _ in named)
        return CitationView(
            status="resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved",
            matches=matches,
            provision=provision,
        )

    def readable(self, version_id: str) -> str | None:
        """A public version's content hash, checked on its rights gate without reading its text; None
        when it is not public now."""
        source_id = self.repository.version_owner(version_id)
        if source_id is None:
            return None
        try:
            source, rights = self._require(source_id)
        except SourceUnavailable:
            return None
        for gate in self.repository.version_gates(source.id, rights.approved_content_hashes):
            if gate.id == version_id and decide(rights, gate, now=self.now).full_text:
                return gate.content_hash
        return None

    # --- the text search (OA-S1, RET-02, API-04) ------------------------------------------------------

    def _scope_records(self, filters: text_index.Filters) -> int:
        if filters.source_id or filters.version_id:
            source_id = filters.source_id or self.repository.version_owner(filters.version_id)
            return len(self._public({source_id})) if source_id else 0
        return self.repository.count_public(
            jurisdictions=filters.jurisdictions,
            types=filters.types,
            statuses=filters.statuses,
            language=filters.language,
            years=(filters.year_from, filters.year_to),
            works=filters.admitted,
        )

    @staticmethod
    def _within(source, version, filters: text_index.Filters) -> bool:
        return (
            filters.admits(source.jurisdiction_id, source.id)
            and (not filters.types or source.document_type in filters.types)
            and (not filters.statuses or source.legal_status in filters.statuses)
            and (filters.year_from is None or (source.year or 0) >= filters.year_from)
            and (filters.year_to is None or (source.year is not None and source.year <= filters.year_to))
            and (not filters.source_id or source.id == filters.source_id)
            and (not filters.version_id or version.id == filters.version_id)
            and _speaks(version, filters.language)
            and (filters.as_of is None or _in_force(version, filters.as_of))
        )

    def _hit(self, version, source, rights, *, unit, snippet, matched_by, score, scope=None) -> TextHit:
        text_, start, end, marks = snippet
        return TextHit(
            anchor_id=unit["node_id"],
            source_id=source.id,
            version_id=version.id,
            title=source.title,
            citation=f"{source.citation}, {unit['pinpoint']}" if unit["pinpoint"] else source.citation,
            pinpoint=unit["pinpoint"],
            kind=unit["kind"],
            locator=unit["locator"],
            heading=unit["heading"],
            language=version.language,
            jurisdiction_id=source.jurisdiction_id,
            document_type=source.document_type,
            legal_status=source.legal_status,
            year=source.year,
            effective_from=version.effective_from,
            effective_until=version.effective_until,
            start_byte=unit["start_byte"],
            end_byte=unit["end_byte"],
            snippet=text_,
            snippet_start_byte=start,
            snippet_end_byte=end,
            highlights=marks,
            content_hash=version.content_hash,
            matched_by=matched_by,
            score=score,
            official_url=str(version.canonical_url),
            licence=rights.licence,
            attribution_required=rights.attribution_required,
            attribution_text=rights.attribution_text,
            applicability=scope.label(source) if scope is not None else None,
        )

    def search_text(
        self, query: str, filters: text_index.Filters, *, limit: int = 25, cursor=None, scope=None
    ):
        """A page of the passages of published texts that match ``query`` (OA-S1), best first by the
        documented ranking (api.text_index). A query that reads as a citation of a part is resolved, not
        searched: the part comes first (RET-02). Every hit's text is rechecked before its words are used;
        ``status`` says whether nothing matched, the scope has no published text, or the search could
        not run, and ``coverage`` what was searched. With an applicable-law ``scope`` (api.applicable),
        the records it admits join the jurisdiction's, each hit says how it relates to that jurisdiction,
        and the page states the widening."""
        if scope is not None:
            filters = replace(filters, admitted=scope.admitted)
        query = " ".join(query.split())
        if not query:
            raise InvalidSearch("Search term must not be blank")
        if not 1 <= limit <= 100:
            raise InvalidSearch("Invalid page bounds")
        offset = _offset(cursor, "hits")
        if offset > MAX_HIT_OFFSET:
            raise InvalidSearch("Invalid cursor")
        conn, notices = self.repository.connection, []
        citation = self.citations(query, as_of=filters.as_of, language=filters.language)
        if not citation.matches or any(not filters.admits(m.jurisdiction_id, m.id) for m in citation.matches):
            citation = None
        first = []
        if citation and citation.provision and citation.provision.status == "resolved":
            match = citation.provision.candidates[0]
            version, source, rights = self.version(match.version_id)
            if self._within(source, version, filters):
                first.append((version, source, rights, match))
        exclude = (first[0][3].version_id, first[0][3].start_byte, first[0][3].end_byte) if first else None
        lead = first if offset == 0 else []
        found = text_index.search(
            conn,
            query,
            filters,
            limit=limit - len(lead),
            offset=max(0, offset - len(first)),
            exclude=exclude,
        )
        loaded: dict[str, tuple | None] = {}

        def public(version_id):
            if version_id not in loaded:
                try:
                    loaded[version_id] = self.version(version_id)
                except SourceUnavailable:
                    loaded[version_id] = None
            return loaded[version_id]

        rows = [
            row
            for row in found.rows
            if public(row.version_id) and public(row.version_id)[0].content_hash == row.content_hash
        ]
        if len(rows) < len(found.rows):
            notices.append(
                f"{len(found.rows) - len(rows)} passages were left out: their text stopped being public while "
                "this search ran."
            )
        units = [dict(row._mapping) for row in rows]
        words = [
            public(u["version_id"])[0].canonical_text.encode()[u["start_byte"] : u["end_byte"]].decode()
            for u in units
        ]
        heads = text_index.headlines(
            conn, query, [(u["config"], w) for u, w in zip(units, words, strict=True)]
        )
        items = []
        for version, source, rights, match in lead:
            span = version.canonical_text.encode()[match.start_byte : match.end_byte].decode()
            unit = match.model_dump() | {"node_id": match.anchor_id}
            items.append(
                self._hit(
                    version, source, rights, unit=unit,
                    snippet=text_index.place(span, "", match.start_byte), matched_by="citation", score=None,
                    scope=scope,
                )
            )  # fmt: skip
        for unit, words_, head in zip(units, words, heads, strict=True):
            version, source, rights = public(unit["version_id"])
            items.append(
                self._hit(
                    version, source, rights, unit=unit,
                    snippet=text_index.place(words_, head, unit["start_byte"]), matched_by="text",
                    score=round(float(unit["score"]), 6), scope=scope,
                )
            )  # fmt: skip
        searched = (
            text_index.scope(conn, filters) if text_index.available(conn) else text_index.Scope(0, 0, ())
        )
        records = self._scope_records(filters)
        total = found.total + len(first)
        if found.state in ("unavailable", "building", "timeout"):
            status = "degraded"
            notices.append(
                {
                    "unavailable": "Text search needs the section index, which this deployment does not have.",
                    "building": "The section index is being built. Try again in a few minutes.",
                    "timeout": "The search took longer than ten seconds. Add words or narrow it with filters.",
                }[found.state]
            )
        elif searched.texts == 0:
            status = "unsupported_scope"
        elif not items:
            status = "no_result"
            if any(text_index.config_for(language) in found.common_only for language in searched.languages):
                notices.append(
                    "The search has only common words, which are not indexed. Add a distinctive word."
                )
        else:
            status = "results"
        analysis = {language: text_index.config_for(language) for language in searched.languages}
        notices += text_index.word_for_word(searched.languages)
        next_offset = offset + limit
        if next_offset < total and next_offset > MAX_HIT_OFFSET:
            notices.append(
                f"Only the first {MAX_HIT_OFFSET:,} passages are served. Narrow the search to see others."
            )
        return TextSearchPage(
            status=status,
            query=query,
            total=total,
            total_exact=found.exact,
            items=tuple(items),
            next_cursor=_cursor(next_offset, "hits")
            if next_offset < total and next_offset <= MAX_HIT_OFFSET
            else None,
            citation=citation,
            coverage=SearchCoverage(
                records_in_scope=records,
                records_with_text=searched.records,
                texts_searched=searched.texts,
                notice=(
                    f"Only published texts are searched: {searched.records:,} of the {records:,} records in this "
                    "scope have one. Find records without a published text with /search."
                ),
            ),
            analysis=analysis,
            analyzer=text_index.ANALYZER,
            ranking=text_index.RANKING,
            index_built_at=searched.built_at,
            notices=tuple(notices),
            applicability=scope.statement if scope is not None else None,
        )

    def jurisdictions(self, as_of: date | None = None):
        """The catalogue's jurisdictions; with a date, each country's reviewed memberships on it."""
        views = tuple(JurisdictionView.model_validate(row) for row in self.repository.rows("jurisdictions"))
        if as_of is None:
            return views
        held = self.memberships()
        return tuple(
            view
            if country(view.id) is None  # a body is no member: its members are in jurisdiction()
            else view.model_copy(update={"member_of": membership_list(held, view.id, "member", as_of)})
            for view in views
        )

    def jurisdiction(self, jurisdiction_id: str, as_of: date) -> JurisdictionDetail:
        """One jurisdiction on a date: a country's bodies, or a body's members, reviewed only. Every
        jurisdiction of the profiles answers, catalogued or not; an unknown ID is unavailable."""
        rows = self.repository.rows("jurisdictions", ids=[jurisdiction_id])
        profile = _profiles().get(jurisdiction_id)
        if rows:
            view = JurisdictionView.model_validate(rows[0])
        elif profile is not None:
            view = JurisdictionView.model_validate(catalogue_entry(jurisdiction_id).model_dump())
        else:
            raise SourceUnavailable("No such jurisdiction")
        held = self.memberships(jurisdiction_id)
        body = country(jurisdiction_id) is None
        return JurisdictionDetail(
            **view.model_dump(exclude={"member_of"}),
            level=profile.level if profile else None,
            as_of=as_of,
            member_of=None if body else membership_list(held, jurisdiction_id, "member", as_of),
            members=membership_list(held, jurisdiction_id, "body", as_of) if body else None,
        )

    def memberships(self, jurisdiction_id: str | None = None) -> list[MembershipView]:
        """The reviewed memberships published to the catalogue (corpus.membership, operations.memberships),
        all or those of one member or body, each with its reviewed suspensions; none in SQLite or before
        migration 0042."""
        conn = self.repository.connection
        if conn.dialect.name != "postgresql":
            return []
        ns = namespace(conn)
        table = ns.qualified(conn, "corpus", "membership")
        suspended = ns.qualified(conn, "corpus", "membership_suspension")
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": suspended}) is None:
            return []
        rows = (
            conn.execute(
                text(
                    "SELECT m.membership_id AS id, m.member_id, m.body_id, m.valid_from, m.valid_to, m.basis,"
                    " m.evidence_url, m.note, m.reviewed_by, m.reviewed_at,"
                    " coalesce(mj.payload->>'name', m.member_id) AS member_name,"
                    " coalesce(bj.payload->>'name', m.body_id) AS body_name"
                    f" FROM {table} m JOIN jurisdictions mj ON mj.id = m.member_id"
                    " JOIN jurisdictions bj ON bj.id = m.body_id"
                    + ("" if jurisdiction_id is None else " WHERE m.member_id = :j OR m.body_id = :j")
                    + " ORDER BY m.body_id, m.member_id, m.valid_from_earliest LIMIT :bound"
                ),
                {"j": jurisdiction_id, "bound": MAX_UNPAGED_ROWS + 1},
            )
            .mappings()
            .all()
        )
        if len(rows) > MAX_UNPAGED_ROWS:
            raise ResultTooLarge("Pilot unpaged response exceeds row bound")
        suspensions: dict[str, list] = {}
        if rows:
            for row in conn.execute(
                text(
                    "SELECT suspension_id AS id, membership_id, valid_from, valid_to, legal_basis, evidence_url,"
                    f" lifted_evidence_url, note, reviewed_by, reviewed_at FROM {suspended}"
                    " WHERE membership_id = ANY(:ids) ORDER BY membership_id, valid_from_earliest"
                ),
                {"ids": [row["id"] for row in rows]},
            ).mappings():
                suspensions.setdefault(row["membership_id"], []).append(
                    SuspensionView.model_validate(
                        {k: v for k, v in row.items() if k != "membership_id"}
                        | {"reviewed_at": row["reviewed_at"].astimezone(UTC)}
                    )
                )
        # In UTC whatever the database session's time zone.
        return [
            MembershipView.model_validate(
                dict(row)
                | {
                    "reviewed_at": row["reviewed_at"].astimezone(UTC),
                    "suspensions": tuple(suspensions.get(row["id"], ())),
                }
            )
            for row in rows
        ]

    def countries(self):
        """Each country with catalogued material, for choosing a plan's countries: its sources and how
        many have open full text. Sub-national material counts under its country (api.reach)."""
        names = {j.id: j.name for j in self.jurisdictions()}
        found: dict[str, dict] = {}
        for source, rights in self.repository.sources():
            code = country(source.jurisdiction_id)
            if code is None:
                continue
            entry = found.setdefault(
                code, {"id": code, "name": names.get(code, code.upper()), "sources": 0, "full_text": 0}
            )
            entry["sources"] += 1
            entry["full_text"] += any(True for _ in self._approved(source, rights))
        return sorted(found.values(), key=lambda c: c["name"])

    def coverage(self):
        """Counts of public sources, by jurisdiction from the database, and of those with a cleared
        text and its versions, from the few sources that have any approved."""
        scopes = Counter(self.repository.public_counts())
        total = sum(scopes.values())
        open_sources, published = 0, 0
        for source, rights in self.repository.sources(where=self.repository.potential_text()):
            versions = sum(1 for _ in self._approved(source, rights))
            published += versions
            open_sources += bool(versions)
        return CoverageView(
            catalogued_sources=total,
            open_full_text_sources=open_sources,
            metadata_only_sources=total - open_sources,
            published_versions=published,
            jurisdictions=dict(scopes),
            notice="Catalogue coverage only; not a claim of complete or current law.",
        )
