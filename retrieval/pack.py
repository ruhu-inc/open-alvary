"""The evidence pack (RSV-07): the router's plans run their arms over the section index, the results
are fused, signalled and diversified (retrieval.ranking), and each selected unit is read from its
public text after the rights check, with its exact words or an excerpt within the byte budget, its
hashes, currentness (retrieval.legal_time), authority inputs, derived links and the explanation of its
position. The same request over the same revisions gives the same pack."""

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from sqlalchemy import text as sql

from api import text_index
from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable, expression_id
from retrieval import indexer, links, rerank, router
from retrieval.legal_time import Events
from retrieval.ranking import INSTRUMENT_RANK, PROFILE, Candidate, Hit, apply_signals, diversify, fuse
from schema.public import (
    CatalogueMatch,
    EvidenceAuthority,
    EvidenceBytes,
    EvidenceContext,
    EvidenceCoverage,
    EvidenceDefinition,
    EvidencePack,
    EvidenceQualification,
    EvidenceQuery,
    EvidenceReference,
    EvidenceRevisions,
    EvidenceUnit,
    NextStep,
    RankingExplanation,
    RankingSignal,
)

ARM_LIMIT = 100  # RET-02: at most 100 raw candidates per arm
SIDE_LIMIT = 20  # the instrument, definition and document arms
MIN_EXCERPT = 256  # below this many bytes left, no further unit is started
INCLUDE = frozenset({"context", "expansion", "ranking"})
QUALIFYING = ("repealed_by", "superseded_by", "amended_by")


@dataclass(frozen=True)
class Request:
    query: str
    filters: text_index.Filters
    max_units: int = 10
    max_evidence_bytes: int = 49_152
    include: frozenset[str] = INCLUDE


def _hit(row, unit: str | None = None) -> Hit:
    m = row._mapping if hasattr(row, "_mapping") else row
    node_kind = m.get("kind") or "document"
    if unit is None:
        unit = "provision" if node_kind in ("section", "article", "paragraph", "definition", "subsection",
                                            "subparagraph", "schedule", "annex", "table") else "container_run"  # fmt: skip
    return Hit(
        version_id=m["version_id"],
        start_byte=m["start_byte"],
        end_byte=m["end_byte"],
        work_id=m["work_id"],
        node_id=m.get("node_id"),
        unit=unit,
        kind=node_kind if unit != "document" else "document",
        locator=m.get("locator") or "document",
        pinpoint=m.get("pinpoint"),
        heading=m.get("heading"),
        language=m["language"],
        config=m["config"],
        content_hash=m["content_hash"],
        legal_status=m.get("legal_status") or "unknown",
        replaced=bool(m.get("replaced")),
        jurisdiction_id=m.get("jurisdiction_id") or "unknown",
        document_type=m.get("document_type") or "other",
    )


def dataset_revision(conn) -> str:
    """The change feed's epoch and highest publication revision: every published change moves it."""
    try:
        from changes.feed import Feed

        feed = Feed(conn, None)
        return f"{feed.state()['epoch']}:{feed.high()}"
    except Exception:  # no change log on this deployment (SQLite pilot)
        return "unknown"


_EMBEDDERS: dict[str, object] = {}  # one per profile, kept for the process


