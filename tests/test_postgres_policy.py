"""Real migrations and synthetic, non-superuser capability subjects; no real approvals."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.identity import backfill
from api.store import Store, seed
from rights.database import PolicyNamespace, PolicyRepository, provision_roles
from schema.identity import mint
from schema.models import SourceVersion
from schema.policy import CollectionDecision

pytestmark = pytest.mark.postgres
MATERIALS = ["operative_tables", "amounts", "dates", "negations", "cross_references", "identity_citation"]
RAW = b"Original M1.4 synthetic private fixture; not legislation."


@dataclass
class Foundation:
    engine: object
    ns: PolicyNamespace
    roles: dict
    actors: dict
    collection: str
    other_collection: str
    work: str
    evidence: object
    now: object

    @contextmanager
    def as_role(self, kind):
        with self.engine.begin() as conn:
            role = conn.dialect.identifier_preparer.quote_identifier(self.roles[kind])
            # Session-level identity matters: SET ROLE from a superuser session can
            # always switch again, so it cannot prove a runtime subject cannot escalate.
            conn.execute(text(f"SET LOCAL SESSION AUTHORIZATION {role}"))
            yield conn

    def name(self, conn, kind, table):
        return self.ns.qualified(conn, kind, table)

    def record(self, table, *, role="review", **fields):
        values = dict(
            revision=1,
            collection_id=self.collection,
            actor_id=self.actors[role],
            evidence_id=self.evidence,
            valid_from=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(days=1),
            state="approved",
            reason="Synthetic test decision",
        )
        values.update(fields)
        with self.as_role(role) as conn:
            columns = ",".join(values)
            params = ",".join(":" + key for key in values)
            conn.execute(
                text(f"INSERT INTO {self.name(conn, 'policy', table)} ({columns}) VALUES ({params})"), values
            )
        return values

    def decision(self, **fields):
        defaults = dict(
            basis_type="original_authorship",
            conditions_satisfied=True,
            metadata_privacy_class="no_personal_data",
            privacy_reason="Synthetic metadata has no personal data",
        )
        defaults.update(fields)
        return self.record("collection_decision", **defaults)

    def assessment(self, **fields):
        fields.setdefault("id", uuid4())
        fields.setdefault("privacy_class", "no_personal_data")
        defaults = dict(
            acquire="allow",
            retain="allow",
            classification_reason="Original synthetic fixture",
            purpose="local_test",
            retention_deadline=self.now + timedelta(hours=1),
        )
        defaults.update(fields)
        return self.record("acquisition_assessment", **defaults)

    def stage(self, assessment, **fields):
        values = dict(
            id=uuid4(),
            collection_id=self.collection,
            assessment_id=assessment["id"],
            assessment_revision=assessment["revision"],
            artifact_hash=sha256(RAW).hexdigest(),
            size_bytes=len(RAW),
            private_bytes=RAW,
            retention_deadline=self.now + timedelta(minutes=30),
        )
        values.update(fields)
        with self.as_role("acquisition") as conn:
            columns = ",".join(values)
            params = ",".join(":" + key for key in values)
            conn.execute(
                text(
                    f"INSERT INTO {self.name(conn, 'staging', 'staged_artifact')} ({columns}) VALUES ({params})"
                ),
                values,
            )
        return values


@pytest.fixture
def foundation(pg_engine):
    migrate(pg_engine)
    seed(Store(pg_engine))
    roles = {}
    with pg_engine.begin() as conn:
        backfill(conn, expected_epoch=0)
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        roles = provision_roles(conn, ns)
        roles["content"] = "content_" + uuid4().hex
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.execute(text(f"CREATE ROLE {quote(roles['content'])} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        conn.execute(text(f"GRANT {quote(roles['review'])} TO {quote(roles['content'])}"))
        actors = {kind: uuid4() for kind in ("review", "content", "acquisition", "release")}
        for kind, actor in actors.items():
            conn.execute(
                text(f"""
                INSERT INTO {ns.qualified(conn, "policy", "actor")}(id,database_role,identity_kind,active)
                SELECT :id,oid,'synthetic',true FROM pg_roles WHERE rolname=:role
            """),
                {"id": actor, "role": roles[kind]},
            )
        collection, other_collection = mint("scp"), mint("scp")
        for key, provider in ((collection, "synthetic-one"), (other_collection, "synthetic-two")):
            conn.execute(
                text("""
                INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key)
                VALUES (:id,'ng','act',:provider)
            """),
                {"id": key, "provider": provider},
            )
        work = conn.scalar(
            text("SELECT id FROM identity_works WHERE document_class='act' ORDER BY id LIMIT 1")
        )
        conn.execute(
            text("UPDATE identity_works SET collection_id=:c,collection_unknown_reason=NULL WHERE id=:id"),
            {"c": collection, "id": work},
        )
        for kind, cap in (
            ("review", "rights"),
            ("content", "content"),
            ("acquisition", "acquire"),
            ("release", "release"),
        ):
            conn.execute(
                text(
                    f"INSERT INTO {ns.qualified(conn, 'policy', 'assignment')} VALUES (:actor,:collection,:cap)"
                ),
                {"actor": actors[kind], "collection": collection, "cap": cap},
            )
        evidence = uuid4()
        now = conn.scalar(text("SELECT statement_timestamp()"))
        conn.execute(
            text(f"""
            INSERT INTO {ns.qualified(conn, "policy", "evidence")}
                VALUES (:id,:c,'private://synthetic-evidence',:hash,:now)
        """),
            {"id": evidence, "c": collection, "hash": "e" * 64, "now": now},
        )
    try:
        yield Foundation(pg_engine, ns, roles, actors, collection, other_collection, work, evidence, now)
    finally:
        with pg_engine.begin() as conn:
            administrator = conn.scalar(text("SELECT session_user"))
            quote = conn.dialect.identifier_preparer.quote_identifier
            for schema in (ns.schema("policy"), ns.schema("staging"), ns.schema("corpus")):
                conn.execute(text(f"DROP SCHEMA {quote(schema)} CASCADE"))
            for role in roles.values():
                conn.execute(text(f"DROP OWNED BY {quote(role)}"))
                conn.execute(text(f"REVOKE {quote(role)} FROM {quote(administrator)}"))
                conn.execute(text(f"DROP ROLE {quote(role)}"))


def test_default_missing_unknown_and_expired_decisions_deny(foundation):
    f = foundation
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed
    f.decision()  # Vector defaults remain unknown, including metadata.
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed
    f.decision(revision=2, redistribute_metadata="allow", expires_at=f.now - timedelta(minutes=1))
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def test_serving_view_contains_only_eligible_safe_metadata(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with f.as_role("release") as conn:
        conn.execute(
            text(f"""
            INSERT INTO {f.name(conn, "corpus", "metadata_projection")}
                VALUES (:work,:collection,'Original synthetic title','Synthetic citation','https://example.org/synthetic',false)
        """),
            {"work": f.work, "collection": f.collection},
        )
    with f.as_role("serving") as conn:
        repo = PolicyRepository(conn, f.ns)
        result = repo.metadata(f.work)
        assert set(result.model_dump()) == {"work_id", "title", "citation", "reference_url"}
        assert result.title == "Original synthetic title"
        assert not repo.public_decision(f.collection, "redistribute_text").allowed
    f.decision(revision=2, redistribute_metadata="deny")
    with f.as_role("serving") as conn:
        assert PolicyRepository(conn, f.ns).metadata(f.work) is None


@pytest.mark.parametrize(
    "kind,table",
    [
        ("policy", "evidence"),
        ("policy", "controller_record"),
        ("policy", "acquisition_assessment"),
        ("policy", "actor"),
        ("policy", "audit_event"),
        ("staging", "staged_artifact"),
        ("corpus", "metadata_projection"),
    ],
)
def test_serving_cannot_read_private_tables(foundation, kind, table):
    f = foundation
    with pytest.raises(DBAPIError) as denied, f.as_role("serving") as conn:
        conn.execute(text(f"SELECT * FROM {f.name(conn, kind, table)}"))
    assert denied.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("role", ["serving", "acquisition", "release"])
def test_nonreviewers_cannot_mutate_policy(foundation, role):
    f = foundation
    with pytest.raises(DBAPIError) as denied, f.as_role(role) as conn:
        conn.execute(text(f"INSERT INTO {f.name(conn, 'policy', 'collection_decision')} DEFAULT VALUES"))
    assert denied.value.orig.sqlstate == "42501"


def test_actor_is_bound_to_database_subject_not_supplied_identity(foundation):
    f = foundation
    with pytest.raises(DBAPIError) as denied:
        f.decision(redistribute_metadata="allow", actor_id=f.actors["content"])
    assert denied.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as denied:
        f.record(
            "collection_decision",
            role="content",
            basis_type="original_authorship",
            conditions_satisfied=True,
            metadata_privacy_class="no_personal_data",
            privacy_reason="Test",
            redistribute_metadata="allow",
        )
    assert denied.value.orig.sqlstate == "42501"
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 0
    with f.as_role("content") as conn:
        conn.execute(text(f'SET LOCAL ROLE "{f.roles["review"]}"'))
        assert conn.scalar(text(f"SELECT {f.name(conn, 'policy', 'current_actor')}()")) == f.actors["content"]
        assert not conn.scalar(
            text(f"SELECT {f.name(conn, 'policy', 'assigned')}(:cid,'rights')"), {"cid": f.collection}
        )


def test_review_scope_rls_hides_unassigned_evidence_and_rejects_wrong_scope(foundation):
    f = foundation
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {f.name(conn, 'policy', 'evidence')} VALUES (:id,:c,'private://other',:hash,:now)"
            ),
            {"id": uuid4(), "c": f.other_collection, "hash": "f" * 64, "now": f.now},
        )
    with f.as_role("review") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'evidence')}")) == 1
    with pytest.raises(DBAPIError) as denied:
        f.decision(collection_id=f.other_collection)
    assert denied.value.orig.sqlstate == "42501"


def test_revision_and_audit_are_atomic_and_append_only(foundation):
    f = foundation
    values = f.decision(redistribute_metadata="allow")
    with f.as_role("review") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1
    with pytest.raises(DBAPIError) as stale:
        f.decision(redistribute_metadata="deny")
    assert stale.value.orig.sqlstate == "40001"
    with f.as_role("review") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1
    for table in ("collection_decision", "audit_event", "evidence"):
        with pytest.raises(DBAPIError) as denied, f.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {f.name(conn, 'policy', table)}"))
        assert denied.value.orig.sqlstate == "55000"
    assert CollectionDecision.model_validate(values).revision == 1


def test_invalid_decision_rolls_back_revision_and_audit(foundation):
    f = foundation
    with pytest.raises(IntegrityError):
        f.decision(expires_at=f.now - timedelta(days=2))
    f.decision(redistribute_metadata="allow")
    with f.as_role("review") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1


def test_unreviewed_or_expired_acquisition_cannot_stage(foundation):
    f = foundation
    assessment = f.assessment(state="draft")
    with pytest.raises(DBAPIError):
        f.stage(assessment)
    expired = f.assessment(
        valid_from=f.now - timedelta(days=2),
        expires_at=f.now - timedelta(hours=1),
        retention_deadline=f.now - timedelta(hours=2),
    )
    with pytest.raises(DBAPIError):
        f.stage(expired)
    missing = {"id": uuid4(), "revision": 1}
    with pytest.raises(DBAPIError):
        f.stage(missing)


def test_private_staging_is_independent_of_public_text_permission(foundation):
    f = foundation
    assessment = f.assessment()
    f.stage(assessment)
    with f.as_role("acquisition") as conn:
        row = conn.execute(
            text(f"SELECT private_bytes FROM {f.name(conn, 'staging', 'staged_artifact')}")
        ).one()
        assert bytes(row[0]) == RAW
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def test_superseded_assessment_holds_reads_before_reconciliation_job(foundation):
    f = foundation
    assessment = f.assessment()
    f.stage(assessment)
    f.assessment(id=assessment["id"], revision=2, state="revoked")
    with f.as_role("acquisition") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0
    with pytest.raises(DBAPIError):
        f.stage(assessment)


def test_retention_and_immutable_staging_lineage(foundation):
    f = foundation
    assessment = f.assessment()
    with pytest.raises(DBAPIError):
        f.stage(assessment, retention_deadline=f.now + timedelta(hours=2))
    with pytest.raises(IntegrityError):
        f.stage(assessment, artifact_hash="a" * 64)
    staged = f.stage(assessment)
    with pytest.raises(DBAPIError) as denied, f.as_role("acquisition") as conn:
        conn.execute(
            text(
                f"UPDATE {f.name(conn, 'staging', 'staged_artifact')} SET assessment_revision=2 WHERE id=:id"
            ),
            {"id": staged["id"]},
        )
    assert denied.value.orig.sqlstate == "55000"
    with f.as_role("acquisition") as conn:
        conn.execute(
            text(f"SELECT {f.name(conn, 'policy', 'erase_staged')}(:id)"),
            {"id": staged["id"]},
        )
    with f.engine.connect() as conn:
        assert (
            conn.scalar(text(f"SELECT private_bytes FROM {f.name(conn, 'staging', 'staged_artifact')}"))
            is None
        )


def privacy_chain(f, **controller_fields):
    controller = f.record(
        "controller_record",
        id=uuid4(),
        legal_name="Synthetic Controller",
        postal_address="Synthetic test address",
        jurisdiction_id="ng",
        privacy_contact="private-test-contact",
        accountable_role="synthetic-reviewer",
        **controller_fields,
    )
    review = f.record(
        "privacy_review",
        id=uuid4(),
        controller_id=controller["id"],
        controller_revision=1,
        purpose="local_test",
        lawful_basis="Synthetic fixture",
        assessment_reference="private://privacy",
        clearance="cleared",
    )
    return controller, review


def test_personal_data_requires_exact_current_privacy_and_controller_revision(foundation):
    f = foundation
    with pytest.raises(IntegrityError):
        f.assessment(privacy_class="personal_data")
    controller, privacy = privacy_chain(f)
    assessment = f.assessment(
        privacy_class="personal_data", privacy_review_id=privacy["id"], privacy_review_revision=1
    )
    f.stage(assessment)
    f.record(
        "controller_record",
        id=controller["id"],
        revision=2,
        legal_name="Synthetic Controller",
        postal_address="Synthetic changed address",
        jurisdiction_id="ng",
        privacy_contact="private-test-contact",
        accountable_role="synthetic-reviewer",
    )
    with f.as_role("acquisition") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0


def test_expired_controller_denies_private_acquisition(foundation):
    f = foundation
    _, privacy = privacy_chain(f, valid_from=f.now - timedelta(days=2), expires_at=f.now - timedelta(hours=1))
    assessment = f.assessment(
        privacy_class="personal_data", privacy_review_id=privacy["id"], privacy_review_revision=1
    )
    with pytest.raises(DBAPIError):
        f.stage(assessment)


def test_material_coverage_and_independent_review_preflight(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with pytest.raises(IntegrityError):
        f.record(
            "verification_policy",
            role="content",
            sample_numerator=1,
            sample_denominator=1,
            escalation_rule="Synthetic all items",
            material_classes=["dates"],
        )
    f.record(
        "verification_policy",
        role="content",
        sample_numerator=1,
        sample_denominator=1,
        escalation_rule="Synthetic all items",
        material_classes=MATERIALS,
    )
    with pytest.raises(DBAPIError) as incomplete, f.as_role("release") as conn:
        PolicyRepository(conn, f.ns).lock_approval_inputs(f.collection, rights_revision=1, content_revision=1)
    assert incomplete.value.orig.sqlstate == "23514"
    with f.as_role("content") as conn:
        for material in MATERIALS:
            conn.execute(
                text(f"""
                INSERT INTO {f.name(conn, "policy", "verification_material")}
                VALUES (:cid,1,:material,'human_source_comparison',1,1,:evidence,'Review all failures','Exclude unresolved items')
            """),
                {"cid": f.collection, "material": material, "evidence": f.evidence},
            )
    with f.as_role("release") as conn:
        function = f.name(conn, "policy", "lock_approval_inputs")
        assert conn.scalar(text(f"SELECT {function}(:cid,1,1)"), {"cid": f.collection})
    with pytest.raises(DBAPIError) as stale, f.as_role("release") as conn:
        PolicyRepository(conn, f.ns).lock_collection(f.collection, expected_revision=2)
    assert stale.value.orig.sqlstate == "40001"
    with f.engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO {f.name(conn, 'policy', 'assignment')} VALUES (:actor,:cid,'content')"),
            {"actor": f.actors["review"], "cid": f.collection},
        )
    f.record(
        "verification_policy",
        role="review",
        revision=2,
        sample_numerator=1,
        sample_denominator=1,
        escalation_rule="Synthetic all items",
        material_classes=MATERIALS,
    )
    with pytest.raises(DBAPIError) as denied, f.as_role("release") as conn:
        assert conn.scalar(text(f"SELECT {function}(:cid,1,2)"), {"cid": f.collection})
    assert denied.value.orig.sqlstate == "42501"


def test_non_superuser_table_owner_cannot_bypass_forced_rls(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    owner = "owner_" + uuid4().hex
    with f.engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.execute(text(f"CREATE ROLE {quote(owner)} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA {quote(f.ns.schema('policy'))} TO {quote(owner)}"))
        conn.execute(
            text(
                f"GRANT EXECUTE ON FUNCTION {f.name(conn, 'policy', 'assigned')}(text,text) TO {quote(owner)}"
            )
        )
        conn.execute(
            text(f"ALTER TABLE {f.name(conn, 'policy', 'collection_decision')} OWNER TO {quote(owner)}")
        )
    try:
        with f.engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ROLE {quote(owner)}"))
            assert (
                conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'collection_decision')}"))
                == 0
            )
    finally:
        with f.engine.begin() as conn:
            administrator = quote(conn.scalar(text("SELECT session_user")))
            conn.execute(
                text(f"ALTER TABLE {f.name(conn, 'policy', 'collection_decision')} OWNER TO {administrator}")
            )
            conn.execute(text(f"DROP OWNED BY {quote(owner)}"))
            conn.execute(text(f"DROP ROLE {quote(owner)}"))


def test_capability_roles_cannot_become_guard_or_gain_schema_ownership(foundation):
    f = foundation
    for kind in ("serving", "acquisition", "review", "release"):
        with f.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rolsuper,rolbypassrls,rolcreaterole,rolcanlogin FROM pg_roles WHERE rolname=:role"
                ),
                {"role": f.roles[kind]},
            ).one()
            assert not any(row)
        with pytest.raises(DBAPIError) as denied, f.as_role(kind) as conn:
            conn.execute(text(f'SET LOCAL ROLE "{f.roles["guard"]}"'))
        assert denied.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    "fields",
    [
        {"conditions_satisfied": False},
        {"basis_type": "unknown"},
        {"state": "draft"},
        {"state": "revoked"},
    ],
)
def test_unresolved_or_revoked_decision_denies_metadata(foundation, fields):
    f = foundation
    f.decision(redistribute_metadata="allow", **fields)
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def test_privacy_rejection_stops_staged_read_and_scoped_erasure_is_idempotent(foundation):
    f = foundation
    controller, privacy = privacy_chain(f)
    assessment = f.assessment(
        privacy_class="personal_data", privacy_review_id=privacy["id"], privacy_review_revision=1
    )
    staged = f.stage(assessment)
    f.record(
        "privacy_review",
        id=privacy["id"],
        revision=2,
        controller_id=controller["id"],
        controller_revision=1,
        purpose="local_test",
        lawful_basis="Synthetic fixture",
        assessment_reference="private://rejected",
        clearance="rejected",
    )
    with f.as_role("acquisition") as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0
        for _ in range(2):
            assert conn.scalar(
                text(f"SELECT {f.name(conn, 'policy', 'erase_staged')}(:id)"), {"id": staged["id"]}
            )
    with f.as_role("review") as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_erasure'"
                )
            )
            == 1
        )


def test_version_override_cannot_widen_parent_decision_and_hash_is_pinned(foundation):
    f = foundation
    with f.engine.connect() as conn:
        source = conn.scalar(
            text("SELECT source_id FROM identity_legacy_sources WHERE work_id=:work"), {"work": f.work}
        )
    store = Store(f.engine)
    corpus = store.load()
    literal = "Original synthetic policy version; not law."
    hash = sha256(literal.encode()).hexdigest()
    corpus.versions.append(
        SourceVersion(
            id="synthetic-policy-version",
            source_id=source,
            canonical_url="https://example.org/synthetic",
            retrieved_at=f.now,
            raw_hash=hash,
            content_hash=hash,
            canonical_text=literal,
            parser="synthetic",
        )
    )
    store.save(corpus)
    with f.engine.begin() as conn:
        backfill(conn, expected_epoch=0)
        conn.execute(
            text(
                f"INSERT INTO {f.name(conn, 'policy', 'version_binding')} VALUES (:version,:source,:work,:cid,:hash)"
            ),
            {
                "version": "synthetic-policy-version",
                "source": source,
                "work": f.work,
                "cid": f.collection,
                "hash": hash,
            },
        )
    f.decision(redistribute_text="deny")
    f.record("version_override", version_id="synthetic-policy-version", redistribute_text="allow")

    def allowed():
        with f.as_role("review") as conn:
            return conn.scalar(
                text(
                    f"SELECT {f.name(conn, 'policy', 'review_permission')}(:cid,'redistribute_text',:version)"
                ),
                {"cid": f.collection, "version": "synthetic-policy-version"},
            )

    assert not allowed()
    f.decision(revision=2, redistribute_text="allow")
    assert allowed()  # Private preflight only: no approved version or public text capability.
    f.record("version_override", version_id="synthetic-policy-version", revision=2, redistribute_text="deny")
    assert not allowed()
    with pytest.raises(IntegrityError), f.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE identity_legacy_versions SET content_hash=:hash WHERE version_id='synthetic-policy-version'"
            ),
            {"hash": "a" * 64},
        )
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_text").allowed


def test_search_path_cannot_redirect_security_definer_policy_reads(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with f.as_role("serving") as conn:
        conn.execute(text("CREATE TEMP TABLE collection_decision(redistribute_metadata text)"))
        conn.execute(text("INSERT INTO collection_decision VALUES ('deny')"))
        conn.execute(text("SET LOCAL search_path=pg_temp,public"))
        assert PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def test_populated_0002_upgrade_preserves_identity_shadows(pg_engine):
    migrate(pg_engine, "0002")
    store = Store(pg_engine)
    seed(store)
    with pg_engine.begin() as conn:
        backfill(conn, expected_epoch=0)
        before = {
            table: list(conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings())
            for table in ("identity_works", "identity_legacy_sources", "identity_legacy_snapshots")
        }
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    pilot = store.load().model_dump(mode="json")
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        after = {
            table: list(conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings())
            for table in before
        }
        for table in (
            "actor",
            "acquisition_assessment",
            "collection_decision",
            "verification_policy",
            "audit_event",
        ):
            assert conn.scalar(text(f"SELECT count(*) FROM {ns.qualified(conn, 'policy', table)}")) == 0
    assert after == before
    assert store.load().model_dump(mode="json") == pilot


def test_concurrent_policy_revisions_have_one_winner_and_one_audit(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    barrier = Barrier(2)

    def append():
        try:
            with f.as_role("review") as conn:
                conn.execute(text("SET LOCAL lock_timeout='4s'"))
                barrier.wait(timeout=3)
                conn.execute(
                    text(f"""
                    INSERT INTO {f.name(conn, "policy", "collection_decision")}
                        (revision,collection_id,actor_id,evidence_id,valid_from,expires_at,state,reason,
                         basis_type,conditions_satisfied,metadata_privacy_class,privacy_reason,redistribute_metadata)
                    VALUES (2,:cid,:actor,:evidence,:start,:end,'approved','Synthetic simultaneous review',
                            'original_authorship',true,'no_personal_data','Synthetic','deny')
                """),
                    {
                        "cid": f.collection,
                        "actor": f.actors["review"],
                        "evidence": f.evidence,
                        "start": f.now - timedelta(hours=1),
                        "end": f.now + timedelta(days=1),
                    },
                )
            return "committed"
        except DBAPIError as exc:
            return exc.orig.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append) for _ in range(2)]
        results = [future.result(timeout=6) for future in futures]
    assert sorted(results) == ["40001", "committed"]
    with f.as_role("review") as conn:
        assert (
            conn.scalar(
                text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='collection'")
            )
            == 2
        )
