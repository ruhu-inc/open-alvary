"""/coverage counted by the database (migration 0045) gives what reading every record through the rights
policy gives, whatever a record's rights or versions say, at any time of asking; it reads no payload, and a
withdrawal or an expiry takes effect on the next answer (F01)."""

import random
from datetime import datetime, timedelta
from hashlib import sha256

import pytest
from alembic import command
from alembic.config import Config
from conftest import NOW
from sqlalchemy import event, text
from test_postgres import pg_engine as pg_engine

from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository
from api.store import Store
from rights.policy import decide
from schema.models import RightsRecord, SourceVersion

pytestmark = pytest.mark.postgres

ASKED = NOW + timedelta(days=10)
EARLIER, LATER = ASKED - timedelta(days=5), ASKED + timedelta(days=5)
# One change each to a record that may otherwise be read: everything decide asks of its rights.
RIGHTS_CHANGES = [
    {},
    *({"status": status} for status in ("LINK_ONLY", "RESTRICTED", "RIGHTS_UNDER_REVIEW")),
    {"status": "OPEN_WITH_CONDITIONS"},
    {"status": "OPEN_WITH_CONDITIONS", "conditions": ["Attribute the ministry"]},
    {"status": "OPEN_WITH_CONDITIONS", "conditions": ["Attribute the ministry"], "conditions_satisfied": True},
    {"redistribution_allowed": "conditional"},
    {"redistribution_allowed": "conditional", "conditions": ["Attribute"], "conditions_satisfied": True},
    {"redistribution_allowed": "conditional", "conditions_satisfied": True},
    {"conditions": ["Attribute the ministry"]},
    {"conditions": ["Attribute the ministry"], "conditions_satisfied": True},
    {"conditions_satisfied": True},
    *({"redistribution_allowed": value} for value in ("no", "unknown")),
    *({"commercial_reuse_allowed": value} for value in ("no", "conditional", "unknown")),
    *({"derivatives_allowed": value} for value in ("no", "conditional", "unknown")),
    *({name: None} for name in ("reviewer", "licence", "rights_holder", "legal_note", "database_rights_notes")),
    *({name: ""} for name in ("licence", "rights_holder", "legal_note", "database_rights_notes")),
    {"reviewed_at": None},
    {"reviewed_at": LATER},
    {"reviewed_at": ASKED},
    {"reviewed_at": datetime.fromisoformat("2026-01-05T09:30:00+01:00")},
    {"evidence": []},
    {"evidence": ["https://example.org/a", "https://example.org/b"]},
    {"expires_at": EARLIER},
    {"expires_at": ASKED},
    {"expires_at": LATER},
    *({"privacy_status": value} for value in ("pending", "redacted", "blocked")),
    {"attribution_required": None},
    {"attribution_required": False, "attribution_text": None},
    {"attribution_required": True, "attribution_text": None},
    {"attribution_required": True, "attribution_text": ""},
    {"metadata_public": False},
    {"metadata_basis": None},
]
# And to its version.
VERSION_CHANGES = [
    {"verification_status": "pending"},
    {"verification_status": "rejected"},
    {"content_verifier": None},
    {"content_verified_at": None},
    {"content_verified_at": LATER},
    {"content_verified_at": ASKED},
    {"content_verified_at": datetime.fromisoformat("2026-01-05T09:30:00-05:00")},
]


def version_of(source_id: str, words: str, **changes) -> SourceVersion:
    digest = sha256(words.encode()).hexdigest()
    return SourceVersion(
        **{
            "id": f"v-{digest[:12]}",
            "source_id": source_id,
            "canonical_url": "https://example.org/text",
            "retrieved_at": NOW,
            "raw_hash": digest,
            "content_hash": digest,
            "canonical_text": words,
            "parser": "fixture",
            "verification_status": "verified",
            "content_verifier": "content-reviewer",
            "content_verified_at": NOW,
            **changes,
        }
    )


def add_record(corpus, template, rights_changes=(), version_changes=(), approve=True, extra_texts=0):
    """A record that may be read, but for the changes given."""
    index = len(corpus.sources)
    source = template.model_copy(update={"id": f"record-{index}"})
    words = f"Text of record {index}.\n"
    versions = [version_of(source.id, words, **dict(version_changes))]
    versions += [version_of(source.id, f"{words}Edition {n}.\n") for n in range(extra_texts)]
    rights = RightsRecord.model_validate(
        {
            **corpus.rights[0].model_dump(mode="json"),
            "id": f"rights-{source.id}",
            "source_id": source.id,
            "approved_content_hashes": [v.content_hash for v in versions] if approve else [],
            **dict(rights_changes),
        }
    )
    corpus.sources.append(source)
    corpus.rights.append(rights)
    corpus.versions.extend(versions)
    return source, rights, versions


def counted(engine, now=ASKED):
    """What the database counts and what reading each record through the policy gives, side by side."""
    with engine.begin() as conn:
        repository = LegacyRepository(conn)
        service = LegacyCatalogue(repository)
        service.now = now
        assert repository._gated()
        view = service.coverage()
        gated_counts = repository.public_counts()
        repository._has_gates = False
        read = service.coverage()
        assert repository.public_counts() == gated_counts
        assert list(repository.public_counts()) == list(gated_counts)  # in the same order too
    assert view == read
    return view