class Retriever:
    def __init__(
        self,
        service: LegacyCatalogue,
        *,
        today: date | None = None,
        vector_profile: str | None = None,
        embedder=None,
        reranker=None,
    ):
        self.service = service
        self.conn = service.repository.connection
        self.today = today or datetime.now(UTC).date()
        self.vector_profile = vector_profile or os.environ.get("OPEN_ALVARY_VECTOR_PROFILE")
        self.generation = indexer.active(self.conn, self.vector_profile) if self.vector_profile else None
        self.embedder = embedder
        self.reranker = reranker if reranker is not None else rerank.configured()
        self._loaded: dict[str, tuple | None] = {}
        self._nodes: dict[str, list] = {}
        self._qualifications: dict[str, tuple] = {}
        self.events = Events(self.conn)

    # --- reading public texts --------------------------------------------------------------------

    def _public(self, version_id: str):
        if version_id not in self._loaded:
            try:
                self._loaded[version_id] = self.service.version(version_id)
            except SourceUnavailable:
                self._loaded[version_id] = None
        return self._loaded[version_id]

    def _structure(self, version) -> list:
        if version.id not in self._nodes:
            self._nodes[version.id] = self.service._ordered_nodes(version)
        return self._nodes[version.id]

    def _qualified(self, source_id: str) -> tuple:
        if source_id not in self._qualifications:
            page = self.service.relationships(source_id, kinds=list(QUALIFYING), limit=25, current=False)
            self._qualifications[source_id] = tuple(
                EvidenceQualification(
                    kind=item.kind, source_id=item.source_id, title=item.title, citation=item.citation,
                    effective_date=item.effective_date,
                )
                for item in page.items
                if item.verified
            )  # fmt: skip
        return self._qualifications[source_id]

    # --- arms ------------------------------------------------------------------------------------

    def _citation_arm(self, plan: router.Plan, filters) -> list[Hit]:
        provision = plan.citation.provision
        hits = []
        for match in provision.candidates:
            loaded = self._public(match.version_id)
            if loaded is None or not self.service._within(loaded[1], loaded[0], filters):
                continue
            version, source, _ = loaded
            node = next((n for n in self._structure(version) if n.id == match.anchor_id), None)
            if node is None:
                continue
            hits.append(
                Hit(
                    version_id=version.id, start_byte=match.start_byte, end_byte=match.end_byte,
                    work_id=source.id, node_id=node.id, unit="provision", kind=node.kind,
                    locator=node.locator, pinpoint=match.pinpoint, heading=node.heading,
                    language=version.language, config=text_index.config_for(version.language),
                    content_hash=version.content_hash, legal_status=source.legal_status, replaced=False,
                    jurisdiction_id=source.jurisdiction_id, document_type=source.document_type,
                )
            )  # fmt: skip
        return hits

    def _documents_of(self, works, filters) -> list[Hit]:
        table = text_index._documents(self.conn)
        if table is None or not works:
            return []
        where, params = replace(filters, kinds=(), source_id=None, version_id=None).where()
        rows = self.conn.execute(
            sql(
                f"SELECT u.version_id, u.work_id, u.start_byte, u.end_byte, u.node_id, u.language, u.config,"
                f" u.content_hash, u.legal_status, u.replaced, u.jurisdiction_id, u.document_type"
                f" FROM {table} u WHERE u.work_id = ANY(:works) AND {where}"
                " ORDER BY u.replaced, u.version_id"
            ),
            params | {"works": sorted(works)},
        ).all()
        return [_hit(r, "document") for r in rows]

    def _definitions(self, term: str, rows) -> list[Hit]:
        """The definition nodes of ``term`` inside the units that match it, in the units' order: a
        section is one unit, so the definition is found within it and returned as its own span."""
        hits, wanted = [], term.casefold()
        for row in rows:
            loaded = self._public(row.version_id)
            if loaded is None or loaded[0].content_hash != row.content_hash:
                continue
            version, source, _ = loaded
            nodes = self._structure(version)
            by_id = {n.id: n for n in nodes}
            for defined, node in links.definitions(version.canonical_text.encode(), nodes):
                if defined.casefold() != wanted or not row.start_byte <= node.start_byte < row.end_byte:
                    continue
                hits.append(
                    Hit(
                        version_id=version.id, start_byte=node.start_byte, end_byte=node.end_byte,
                        work_id=source.id, node_id=node.id, unit="provision", kind=node.kind,
                        locator=node.locator, pinpoint=self.service._pinpoint(node, source.document_type, by_id),
                        heading=node.heading, language=version.language, config=row.config,
                        content_hash=version.content_hash, legal_status=row.legal_status,
                        replaced=bool(row.replaced), jurisdiction_id=source.jurisdiction_id,
                        document_type=source.document_type,
                    )
                )  # fmt: skip
        return hits

    def _arms(self, query: str, plan: router.Plan, filters) -> tuple[dict[str, list[Hit]], list[str], int]:
        arms: dict[str, list[Hit]] = {}
        degraded: list[str] = []
        if "citation" in plan.routes:
            arms["citation"] = self._citation_arm(plan, filters)
        if "instrument" in plan.routes:
            hits = self._documents_of(plan.works, filters)
            words = plan.residual or query
            for work_id in sorted(plan.works):
                found = text_index.search(
                    self.conn, words, replace(filters, source_id=work_id), limit=SIDE_LIMIT, offset=0
                )
                hits += [_hit(r) for r in found.rows]
            arms["instrument"] = hits
        if "definition" in plan.routes:
            scoped = filters if len(plan.works) != 1 else replace(filters, source_id=next(iter(plan.works)))
            found = text_index.search(self.conn, plan.term, scoped, limit=SIDE_LIMIT, offset=0)
            arms["definition"] = self._definitions(plan.term, found.rows)
        total = 0
        if "topic" in plan.routes:
            found = text_index.search(self.conn, query, filters, limit=ARM_LIMIT, offset=0)
            if found.state in ("unavailable", "building", "timeout"):
                degraded.append(f"lexical:{found.state}")
            arms["lexical"] = [_hit(r) for r in found.rows]
            total = found.total
            if found.state == "ok" and found.total < SIDE_LIMIT and "citation" not in plan.routes:
                # A question written as a sentence rarely has every word in one provision: units with
                # any of its words form a second arm, so units with all of them still rank first.
                relaxed = text_index.search(
                    self.conn, query, filters, limit=ARM_LIMIT, offset=0, any_terms=True
                )
                arms["lexical_any"] = [_hit(r) for r in relaxed.rows]
            documents = text_index.search_documents(self.conn, query, filters, limit=SIDE_LIMIT)
            arms["document"] = [_hit(r, "document") for r in documents.rows]
            dense = self._dense(query, filters, degraded)
            if dense is not None:
                arms["dense"] = dense
        return arms, degraded, total

    def _dense(self, query: str, filters, degraded: list[str]) -> list[Hit] | None:
        """The dense arm over the active generation (RSV-04); None (with the reason in ``degraded``) when
        no profile is configured or active, or the embedder or search fails."""
        if not self.vector_profile:
            degraded.append("dense:not_configured")
            return None
        if self.generation is None:
            degraded.append("dense:not_active")
            return None
        from retrieval.vectors import Profile, embedder_for

        profile = Profile.from_dict(self.generation["profile"])
        try:
            embedder = self.embedder or _EMBEDDERS.setdefault(profile.id, embedder_for(profile))
            [vector] = embedder.embed([query], query=True)
            units, _ = text_index._tables(self.conn)
            where, params = filters.where()
            with self.conn.begin_nested():
                found = indexer.store_for(self.conn, self.generation).search(
                    units, where, params, vector, ARM_LIMIT
                )
        except Exception:  # the lexical arms carry the request; the pack says the dense arm did not run
            degraded.append("dense:unavailable")
            return None
        return [_hit(row) for row, _distance in found]

    def _rerank(self, query: str, candidates: list[Candidate], degraded: list[str]) -> None:
        """Score the first fused candidates with the reranker (RSV-05); on failure the fused order stands."""
        if self.reranker is None:
            degraded.append("reranker:not_configured")
            return
        pool = [c for c in candidates[: rerank.MAX_CANDIDATES] if self._public(c.hit.version_id)]
        texts = [
            self._public(c.hit.version_id)[0]
            .canonical_text.encode()[c.hit.start_byte : c.hit.end_byte]
            .decode()
            for c in pool
        ]
        try:
            scores = self.reranker.scores(query, texts) if pool else []
        except Exception:
            degraded.append("reranker:unavailable")
            return
        for candidate, score in zip(pool, scores, strict=True):
            candidate.rerank_score = score

    # --- the pack --------------------------------------------------------------------------------

    def retrieve(self, request: Request) -> EvidencePack:
        query = " ".join(request.query.split())
        if not query:
            raise InvalidSearch("Search term must not be blank")
        if not 1 <= request.max_units <= 25 or not 4_096 <= request.max_evidence_bytes <= 262_144:
            raise InvalidSearch("Invalid evidence bounds")
        filters = request.filters
        citation = self.service.citations(query, as_of=filters.as_of, language=filters.language)
        if (
            citation.matches
            and filters.jurisdictions is not None
            and any(m.jurisdiction_id not in filters.jurisdictions for m in citation.matches)
        ):
            citation = None
        works = text_index.named_works(self.conn, query, filters)
        plan = router.plan(
            query, citation=citation, works=works, as_of=filters.as_of, jurisdictions=filters.jurisdictions
        )
        arms, degraded, _ = self._arms(query, plan, filters)
        fused = fuse(arms)
        self._rerank(query, fused, degraded)
        candidates = apply_signals(fused, set(works))
        scoped = "citation" in plan.routes or len(works) == 1 or bool(filters.source_id or filters.version_id)
        candidates = diversify(candidates, scoped=scoped, comparative=plan.comparative)
        evidence, used, sent, omitted, notices = self._assemble(query, candidates, plan, request)

        searched = (
            text_index.scope(self.conn, filters)
            if text_index.available(self.conn)
            else text_index.Scope(0, 0, ())
        )
        records = self.service._scope_records(filters)
        if degraded and not evidence:
            status = "degraded"
        elif searched.texts == 0:
            status = "unsupported_scope"
        elif not evidence:
            status = "no_result"
        else:
            status = "results"
        catalogue, next_steps = self._catalogue(query, citation, status, filters)
        if citation and citation.provision and citation.provision.status == "ambiguous":
            next_steps.append(
                NextStep(
                    kind="ambiguous_citation",
                    detail=f"The citation names {len(citation.provision.candidates)} parts; each is in the evidence or "
                    "listed here. Choose with as_of or language.",
                    candidates=tuple(c.anchor_id for c in citation.provision.candidates),
                )
            )
        notices += [
            f"Texts in {language} are matched word for word until the language passes the search tests."
            for language in searched.languages
            if text_index.config_for(language) == "simple"
        ]
        return EvidencePack(
            status=status,
            query=EvidenceQuery(
                text=query,
                routes=plan.routes,
                filters=self._filters(filters),
                as_of=filters.as_of,
            ),
            revisions=EvidenceRevisions(
                dataset=dataset_revision(self.conn),
                router=router.ROUTER,
                ranking=PROFILE,
                analyzer=text_index.ANALYZER,
                vector=f"{self.generation['profile_id']}@{self.generation['generation_id']}"
                if self.generation
                else None,
                reranker=self.reranker.profile if self.reranker is not None else None,
            ),
            coverage=EvidenceCoverage(
                records_in_scope=records,
                records_with_text=searched.records,
                texts_searched=searched.texts,
                dense_coverage=self._dense_coverage(filters),
                degraded=tuple(dict.fromkeys(degraded)),
                incomplete_evidence=omitted > 0,
                omitted_units=omitted,
                notices=tuple(notices),
            ),
            evidence=tuple(evidence),
            catalogue=tuple(catalogue),
            next_steps=tuple(next_steps),
            bytes=EvidenceBytes(unique=used, transmitted=sent, budget=request.max_evidence_bytes),
        )

    def _dense_coverage(self, filters) -> float | None:
        """The share of texts in scope whose vectors are in the active generation."""
        if self.generation is None or not text_index.available(self.conn):
            return None
        units, _ = text_index._tables(self.conn)
        where, params = filters.where()
        row = self.conn.execute(
            sql(
                f"SELECT count(DISTINCT u.version_id) AS texts, count(DISTINCT u.version_id) FILTER"
                f" (WHERE j.state = 'done') AS done FROM {units} u LEFT JOIN {indexer._t(self.conn, 'index_job')} j"
                f" ON j.version_id = u.version_id AND j.generation_id = :g WHERE {where}"
            ),
            params | {"g": self.generation["generation_id"]},
        ).one()
        return round(row.done / row.texts, 4) if row.texts else None

    @staticmethod
    def _filters(filters: text_index.Filters) -> dict:
        shown = {
            "jurisdictions": list(filters.jurisdictions) if filters.jurisdictions is not None else None,
            "language": filters.language,
            "document_types": list(filters.types) or None,
            "legal_status": list(filters.statuses) or None,
            "year_from": filters.year_from,
            "year_to": filters.year_to,
            "source_id": filters.source_id,
            "version_id": filters.version_id,
        }
        return {k: v for k, v in shown.items() if v is not None}

    def _catalogue(self, query, citation, status, filters) -> tuple[list[CatalogueMatch], list[NextStep]]:
        matches = [m for m in (citation.matches if citation else ()) if not m.has_full_text]
        if status != "results" and not matches:
            jurisdiction = None if filters.jurisdictions is None else set(filters.jurisdictions)
            try:
                matches = [
                    m
                    for m in self.service.search(
                        query[:200], jurisdiction=jurisdiction, limit=5, access="metadata"
                    ).items
                ]
            except InvalidSearch:
                matches = []
        found = [
            CatalogueMatch(
                source_id=m.id, title=m.title, citation=m.citation, jurisdiction_id=m.jurisdiction_id,
                document_type=m.document_type, official_url=str(m.canonical_url),
            )
            for m in matches[:5]
        ]  # fmt: skip
        steps = []
        if found:
            steps.append(
                NextStep(
                    kind="metadata_only",
                    detail="These records match but have no published text yet: their official links are given, "
                    "never their words.",
                    candidates=tuple(m.source_id for m in found),
                )
            )
        return found, steps

    def _assemble(self, query, candidates: list[Candidate], plan, request: Request):
        evidence: list[EvidenceUnit] = []
        spans_used: set[tuple] = set()
        used = sent = omitted = 0
        notices: list[str] = []
        budget = request.max_evidence_bytes
        skipped = 0
        for candidate in candidates:
            if len(evidence) >= request.max_units:
                break
            hit = candidate.hit
            loaded = self._public(hit.version_id)
            if loaded is None or loaded[0].content_hash != hit.content_hash:
                skipped += 1
                continue
            version, source, rights = loaded
            raw = version.canonical_text.encode()
            span = raw[hit.start_byte : hit.end_byte]
            left = budget - used
            if left < MIN_EXCERPT:
                omitted += 1
                continue
            complete = len(span) <= left
            if complete:
                start, end = hit.start_byte, hit.end_byte
            else:
                start, end = self._excerpt(query, hit, span, left)
            words = raw[start:end].decode("utf-8")
            if (version.id, start, end) not in spans_used:
                spans_used.add((version.id, start, end))
                used += end - start
            sent += end - start
            nodes = self._structure(version)
            by_id = {n.id: n for n in nodes}
            context = None
            if "context" in request.include or "expansion" in request.include:
                context, extra_unique, extra_sent = self._context(
                    candidate,
                    source.id,
                    version,
                    raw,
                    nodes,
                    by_id,
                    words,
                    spans_used,
                    request,
                    budget - used,
                )
                used += extra_unique
                sent += extra_sent
            evidence.append(
                EvidenceUnit(
                    anchor_id=hit.node_id or version.id,
                    source_id=source.id,
                    version_id=version.id,
                    expression_id=expression_id(version),
                    unit=hit.unit,
                    kind=hit.kind,
                    locator=hit.locator,
                    pinpoint=hit.pinpoint,
                    heading=hit.heading,
                    citation=f"{source.citation}, {hit.pinpoint}" if hit.pinpoint else source.citation,
                    title=source.title,
                    language=version.language,
                    jurisdiction_id=source.jurisdiction_id,
                    document_type=source.document_type,
                    official_url=str(version.canonical_url),
                    text=words,
                    text_start_byte=start,
                    text_end_byte=end,
                    complete=complete,
                    start_byte=hit.start_byte,
                    end_byte=hit.end_byte,
                    span_hash=hashlib.sha256(span).hexdigest(),
                    content_hash=version.content_hash,
                    licence=rights.licence,
                    attribution_required=rights.attribution_required,
                    attribution_text=rights.attribution_text,
                    currentness=self.events.currentness(
                        source, version, hit.locator, inner(nodes, hit), plan.as_of, self.today
                    ),
                    authority=EvidenceAuthority(
                        document_type=source.document_type,
                        instrument_rank=INSTRUMENT_RANK.get(source.document_type),
                        court_relation="unknown" if source.document_type == "judgment" else "not_applicable",
                    ),
                    ranking=self._explain(candidate, plan, len(evidence) + 1)
                    if "ranking" in request.include
                    else None,
                    context=context,
                    warnings=self._warnings(hit, complete),
                )
            )
        if skipped:
            notices.append(f"{skipped} units were left out: their text stopped being public while this ran.")
        if omitted:
            notices.append(
                f"{omitted} further units did not fit max_evidence_bytes ({budget:,}); raise it or narrow the query."
            )
        return evidence, used, sent, omitted, notices

    def _excerpt(self, query, hit: Hit, span: bytes, left: int) -> tuple[int, int]:
        """The unit's best fragment for the query (its exact bytes), widened to whole sentences while
        it fits; or its opening words."""
        words = span.decode("utf-8")
        [head] = text_index.headlines(self.conn, query, [(hit.config, words)])
        _, start, end, _ = text_index.place(words, head, hit.start_byte)
        raw_start, raw_end = start - hit.start_byte, end - hit.start_byte
        while raw_end - raw_start > left and raw_end > raw_start:
            raw_end = span.rfind(b" ", raw_start, raw_end - 1)
            if raw_end <= raw_start:
                raw_end = raw_start
        for stop in (b". ", b".\n", b"\n\n"):
            later = span.find(stop, raw_end)
            if later != -1 and later + 1 - raw_start <= left:
                raw_end = later + 1
                break
        while raw_end < len(span) and span[raw_end] & 0xC0 == 0x80:
            raw_end -= 1
        return hit.start_byte + raw_start, hit.start_byte + max(raw_start, raw_end)

    def _context(self, candidate, source_id, version, raw, nodes, by_id, words, spans_used, request, left):
        hit = candidate.hit
        node = by_id.get(hit.node_id)
        chain, at = [], node
        while at is not None and len(chain) < 64:
            if at.kind != "document" and (at.label or at.heading):
                chain.append(" ".join(filter(None, (at.label, at.heading))))
            at = by_id.get(at.parent_id)
        definitions, references = (), ()
        unique = sent = 0
        if "expansion" in request.include and hit.unit != "document":
            own = {hit.node_id} if hit.node_id else set()
            found = []
            for term, defined in links.in_scope(words, links.definitions(raw, nodes), own):
                size = defined.end_byte - defined.start_byte
                fresh = (version.id, defined.start_byte, defined.end_byte) not in spans_used
                if fresh and size > left - unique:
                    continue
                if fresh:
                    spans_used.add((version.id, defined.start_byte, defined.end_byte))
                    unique += size
                sent += size
                body = raw[defined.start_byte : defined.end_byte]
                found.append(
                    EvidenceDefinition(
                        term=term, anchor_id=defined.id, locator=defined.locator, text=body.decode("utf-8"),
                        start_byte=defined.start_byte, end_byte=defined.end_byte,
                        span_hash=hashlib.sha256(body).hexdigest(),
                    )
                )  # fmt: skip
            definitions = tuple(found)
            by_locator = {n.locator: n for n in nodes}
            references = tuple(
                EvidenceReference(pinpoint=shown, anchor_id=target.id, locator=target.locator)
                for shown, target in links.references(words, by_locator, hit.locator)
            )
        context = EvidenceContext(
            heading_path=tuple(reversed(chain)) if "context" in request.include else (),
            definitions=definitions,
            references=references,
            qualifications=self._qualified(source_id) if "context" in request.include else (),
            also_in=tuple(candidate.also_in),
        )
        return context, unique, sent

    @staticmethod
    def _explain(candidate: Candidate, plan, position: int) -> RankingExplanation:
        return RankingExplanation(
            position=position,
            router=router.ROUTER,
            routes=plan.routes,
            profile=PROFILE,
            arms=dict(candidate.arms),
            fused_score=round(candidate.fused, 8),
            rerank_score=round(candidate.rerank_score, 6) if candidate.rerank_score is not None else None,
            band=candidate.band,
            signals=tuple(RankingSignal(name=n, value=v, effect=e) for n, v, e in candidate.signals),
            diversification=tuple(candidate.diversification),
        )

    @staticmethod
    def _warnings(hit: Hit, complete: bool) -> tuple[str, ...]:
        found = []
        if not complete:
            found.append("excerpt: the unit is longer than the bytes left; read it whole from its anchor.")
        if hit.unit == "document":
            found.append("document unit: evidence of which instrument is relevant, never of a provision.")
        if hit.replaced:
            found.append("A later published text of this record replaces this one.")
        return tuple(found)


def inner(nodes, hit: Hit) -> list[str]:
    """The locators of the parts inside a unit's span (its own excluded)."""
    return [
        n.locator
        for n in nodes
        if hit.start_byte <= n.start_byte and n.end_byte <= hit.end_byte and n.locator != hit.locator
    ]
