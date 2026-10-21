"""Fusion, documented ranking signals and diversification (RSV-05, ranking profile oa-rank-3).

Each arm's hits collapse to evidence units (a provision, a run of a container's own words, or a
document unit) at their best rank in that arm; reciprocal-rank fusion (k=60, RET-02) combines the arms.
When a reranker ran (retrieval.rerank), its scores (0 to 1) order the candidates it scored, ahead of the
rest in fused order. oa-rank-3 differs from oa-rank-2 in four ways:
- a long unit is reranked by its best passage;
- a weak match (found only by meaning or by some of the query's words) is evidence only with the
  reranker's support (rerank.SUPPORT_FLOOR);
- the dense arm does not run when a citation resolves;
- the link and page routes lead, with the citation's.

Then, in order:

1. Legal status: a record repealed, superseded or historical, or a text a later published text
   replaces, scores 0.8 of its fused score (as oa-rank-1).
2. Route match: what a route resolved exactly comes first: the part or text a link addresses, the
   citation's part, the provisions on a page asked for, then the definition asked for (RSV-03), as
   /search/text puts a cited part first. These never move in step 4.
3. Instrument match: when the query names instruments, their units come next.
4. Bands: the other positions fall into bands of BAND_WIDTH. Within a band, legislation is reordered
   among its own positions by instrument (constitution, Act, subsidiary legislation, guidance), and
   decisions among theirs by their court's relation to the queried jurisdiction (binding, persuasive,
   then not applicable or unknown), so neither displaces the other. A unit never moves out of its band.
5. Diversification: a unit overlapping one already kept from the same text collapses into it; the
   same locator in another text of the same work and language collapses into the first (``also_in``);
   unless the query is scoped to one instrument or citation, units beyond the first PER_WORK of one
   text move behind every other text's units (deferred, never dropped); a comparative query interleaves
   jurisdictions in rank order.

Ties go to the version ID, then the unit's first byte. Every unit records each step that touched it.
"""

from collections import defaultdict
from dataclasses import dataclass, field

PROFILE = "oa-rank-3"
RRF_K = 60
BAND_WIDTH = 5
PER_WORK = 3
NOT_IN_FORCE = 0.8
NOT_IN_FORCE_STATUSES = frozenset({"repealed", "superseded", "historical"})
# The instrument hierarchy shared by the jurisdiction profiles assessed so far (0 is highest). A
# judgment's authority is its court's (CPR-04), not a document type's.
INSTRUMENT_RANK = {"constitution": 0, "act": 1, "regulation": 2, "guidance": 3, "gazette": 4, "other": 5}
# A decision's court relation to the queried jurisdiction (CPR-04 court register): binding first.
COURT_ORDER = {"binding": 0, "persuasive": 1, "not_applicable": 2, "unknown": 2}


@dataclass
class Hit:
    """One arm's hit, as the section index (or a document unit, or a resolved citation) gives it."""

    version_id: str
    start_byte: int
    end_byte: int
    work_id: str
    node_id: str | None
    unit: str  # provision, container_run, document
    kind: str
    locator: str
    pinpoint: str | None
    heading: str | None
    language: str
    config: str
    content_hash: str
    legal_status: str
    replaced: bool
    jurisdiction_id: str
    document_type: str
    features: dict[str, float] = field(default_factory=dict)  # the engine's ranking features for this hit


@dataclass
class Candidate:
    hit: Hit
    arms: dict[str, int] = field(default_factory=dict)
    fused: float = 0.0
    rerank_score: float | None = None  # 0 to 1 when the reranker scored it
    court_relation: str | None = None  # for a decision: binding, persuasive, not_applicable or unknown
    court: object | None = None  # for a decision: its court as its register gives it (EvidenceCourt)
    expanded_from: str | None = None  # the seed unit whose reference added it (RSV-06)
    features: dict[str, dict[str, float]] = field(default_factory=dict)  # arm → its features
    band: int = 0
    signals: list[tuple[str, str, str]] = field(default_factory=list)
    diversification: list[str] = field(default_factory=list)
    also_in: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (self.hit.version_id, self.hit.node_id or "", self.hit.unit)

    @property
    def order(self) -> tuple:
        """Reranked candidates first, by score; then the rest by fused score; ties by version and byte."""
        if self.rerank_score is not None:
            return (0, -round(self.rerank_score, 12), self.hit.version_id, self.hit.start_byte)
        return (1, -round(self.fused, 12), self.hit.version_id, self.hit.start_byte)


def fuse(arms: dict[str, list[Hit]]) -> list[Candidate]:
    """Collapse each arm to units (best rank kept, ranks compacted), then reciprocal-rank fusion."""
    found: dict[tuple, Candidate] = {}
    for arm, hits in arms.items():
        rank = 0
        seen: set[tuple] = set()
        for hit in hits:
            candidate = Candidate(hit)
            if candidate.key in seen:
                continue
            seen.add(candidate.key)
            rank += 1
            held = found.setdefault(candidate.key, candidate)
            held.arms[arm] = rank
            if hit.features:
                held.features[arm] = hit.features
            held.fused += 1.0 / (RRF_K + rank)
    return sorted(found.values(), key=lambda c: c.order)


