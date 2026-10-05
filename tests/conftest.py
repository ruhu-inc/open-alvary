from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.store import Store, metadata
from schema.models import Corpus, RightsRecord, SourceVersion, StructureNode

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def catalogue():
    return Corpus.model_validate_json(Path("ingestion/catalogue/ng.json").read_text())


@pytest.fixture
def cleared(catalogue):
    corpus = catalogue.model_copy(deep=True)
    text = "Synthetic fixture — not legislation.\nSection 1. Test only."
    digest = sha256(text.encode()).hexdigest()
    source_id = corpus.sources[0].id
    version = SourceVersion(
        id="test-version",
        source_id=source_id,
        canonical_url="https://example.org/synthetic",
        retrieved_at=NOW,
        raw_hash=digest,
        content_hash=digest,
        canonical_text=text,
        parser="fixture",
        verification_status="verified",
        content_verifier="test-content-reviewer",
        content_verified_at=NOW,
    )
    corpus.versions.append(version)
    corpus.structure.append(
        StructureNode(
            id="test-node",
            source_id=source_id,
            version_id=version.id,
            kind="section",
            locator="section/1",
            order=0,
            start_byte=0,
            end_byte=len(text.encode()),
            source_hash=digest,
        )
    )
    corpus.rights[0] = RightsRecord(
        id=corpus.rights[0].id,
        source_id=source_id,
        status="OPEN",
        rights_holder="Synthetic test author",
        licence="TEST-ONLY-NOT-A-LEGAL-LICENCE",
        redistribution_allowed="yes",
        commercial_reuse_allowed="yes",
        derivatives_allowed="yes",
        attribution_required=True,
        attribution_text="Synthetic test author",
        privacy_status="cleared",
        reviewer="test-rights-reviewer",
        reviewed_at=NOW,
        evidence=["https://example.org/test-permission"],
        legal_note="Synthetic fixture only",
        database_rights_notes="Synthetic fixture only",
        approved_content_hashes=[digest],
        metadata_public=True,
        metadata_basis="Synthetic test metadata",
    )
    return Corpus.model_validate(corpus.model_dump())


@pytest.fixture
def store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    yield Store(engine)
    engine.dispose()