def expected(corpus, now=ASKED):
    """And decide's answer on the corpus as written, so the walk above is checked as well."""
    rights = {r.source_id: r for r in corpus.rights}
    public = [s for s in corpus.sources if decide(rights[s.id], now=now).metadata]
    readable = [v for v in corpus.versions if decide(rights[v.source_id], v, now=now).full_text]
    return len(public), len({v.source_id for v in readable}), len(readable)


def numbers(view):
    return view.catalogued_sources, view.open_full_text_sources, view.published_versions


@pytest.fixture
def corpus(cleared):
    corpus = cleared.model_copy(deep=True)
    corpus.structure, corpus.citations, corpus.amendments = [], [], []
    return corpus


def test_the_database_counts_what_the_policy_allows_for_each_thing_it_asks(corpus, pg_engine):
    template = corpus.sources[0]
    for changes in RIGHTS_CHANGES:
        add_record(corpus, template, changes.items())
    for changes in VERSION_CHANGES:
        add_record(corpus, template, (), changes.items())
    add_record(corpus, template, approve=False)
    add_record(corpus, template, extra_texts=2)
    _, rights, versions = add_record(corpus, template, extra_texts=1)
    rights.approved_content_hashes.remove(versions[1].content_hash)  # one of its two approved
    Store(pg_engine).save(corpus)
    for now in (ASKED, EARLIER, LATER, NOW, NOW - timedelta(days=1)):
        assert numbers(counted(pg_engine, now)) == expected(corpus, now), now
    catalogued, open_sources, published = expected(corpus)
    assert open_sources < catalogued and published > open_sources  # the fixture does tell them apart


def test_the_same_when_several_things_are_wrong_at_once(corpus, pg_engine):
    template = corpus.sources[0]
    rng = random.Random(45)
    for _ in range(120):
        rights_changes = [i for c in rng.sample(RIGHTS_CHANGES, rng.randint(0, 3)) for i in c.items()]
        version_changes = [i for c in rng.sample(VERSION_CHANGES, rng.randint(0, 1)) for i in c.items()]
        add_record(corpus, template, rights_changes, version_changes, extra_texts=rng.randint(0, 2))
    Store(pg_engine).save(corpus)
    assert numbers(counted(pg_engine)) == expected(corpus)


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("sources", "id"),
        ("sources", "jurisdiction_id"),
        ("sources", "authority_id"),
        ("sources", "language"),
        ("rights", "id"),
        ("rights", "source_id"),
        ("versions", "id"),
        ("versions", "source_id"),
        ("versions", "language"),
    ],
)
def test_a_payload_that_disagrees_with_its_row_is_not_counted(corpus, pg_engine, table, field):
    source, _, _ = add_record(corpus, corpus.sources[0], extra_texts=1)
    Store(pg_engine).save(corpus)
    catalogued, open_sources, published = expected(corpus)
    where = {"sources": "id", "rights": "source_id", "versions": "source_id"}[table]
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {table} SET payload = (payload::jsonb || jsonb_build_object('{field}', 'other'))::json"
                f" WHERE {where} = :source"
            ),
            {"source": source.id},
        )
    assert numbers(counted(pg_engine)) == (
        catalogued - (table != "versions"),  # the record itself stands if only its texts disagree
        open_sources - 1,
        published - 2,
    )


def test_coverage_reads_no_payload_and_keeps_nothing(corpus, pg_engine):
    add_record(corpus, corpus.sources[0], extra_texts=1)
    Store(pg_engine).save(corpus)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", capture)
    try:
        with pg_engine.begin() as conn:
            service = LegacyCatalogue(LegacyRepository(conn))
            service.now = ASKED
            first = service.coverage()
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture)
    assert numbers(first) == expected(corpus)
    assert statements and not any("payload" in statement for statement in statements)


def test_withdrawing_a_record_or_its_review_expiring_takes_effect_on_the_next_answer(corpus, pg_engine):
    _, rights, _ = add_record(corpus, corpus.sources[0], (("expires_at", LATER),))
    store = Store(pg_engine)
    store.save(corpus)
    catalogued, open_sources, published = expected(corpus)
    assert numbers(counted(pg_engine)) == (catalogued, open_sources, published)
    # The review ran out; nothing was written.
    assert numbers(counted(pg_engine, LATER)) == (catalogued, open_sources - 1, published - 1)
    rights.metadata_public = False
    store.save(corpus)
    assert numbers(counted(pg_engine)) == (catalogued - 1, open_sources - 1, published - 1)


def test_the_answers_are_the_same_without_the_gates_and_after_they_return(corpus, pg_engine):
    add_record(corpus, corpus.sources[0], extra_texts=1)
    Store(pg_engine).save(corpus)
    before = numbers(counted(pg_engine))
    config = Config("alembic.ini")
    with pg_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0044")
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        assert not repository._gated()
        service = LegacyCatalogue(repository)
        service.now = ASKED
        assert numbers(service.coverage()) == before
    with pg_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert numbers(counted(pg_engine)) == before