LEADING_ARMS = ("link", "citation", "page", "definition")  # what the query names, in its own order


def apply_signals(candidates: list[Candidate], named_works: set[str]) -> list[Candidate]:
    for candidate in candidates:
        hit = candidate.hit
        if hit.legal_status in NOT_IN_FORCE_STATUSES:
            candidate.fused *= NOT_IN_FORCE
            if candidate.rerank_score is not None:
                candidate.rerank_score *= NOT_IN_FORCE
            candidate.signals.append(("legal_status", hit.legal_status, f"x{NOT_IN_FORCE}"))
        elif hit.replaced:
            candidate.fused *= NOT_IN_FORCE
            if candidate.rerank_score is not None:
                candidate.rerank_score *= NOT_IN_FORCE
            candidate.signals.append(
                ("replaced", "a later text of this record replaces it", f"x{NOT_IN_FORCE}")
            )
    ordered = sorted(candidates, key=lambda c: c.order)

    def lead(candidate):
        arm = next(a for a in LEADING_ARMS if a in candidate.arms)
        return (LEADING_ARMS.index(arm), candidate.arms[arm], candidate.order)

    leading = sorted((c for c in ordered if any(a in c.arms for a in LEADING_ARMS)), key=lead)
    for candidate in leading:
        arm = next(a for a in LEADING_ARMS if a in candidate.arms)
        candidate.signals.append(("route_match", arm, "first"))
    ordered = [c for c in ordered if not any(a in c.arms for a in LEADING_ARMS)]
    if named_works:
        first = [c for c in ordered if c.hit.work_id in named_works]
        for candidate in first:
            candidate.signals.append(("instrument_match", candidate.hit.work_id, "first"))
        ordered = first + [c for c in ordered if c.hit.work_id not in named_works]
    banded: list[Candidate] = []
    for start in range(0, len(ordered), BAND_WIDTH):
        band = ordered[start : start + BAND_WIDTH]
        band = _reorder(
            band, lambda c: c.hit.document_type != "judgment", _instrument, "instrument_authority"
        )
        band = _reorder(band, lambda c: c.hit.document_type == "judgment", _court, "court_authority")
        for candidate in band:
            candidate.band = start // BAND_WIDTH
        banded += band
    return leading + banded


def _instrument(candidate: Candidate) -> int:
    return INSTRUMENT_RANK.get(candidate.hit.document_type, len(INSTRUMENT_RANK))


def _court(candidate: Candidate) -> int:
    return COURT_ORDER.get(candidate.court_relation or "unknown", len(COURT_ORDER))


def _reorder(band: list[Candidate], member, key, signal: str) -> list[Candidate]:
    """Within a band, reorder only the members (legislation, or decisions) among their own positions by
    ``key``, so legislation and decisions never displace each other; each moved member records it."""
    places = [n for n, c in enumerate(band) if member(c)]
    members = [band[n] for n in places]
    if len({key(c) for c in members}) <= 1:
        return band
    ordered = sorted(members, key=key)
    out = list(band)
    for place, candidate in zip(places, ordered, strict=True):
        out[place] = candidate
        value = candidate.hit.document_type if signal == "instrument_authority" else candidate.court_relation
        candidate.signals.append((signal, value or "unknown", "tie-break in band"))
    return out


def diversify(candidates: list[Candidate], *, scoped: bool, comparative: bool) -> list[Candidate]:
    kept: list[Candidate] = []
    first_of: dict[tuple, Candidate] = {}
    for candidate in candidates:
        hit = candidate.hit
        holder = next(
            (
                k
                for k in kept
                if k.hit.version_id == hit.version_id
                and k.hit.start_byte < hit.end_byte
                and hit.start_byte < k.hit.end_byte
            ),
            None,
        )
        if holder is not None:
            holder.diversification.append(f"overlapping {hit.locator} collapsed into it")
            continue
        same = (hit.work_id, hit.language, hit.locator, hit.unit)
        if same in first_of and first_of[same].hit.version_id != hit.version_id:
            first_of[same].also_in.append(hit.version_id)
            first_of[same].diversification.append(f"collapsed {hit.version_id}")
            continue
        first_of.setdefault(same, candidate)
        kept.append(candidate)
    if not scoped:
        per_text: dict[str, int] = defaultdict(int)
        first, deferred = [], []
        for candidate in kept:
            per_text[candidate.hit.version_id] += 1
            if per_text[candidate.hit.version_id] > PER_WORK:
                candidate.diversification.append(f"deferred: more than {PER_WORK} units of this text")
                deferred.append(candidate)
            else:
                first.append(candidate)
        kept = first + deferred  # deferred behind other texts' units, never dropped
    if comparative:
        queues: dict[str, list[Candidate]] = defaultdict(list)
        order: list[str] = []
        for candidate in kept:
            if candidate.hit.jurisdiction_id not in queues:
                order.append(candidate.hit.jurisdiction_id)
            queues[candidate.hit.jurisdiction_id].append(candidate)
        interleaved = []
        while any(queues.values()):
            for jurisdiction in order:
                if queues[jurisdiction]:
                    candidate = queues[jurisdiction].pop(0)
                    candidate.diversification.append("jurisdictions interleaved")
                    interleaved.append(candidate)
        kept = interleaved
    return kept
