"""Fail-closed public projection shared by HTTP and release generation."""

from datetime import UTC, datetime
from typing import Protocol

from schema.models import Corpus, Model, Permission, RightsRecord, RightsStatus


class PublicationVersion(Protocol):
    source_id: str
    content_hash: str
    verification_status: str
    content_verifier: str | None
    content_verified_at: datetime | None


class Decision(Model):
    metadata: bool
    full_text: bool
    reasons: list[str]


def decide(
    record: RightsRecord | None, version: PublicationVersion | None = None, *, now: datetime | None = None
) -> Decision:
    now = now or datetime.now(UTC)
    if record is None or record.status == RightsStatus.RESTRICTED:
        return Decision(metadata=False, full_text=False, reasons=["Missing or restricted rights record"])
    metadata = record.metadata_public and bool(record.metadata_basis)
    reasons = []
    if not metadata:
        reasons.append("Metadata publication has no recorded basis")
    if record.status not in {RightsStatus.OPEN, RightsStatus.OPEN_WITH_CONDITIONS}:
        reasons.append("Full-text redistribution is not approved")
    if record.redistribution_allowed not in {Permission.YES, Permission.CONDITIONAL}:
        reasons.append("Redistribution permission is missing")
    # The shared open corpus must remain usable by any downstream client.
    if record.commercial_reuse_allowed != Permission.YES or record.derivatives_allowed != Permission.YES:
        reasons.append("Commercial reuse and derivatives require explicit permission")
    if not all(
        [
            record.reviewer,
            record.reviewed_at,
            record.evidence,
            record.licence,
            record.rights_holder,
            record.legal_note,
            record.database_rights_notes,
        ]
    ):
        reasons.append("Rights evidence/reviewer/licence record is incomplete")
    if record.reviewed_at and record.reviewed_at > now:
        reasons.append("Review date is in the future")
    if record.expires_at and record.expires_at <= now:
        reasons.append("Rights review has expired")
    if record.privacy_status not in {"cleared", "redacted"}:
        reasons.append("Privacy review is incomplete")
    if record.attribution_required is None or (record.attribution_required and not record.attribution_text):
        reasons.append("Attribution obligations are unresolved")
    conditional = (
        record.status == RightsStatus.OPEN_WITH_CONDITIONS
        or record.redistribution_allowed == Permission.CONDITIONAL
    )
    if conditional and (not record.conditions or not record.conditions_satisfied):
        reasons.append("Release conditions are unresolved")
    if record.conditions and not record.conditions_satisfied:
        reasons.append("Recorded conditions have not been satisfied")
    if version is None or version.source_id != record.source_id:
        reasons.append("No matching version")
    else:
        if version.content_hash not in record.approved_content_hashes:
            reasons.append("This exact content hash has not been approved")
        if (
            version.verification_status != "verified"
            or not version.content_verifier
            or not version.content_verified_at
            or version.content_verified_at > now
        ):
            reasons.append("Independent content verification is incomplete")
    return Decision(metadata=metadata, full_text=not reasons, reasons=reasons)


def public_corpus(corpus: Corpus, *, metadata_only: bool = False) -> Corpus:
    # Revalidate mutable inputs before crossing a publication boundary.
    corpus = Corpus.model_validate(corpus.model_dump(mode="json"))
    rights = {r.source_id: r for r in corpus.rights}
    source_ids = {s.id for s in corpus.sources if decide(rights.get(s.id)).metadata}
    versions = [
        v
        for v in corpus.versions
        if not metadata_only and v.source_id in source_ids and decide(rights.get(v.source_id), v).full_text
    ]
    version_ids = {v.id for v in versions}
    nodes = [n for n in corpus.structure if n.version_id in version_ids]
    node_ids = {n.id for n in nodes}
    sources = [s for s in corpus.sources if s.id in source_ids]
    return Corpus(
        jurisdictions=corpus.jurisdictions,
        authorities=[a for a in corpus.authorities if a.id in {s.authority_id for s in sources}],
        languages=corpus.languages,
        sources=sources,
        versions=versions,
        structure=nodes,
        rights=[r for r in corpus.rights if r.source_id in source_ids],
        citations=[
            c
            for c in corpus.citations
            if c.source_id in source_ids
            and (not c.version_id or c.version_id in version_ids)
            and (not c.node_id or c.node_id in node_ids)
        ],
        amendments=[
            a
            for a in corpus.amendments
            if a.verified and a.source_id in source_ids and a.target_source_id in source_ids
        ],
        translations=[
            t
            for t in corpus.translations
            if t.source_version_id in version_ids and t.translated_version_id in version_ids
        ],
    )
