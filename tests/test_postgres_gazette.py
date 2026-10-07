"""Native original-synthetic gazette correspondence; no new item byte owners."""

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_canonical import leaf
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE, assessment, declare, stage
from test_postgres_acquisition import chain as chain
from test_postgres_approval import approval_command, approve, bind, review_command, rows
from test_postgres_approval import ready as ready
from test_postgres_assembly import apply_assembly, run_command, snapshot_command
from test_postgres_assembly import assembly as assembly
from test_postgres_canonical import call, canonical_command
from test_postgres_operators import apply, decision_command, verification_command
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.approval import SyntheticApproval
from rights.gazette import SyntheticGazette, declare_synthetic_item_binding
from schema.gazette import ItemApprovalCommand, ItemReviewCommand
from schema.identity import mint

pytestmark = pytest.mark.postgres


@dataclass
class Gazette:
    f: object
    create: object
    rights: object
    editor: object
    maintainer: object
    worker: object
    item_collection: str
    item_evidence: object
    artifact: object
    version: object
    binding: object
    binding_hash: str
    review: object
    source: object | None = None


def dispatch(g, subject, cmd):
    with subject.engine.begin() as conn:
        return SyntheticGazette(conn, g.f.ns).apply(cmd)


def item_identity(g, notice_key="notice:one"):
    f = g.f
    work = mint("wrk")
    expression = mint("exp")
    bid = uuid4()
    with f.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity_works(id,jurisdiction_id,authority_id,document_class,title,citation,reference_url,reference_kind,catalogue_language_id,legacy_legal_status,identity_status,collection_id) SELECT :id,jurisdiction_id,authority_id,'act','Original synthetic identified item','Fictional item only','https://example.test/item','document','en','unknown','identified',:cid FROM identity_works WHERE id=:issue"
            ),
            {"id": work, "cid": g.item_collection, "issue": f.work},
        )
        conn.execute(
            text(
                "INSERT INTO identity_expressions(id,work_id,language_id,edition_kind,edition_key,edition_date_unknown_reason,authenticity_unknown_reason) VALUES(:id,:work,'en','original',:id,'Fictional source has no legal edition','No authenticity claim for fictional fixture')"
            ),
            {"id": expression, "work": work},
        )
        manifestation = conn.scalar(
            text(
                f"SELECT b.manifestation_id FROM {f.ns.qualified(conn, 'staging', 'synthetic_expression_binding')} b JOIN {f.ns.qualified(conn, 'corpus', 'approved_version')} v ON v.binding_id=b.id WHERE v.id=:id"
            ),
            {"id": g.version.version_id},
        )
        conn.execute(
            text("INSERT INTO identity_work_manifestations VALUES(:work,:man)"),
            {"work": work, "man": manifestation},
        )
        node = conn.scalar(
            text(
                f"SELECT id FROM {f.ns.qualified(conn, 'corpus', 'version_node')} WHERE version_id=:v AND local_key=:key"
            ),
            {"v": g.version.version_id, "key": notice_key},
        )
        digest = declare_synthetic_item_binding(
            conn,
            f.ns,
            binding_id=bid,
            item_expression_id=expression,
            issue_version_id=g.version.version_id,
            notice_node_id=node,
            evidence_id=g.item_evidence,
            reason="Original synthetic human identification; no legal approval",
        )
    return bid, digest


def item_review(g, binding=None, binding_hash=None, **changes):
    f = g.f
    bid = binding or g.binding
    with f.engine.connect() as conn:
        bindingrow = (
            conn.execute(
                text(f"SELECT * FROM {f.ns.qualified(conn, 'staging', 'gazette_item_binding')} WHERE id=:id"),
                {"id": bid},
            )
            .mappings()
            .one()
        )
        node = (
            conn.execute(
                text(f"SELECT * FROM {f.ns.qualified(conn, 'corpus', 'version_node')} WHERE id=:id"),
                {"id": bindingrow["notice_node_id"]},
            )
            .mappings()
            .one()
        )
        rep = (
            conn.execute(
                text(
                    f"SELECT r.* FROM {f.ns.qualified(conn, 'corpus', 'representation_revision')} r JOIN {f.ns.qualified(conn, 'corpus', 'representation_head')} h ON h.rep_id=r.id WHERE h.version_id=:id"
                ),
                {"id": g.version.version_id},
            )
            .mappings()
            .one()
        )
        version = (
            conn.execute(
                text(f"SELECT * FROM {f.ns.qualified(conn, 'corpus', 'approved_version')} WHERE id=:id"),
                {"id": g.version.version_id},
            )
            .mappings()
            .one()
        )
    data = dict(
        command_id=uuid4(),
        issue_collection_id=f.collection,
        item_collection_id=g.item_collection,
        binding_id=bid,
        purpose=PURPOSE,
        reason="Original synthetic complete notice identification",
        action="record_gazette_item_review",
        review_id=uuid4(),
        binding_hash=binding_hash or g.binding_hash,
        issue_version_id=g.version.version_id,
        representation_id=rep["id"],
        record_set_hash=rep["record_set_hash"],
        content_hash=version["content_hash"],
        profile_hash=version["profile_hash"],
        ranges=[dict(start_byte=node["start_byte"], end_byte=node["end_byte"], span_hash=node["span_hash"])],
        item_rights_revision=1,
        item_verification_revision=1,
        expected_revision=0,
        evidence_id=g.item_evidence,
        identification_method="human_source_identification",
        review_scope="collection_policy",
        requires_exception_review=False,
    )
    data.update(changes)
    return ItemReviewCommand(**data)


def item_approval(g, review, result, **changes):
    data = dict(
        command_id=uuid4(),
        issue_collection_id=g.f.collection,
        item_collection_id=g.item_collection,
        binding_id=review.binding_id,
        purpose=PURPOSE,
        reason="Original synthetic private correspondence approval",
        action="approve_gazette_item_correspondence",
        review_id=review.review_id,
        review_hash=result.review_hash,
        correspondence_id=uuid4(),
        expected_revision=review.expected_revision,
    )
    data.update(changes)
    return ItemApprovalCommand(**data)


@pytest.fixture
def gazette(operators, request):
    f, create, _ = operators
    item_collection, item_evidence = f.collection, f.evidence
    f.collection = mint("scp")
    work = mint("wrk")
    with f.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key) VALUES(:id,'ng','gazette','original-synthetic-gazette')"
            ),
            {"id": f.collection},
        )
        conn.execute(
            text(
                "INSERT INTO identity_works(id,jurisdiction_id,authority_id,document_class,title,citation,reference_url,reference_kind,catalogue_language_id,legacy_legal_status,identity_status,collection_id) SELECT :id,jurisdiction_id,authority_id,'gazette','Original synthetic gazette issue','Fictional issue only','https://example.test/issue','document','en','unknown','identified',:cid FROM identity_works WHERE id=:old"
            ),
            {"id": work, "cid": f.collection, "old": f.work},
        )
        f.work = work
        f.evidence = uuid4()
        conn.execute(
            text(
                f"INSERT INTO {f.ns.qualified(conn, 'policy', 'evidence')}(id,collection_id,private_reference,evidence_hash,observed_at) VALUES(:id,:cid,'private://synthetic-gazette',:hash,:now)"
            ),
            {"id": f.evidence, "cid": f.collection, "hash": "a" * 64, "now": f.now},
        )
    rights = create("rights", assignments=((f.collection, "rights"), (item_collection, "rights")))
    editor = create(
        "content",
        assignments=(
            (f.collection, "content"),
            (f.collection, "acquire"),
            (item_collection, "content"),
            (item_collection, "acquire"),
        ),
    )
    maintainer = create(
        "release",
        assignments=(
            (f.collection, "release"),
            (f.collection, "acquire"),
            (item_collection, "release"),
            (item_collection, "acquire"),
        ),
    )
    worker = create("acquire", assignments=((f.collection, "acquire"), (item_collection, "acquire")))
    for cid, evidence in ((f.collection, f.evidence), (item_collection, item_evidence)):
        decision = decision_command(f).model_copy(update={"collection_id": cid})
        decision = decision.model_copy(
            update={
                "payload": decision.payload.model_copy(
                    update={
                        "evidence_id": evidence,
                        "redistribute_text": "allow",
                        "quote": "allow",
                        "derive": "allow",
                        "retain": "allow",
                    }
                )
            }
        )
        assert apply(f, rights, decision).status == "applied"
        verification = verification_command(f).model_copy(
            update={
                "collection_id": cid,
                "payload": verification_command(f).payload.model_copy(
                    update={
                        "evidence_id": evidence,
                        "materials": tuple(
                            m.model_copy(update={"competence_evidence_id": evidence})
                            for m in verification_command(f).payload.materials
                        ),
                    }
                ),
            }
        )
        assert apply(f, editor, verification).status == "applied"
    candidates = [
        leaf(201, " Cafe\u0301 لا\u2067RTL\u2069 😀 [n]."),
        leaf(202, "Footnote: no amount waived.", block_type="footnote"),
        leaf(203, "Amount", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
        leaf(
            204,
            "1,000.00 not waived",
            block_type="table_cell",
            cell=dict(table_local_id="t", row=1, column=0),
        ),
        leaf(205, "Second notice: § — literal.\n"),
    ]
    marker_start = candidates[0].text.encode().index(b"[n]")
    plan = dict(
        blocks=[
            dict(
                kind="notice",
                local_id="one",
                blocks=[
                    dict(kind="text", candidate_id=candidates[0].id),
                    dict(
                        kind="table",
                        table_local_id="t",
                        rows=2,
                        columns=1,
                        cells=[dict(candidates=[c.id]) for c in candidates[2:4]],
                    ),
                ],
                footnotes=[dict(local_id="n", candidates=[candidates[1].id])],
                markers=[
                    dict(
                        candidate_id=candidates[0].id,
                        source_start=marker_start,
                        source_end=marker_start + 3,
                        footnote_id="n",
                    )
                ],
            ),
            dict(kind="notice", local_id="two", blocks=[dict(kind="text", candidate_id=candidates[4].id)]),
        ]
    )
    # Snapshot body order follows body leaves; footnote source location is after its notice.
    candidates = [candidates[0], candidates[2], candidates[3], candidates[1], candidates[4]]
    raw = "\n".join(c.text for c in candidates).encode()
    acq = assessment(f, derive="allow", artifact_hash=sha256(raw).hexdigest())
    assert apply(f, rights, acq).status == "applied"
    declare(f, maximum=getattr(request, "param", 100 * 1024 * 1024))
    artifact, _ = stage(f, worker, acq, private_bytes=raw)
    run = run_command(f, artifact, acq, candidates=candidates).model_copy(
        update={"artifact_hash": sha256(raw).hexdigest()}
    )
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    canonical = canonical_command(f, snap, saved.output_hash, plan)
    output = call(f, editor, canonical)
    assert output.status == "applied"
    binding, hash, _, _ = bind(f, artifact)
    review = review_command(f, binding, hash, canonical, output, run.candidates)
    compared = approve(f, editor, review)
    assert compared.status == "applied"
    version = approve(f, maintainer, approval_command(f, review, compared))
    assert version.status == "applied", version
    g = Gazette(
        f,
        create,
        rights,
        editor,
        maintainer,
        worker,
        item_collection,
        item_evidence,
        artifact,
        version,
        None,
        "",
        None,
    )
    g.source = (run, snap, canonical, review, plan)
    g.binding, g.binding_hash = item_identity(g)
    g.review = item_review(g)
    return g


def test_notice_projection_reuses_issue_bytes_nodes_anchors_and_preserves_utf8_table_footnote(gazette):
    g = gazette
    f = g.f
    before = {
        table: rows(f, table)
        for table in ("approved_version", "version_node", "approved_anchor", "approved_cell")
    }
    with g.worker.engine.begin() as conn:
        assert SyntheticApproval(conn, f.ns).read(g.version.version_id, purpose=PURPOSE) is None
    compared = dispatch(g, g.editor, g.review)
    assert compared.status == "applied", compared
    cmd = item_approval(g, g.review, compared)
    result = dispatch(g, g.maintainer, cmd)
    assert result.status == "applied", result
    assert dispatch(g, g.maintainer, cmd).replayed
    with g.worker.engine.begin() as conn:
        projected = SyntheticGazette(conn, f.ns).read(result.correspondence_id, purpose=PURPOSE)
        issue = SyntheticApproval(conn, f.ns).read(g.version.version_id, purpose=PURPOSE)
        assert projected and issue
        span = projected.ranges[0]
        assert (
            projected.projected_text.encode()
            == issue.canonical_text.encode()[span.start_byte : span.end_byte]
        )
        assert sha256(projected.projected_text.encode()).hexdigest() == span.span_hash
        assert (
            "Café" in projected.projected_text
            and "😀" in projected.projected_text
            and "1,000.00 not waived" in projected.projected_text
            and "Footnote:" in projected.projected_text
        )
        assert "Second notice" not in projected.projected_text and not projected.publication_eligible
    for table, data in before.items():
        assert rows(f, table) == data


def published(g):
    reviewed = dispatch(g, g.editor, g.review)
    assert reviewed.status == "applied", reviewed
    cmd = item_approval(g, g.review, reviewed)
    result = dispatch(g, g.maintainer, cmd)
    assert result.status == "applied", result
    return cmd, result


def read_pair(g, result, subject=None):
    with (subject or g.worker).engine.begin() as conn:
        projection = SyntheticGazette(conn, g.f.ns).read(result.correspondence_id, purpose=PURPOSE)
        issue = SyntheticApproval(conn, g.f.ns).read(g.version.version_id, purpose=PURPOSE)
    return projection, issue


@pytest.mark.parametrize(
    "field",
    [
        "binding_hash",
        "record_set_hash",
        "content_hash",
        "profile_hash",
        "representation_id",
        "issue_version_id",
        "evidence_id",
    ],
)
def test_identification_rejects_unpinned_identity_version_representation_and_evidence(gazette, field):
    g = gazette
    value = (
        uuid4()
        if field == "evidence_id"
        else "prp_" + str(uuid4())
        if field == "representation_id"
        else "ver_" + str(uuid4())
        if field == "issue_version_id"
        else "f" * 64
    )
    cmd = g.review.model_copy(update={field: value})
    assert dispatch(g, g.editor, cmd).status in {"forbidden", "validation_failed"}
    assert not rows(g.f, "gazette_item_review", "staging")


@pytest.mark.parametrize("change", ["partial", "outside", "wrong_hash", "bool_offset"])
def test_only_exact_complete_notice_interval_can_be_reviewed(gazette, change):
    g = gazette
    body = g.review.model_dump(mode="json")
    if change == "partial":
        body["ranges"][0]["end_byte"] -= 1
    elif change == "outside":
        body["ranges"][0]["start_byte"] = 0
        body["ranges"][0]["end_byte"] = 131072
    elif change == "wrong_hash":
        body["ranges"][0]["span_hash"] = "f" * 64
    else:
        with g.editor.engine.begin() as conn:
            body["ranges"][0]["start_byte"] = False
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError) as failure, conn.begin_nested():
                conn.execute(
                    text(
                        f"SELECT {g.f.ns.qualified(conn, 'policy', 'apply_gazette_command')}(CAST(:body AS jsonb))"
                    ),
                    {"body": json.dumps(body)},
                )
            assert failure.value.orig.sqlstate in {"22023", "23514"}
        return
    assert dispatch(g, g.editor, ItemReviewCommand.model_validate(body)).status == "validation_failed"


@pytest.mark.parametrize(
    "kind", ["issue_only", "item_only", "content_only", "rights", "release_without_item", "revoked"]
)
def test_correspondence_requires_both_scopes_and_native_release(gazette, kind):
    from rights.operators import revoke_operator

    g = gazette
    f = g.f
    reviewed = dispatch(g, g.editor, g.review)
    assert reviewed.status == "applied"
    cmd = item_approval(g, g.review, reviewed)
    subjects = {"content_only": g.editor, "rights": g.rights, "revoked": g.maintainer}
    if kind == "issue_only":
        subject = g.create("release", assignments=((f.collection, "release"), (f.collection, "acquire")))
    elif kind == "item_only":
        subject = g.create(
            "release", assignments=((g.item_collection, "release"), (g.item_collection, "acquire"))
        )
    elif kind == "release_without_item":
        subject = g.create(
            "release",
            assignments=(
                (f.collection, "release"),
                (g.item_collection, "release"),
                (f.collection, "acquire"),
            ),
        )
    else:
        subject = subjects[kind]
    if kind == "revoked":
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, subject.actor, reason="Original synthetic revocation")
    assert dispatch(g, subject, cmd).status == "forbidden"
    assert not rows(f, "gazette_correspondence")


@pytest.mark.parametrize("which", ["item_rights", "item_content", "issue_rights", "source", "item_actor"])
def test_withholding_either_context_denies_projection_and_dependent_issue_before_jobs(gazette, which):
    from test_postgres_lifecycle import newer

    from rights.operators import revoke_operator

    g = gazette
    f = g.f
    cmd, result = published(g)
    if which == "item_rights":
        old = decision_command(f).model_copy(
            update={
                "collection_id": g.item_collection,
                "payload": decision_command(f).payload.model_copy(update={"evidence_id": g.item_evidence}),
            }
        )
        newer(f, g.rights, old, redistribute_text="deny")
    elif which == "item_content":
        old = verification_command(f).model_copy(
            update={
                "collection_id": g.item_collection,
                "payload": verification_command(f).payload.model_copy(
                    update={
                        "evidence_id": g.item_evidence,
                        "materials": tuple(
                            m.model_copy(update={"competence_evidence_id": g.item_evidence})
                            for m in verification_command(f).payload.materials
                        ),
                    }
                ),
            }
        )
        newer(f, g.editor, old, state="revoked")
    elif which == "issue_rights":
        newer(f, g.rights, decision_command(f), redistribute_text="deny")
    elif which == "item_actor":
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, g.rights.actor, reason="Original synthetic revoke")
    else:
        from rights.acquisition import SyntheticAcquisition

        with g.worker.engine.begin() as conn:
            assert SyntheticAcquisition(conn, f.ns).erase(g.artifact)
    assert read_pair(g, result) == (None, None)
    assert dispatch(g, g.maintainer, cmd).status == "forbidden"


def test_pending_or_withheld_second_identified_item_cannot_be_bypassed_through_first_projection(gazette):
    g = gazette
    _, first = published(g)
    assert all(read_pair(g, first))
    bid, digest = item_identity(g, "notice:two")
    assert read_pair(g, first) == (None, None)
    review = item_review(g, bid, digest)
    compared = dispatch(g, g.editor, review)
    assert compared.status == "applied"
    second = dispatch(g, g.maintainer, item_approval(g, review, compared))
    assert second.status == "applied"
    assert all(read_pair(g, first)) and all(read_pair(g, second))
    assert len(rows(g.f, "approved_version")) == 1 and len(rows(g.f, "gazette_correspondence")) == 2


def test_issue_only_reader_cannot_use_old_reader_alias_or_new_projection_to_skip_item_scope(gazette):
    from sqlalchemy.exc import DBAPIError

    g = gazette
    _, result = published(g)
    issue_only = g.create("acquire", assignments=((g.f.collection, "acquire"),))
    assert read_pair(g, result, issue_only) == (None, None)
    with pytest.raises(DBAPIError), issue_only.engine.begin() as conn:
        conn.execute(
            text(
                f"SELECT {g.f.ns.qualified(conn, 'policy', 'read_approved_synthetic_version_m33')}(:id,:purpose)"
            ),
            {"id": g.version.version_id, "purpose": PURPOSE},
        )


def test_caller_and_injected_failure_roll_back_correspondence_head_audit_and_receipt(gazette):
    g = gazette
    f = g.f
    compared = dispatch(g, g.editor, g.review)
    cmd = item_approval(g, g.review, compared)
    before = rows(f, "gazette_receipt", "policy")
    audit = rows(f, "audit_event", "policy")
    with g.maintainer.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticGazette(conn, f.ns).apply(cmd).status == "applied"
        tx.rollback()
    assert not rows(f, "gazette_correspondence") and not rows(f, "gazette_correspondence_head")
    assert rows(f, "gazette_receipt", "policy") == before and rows(f, "audit_event", "policy") == audit
    with f.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "reject_test_gazette")
        conn.execute(
            text(
                f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Original synthetic insert failure'; END $$"
            )
        )
        conn.execute(
            text(
                f"CREATE TRIGGER reject_test BEFORE INSERT ON {f.ns.qualified(conn, 'corpus', 'gazette_correspondence_head')} FOR EACH ROW EXECUTE FUNCTION {fn}()"
            )
        )
    assert dispatch(g, g.maintainer, cmd).status == "unavailable"
    assert not rows(f, "gazette_correspondence") and rows(f, "audit_event", "policy") == audit
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"DROP TRIGGER reject_test ON {f.ns.qualified(conn, 'corpus', 'gazette_correspondence_head')}"
            )
        )
    assert dispatch(g, g.maintainer, cmd).status == "applied"


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_approvals_create_one_current_mapping_and_changed_retries_conflict(gazette, identical):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    g = gazette
    compared = dispatch(g, g.editor, g.review)
    cmd = item_approval(g, g.review, compared)
    second = (
        cmd if identical else cmd.model_copy(update={"command_id": uuid4(), "correspondence_id": uuid4()})
    )
    barrier = Barrier(2)

    def submit(command):
        barrier.wait(10)
        return dispatch(g, g.maintainer, command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (cmd, second)))
    assert len(rows(g.f, "gazette_correspondence")) == 1
    assert sorted(r.status for r in results) == (
        ["applied", "applied"] if identical else ["applied", "conflict"]
    )
    current = cmd if results[0].status == "applied" else second
    assert (
        dispatch(
            g, g.maintainer, current.model_copy(update={"reason": "Changed original synthetic intent"})
        ).status
        == "conflict"
    )


def test_parent_erasure_clears_all_new_payloads_and_never_copies_issue_bytes(gazette):
    from rights.acquisition import SyntheticAcquisition

    g = gazette
    _, result = published(g)
    anchors = rows(g.f, "approved_anchor")
    with g.worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, g.f.ns).erase(g.artifact)
    for schema, table in [
        ("staging", "gazette_item_binding"),
        ("staging", "gazette_item_review"),
        ("corpus", "gazette_correspondence"),
    ]:
        row = rows(g.f, table, schema)[0]
        assert row["state"] == "erased" and row["payload"] is None
    assert rows(g.f, "approved_anchor") == anchors
    assert read_pair(g, result) == (None, None)
    with g.f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT {g.f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(:cid)"),
                {"cid": g.f.collection},
            )
            == 0
        )


def test_materialized_item_hold_requires_fresh_review_revision_and_does_not_resume_old_rows(gazette):
    from test_postgres_assembly import reconcile
    from test_postgres_lifecycle import newer

    g = gazette
    f = g.f
    _, old = published(g)
    decision = decision_command(f).model_copy(
        update={
            "collection_id": g.item_collection,
            "payload": decision_command(f).payload.model_copy(
                update={
                    "evidence_id": g.item_evidence,
                    "redistribute_text": "allow",
                    "quote": "allow",
                    "derive": "allow",
                    "retain": "allow",
                }
            ),
        }
    )
    rejected = newer(f, g.rights, decision, redistribute_text="deny")
    assert reconcile(f, g.worker).held == 2
    assert rows(f, "gazette_correspondence")[0]["state"] == "held"
    assert rows(f, "approved_version")[0]["state"] == "active"
    newer(f, g.rights, rejected, redistribute_text="allow")
    assert read_pair(g, old) == (None, None)
    review = item_review(g, item_rights_revision=3, expected_revision=1)
    compared = dispatch(g, g.editor, review)
    assert compared.status == "applied", compared
    renewed = dispatch(g, g.maintainer, item_approval(g, review, compared))
    assert renewed.status == "applied", renewed
    assert renewed.revision == 2 and all(read_pair(g, renewed))
    assert read_pair(g, old)[0] is None
    assert any(row["state"] == "held" for row in rows(f, "gazette_correspondence"))


def test_native_subjects_cannot_declare_item_identities_or_read_private_tables(gazette):
    from sqlalchemy.exc import DBAPIError

    g = gazette
    for subject in (g.worker, g.editor, g.maintainer):
        with pytest.raises(ValueError), subject.engine.begin() as conn:
            declare_synthetic_item_binding(
                conn,
                g.f.ns,
                binding_id=uuid4(),
                item_expression_id=mint("exp"),
                issue_version_id=g.version.version_id,
                notice_node_id="nod_" + str(uuid4()),
                evidence_id=g.item_evidence,
                reason="Original synthetic forbidden declaration",
            )
        with pytest.raises(DBAPIError), subject.engine.begin() as conn:
            conn.execute(
                text(f"SELECT payload FROM {g.f.ns.qualified(conn, 'staging', 'gazette_item_binding')}")
            )


def test_bound_item_identity_changes_and_root_wrong_notice_ranges_are_rejected(gazette):
    from sqlalchemy.exc import DBAPIError

    g = gazette
    _, result = published(g)
    binding = rows(g.f, "gazette_item_binding", "staging")[0]
    with pytest.raises(DBAPIError) as failure, g.f.engine.begin() as conn:
        conn.execute(
            text("UPDATE identity_works SET title=:title WHERE id=:id"),
            {"title": "Changed original synthetic identification", "id": binding["item_work_id"]},
        )
    assert failure.value.orig.sqlstate == "55000"
    row = rows(g.f, "gazette_correspondence")[0]
    row.update(id=uuid4(), revision=2, parent_id=result.correspondence_id, end_byte=row["end_byte"] - 1)
    with pytest.raises(DBAPIError) as failure, g.f.engine.begin() as conn:
        columns = ",".join(row)
        values = ",".join("CAST(:payload AS jsonb)" if key == "payload" else ":" + key for key in row)
        row["payload"] = json.dumps(row["payload"])
        conn.execute(
            text(
                f"INSERT INTO {g.f.ns.qualified(conn, 'corpus', 'gazette_correspondence')}({columns}) VALUES({values})"
            ),
            row,
        )
    assert failure.value.orig.sqlstate == "23514"


def test_correspondence_payloads_are_charged_to_issue_allocation_without_item_byte_copies(gazette):
    g = gazette
    f = g.f
    with f.engine.connect() as conn:
        fn = f.ns.qualified(conn, "policy", "staging_live_bytes")
        before = conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": f.collection})
        assert conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": g.item_collection}) == 0
    published(g)
    with f.engine.connect() as conn:
        after = conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": f.collection})
        stored = sum(
            conn.scalar(
                text(
                    f"SELECT coalesce(sum(octet_length(payload::text)),0) FROM {f.ns.qualified(conn, schema, table)}"
                )
            )
            for schema, table in [("staging", "gazette_item_review"), ("corpus", "gazette_correspondence")]
        )
        assert after - before == stored and stored > 0
        assert conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": g.item_collection}) == 0


def test_commit_time_item_expiry_rolls_back_mapping_receipt_and_audit(gazette):
    from datetime import UTC, datetime, timedelta
    from time import sleep

    from sqlalchemy.exc import DBAPIError
    from test_postgres_lifecycle import newer

    g = gazette
    f = g.f
    old = decision_command(f).model_copy(
        update={
            "collection_id": g.item_collection,
            "payload": decision_command(f).payload.model_copy(
                update={
                    "evidence_id": g.item_evidence,
                    "retain": "allow",
                    "derive": "allow",
                    "quote": "allow",
                    "redistribute_text": "allow",
                }
            ),
        }
    )
    with f.engine.connect() as conn:
        deadline = conn.scalar(text("SELECT clock_timestamp()")) + timedelta(seconds=6)
    newer(f, g.rights, old, expires_at=deadline.isoformat())
    review = item_review(g, item_rights_revision=2)
    compared = dispatch(g, g.editor, review)
    assert compared.status == "applied"
    cmd = item_approval(g, review, compared)
    before = rows(f, "gazette_receipt", "policy")
    with g.maintainer.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticGazette(conn, f.ns).apply(cmd).status == "applied"
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            sleep(remaining + 0.05)
        with pytest.raises(DBAPIError) as failure:
            tx.commit()
        assert failure.value.orig.sqlstate == "42501"
    assert not rows(f, "gazette_correspondence") and rows(f, "gazette_receipt", "policy") == before


def test_projection_transaction_blocks_item_withdrawal_through_caller_commit(gazette):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    from test_postgres_lifecycle import newer

    g = gazette
    _, result = published(g)
    started = Event()

    def deny():
        started.set()
        old = decision_command(g.f).model_copy(
            update={
                "collection_id": g.item_collection,
                "payload": decision_command(g.f).payload.model_copy(update={"evidence_id": g.item_evidence}),
            }
        )
        return newer(g.f, g.rights, old, redistribute_text="deny")

    with g.worker.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticGazette(conn, g.f.ns).read(result.correspondence_id, purpose=PURPOSE)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(deny)
            assert started.wait(5)
            sleep(0.2)
            assert not future.done()
            tx.commit()
            future.result(timeout=10)
    assert read_pair(g, result) == (None, None)


def test_injected_correspondence_erasure_failure_rolls_back_whole_issue_family(gazette):
    from rights.acquisition import AcquisitionError, SyntheticAcquisition

    g = gazette
    f = g.f
    published(g)
    before = {
        (schema, table): rows(f, table, schema)
        for schema, table in [
            ("staging", "staged_artifact"),
            ("staging", "gazette_item_binding"),
            ("staging", "gazette_item_review"),
            ("corpus", "gazette_correspondence"),
            ("corpus", "approved_version"),
            ("policy", "audit_event"),
        ]
    }
    with f.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "reject_test_gazette_erasure")
        conn.execute(
            text(
                f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Original synthetic cleanup fault'; END $$"
            )
        )
        conn.execute(
            text(
                f"CREATE TRIGGER reject_test BEFORE UPDATE ON {f.ns.qualified(conn, 'corpus', 'gazette_correspondence')} FOR EACH ROW EXECUTE FUNCTION {fn}()"
            )
        )
    with pytest.raises(AcquisitionError), g.worker.engine.begin() as conn:
        SyntheticAcquisition(conn, f.ns).erase(g.artifact)
    for (schema, table), data in before.items():
        assert rows(f, table, schema) == data


def test_frozen_migration_denies_correspondence_writes_and_allows_erasure(gazette):
    from api.identity import freeze
    from rights.acquisition import SyntheticAcquisition

    g = gazette
    with g.f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert dispatch(g, g.editor, g.review).status in {"forbidden", "unavailable"}
    with g.worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, g.f.ns).erase(g.artifact)
    assert rows(g.f, "gazette_item_binding", "staging")[0]["payload"] is None


def test_gazette_cli_prints_only_scoped_review_metadata(gazette):
    import os
    import subprocess
    import sys

    g = gazette
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=g.editor.engine.url.render_as_string(hide_password=False)
    )
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--gazette", "--schema", g.f.ns.base],
        input=g.review.model_dump_json(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "applied"
    assert (
        "Café" not in result.stdout + result.stderr and g.editor.password not in result.stdout + result.stderr
    )


def test_populated_0009_empty_upgrade_restores_exact_helpers_and_native_reader_grants(ready):
    from alembic import command
    from alembic.config import Config
    from test_postgres import migrate

    f, *_ = ready
    before = rows(f, "snapshot_review", "staging")
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0009")
        original = {
            fn: conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                {"fn": f.ns.schema("policy") + "." + fn},
            )
            for fn in [
                "approved_version_current(text,text)",
                "read_approved_synthetic_version(text,text)",
                "hold_stale_approval(uuid)",
                "bound_identity_immutable()",
                "apply_synthetic_approval(jsonb)",
                "validate_approval_commit()",
                "staging_live_bytes(text)",
                "reserve_assembly_capacity(uuid,text,bigint,integer)",
                "transition_assembly(uuid,text,text)",
                "apply_assembly_command(jsonb)",
            ]
        }
        assert conn.scalar(
            text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"),
            {
                "role": f.ns.role("acquisition"),
                "fn": f.ns.schema("policy") + ".read_approved_synthetic_version(text,text)",
            },
        )
    migrate(f.engine)
    assert rows(f, "snapshot_review", "staging") == before
    with f.engine.connect() as conn:
        for fn, body in original.items():
            assert (
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                    {"fn": f.ns.schema("policy") + "." + fn.replace("(", "_m33(", 1)},
                )
                == body
            )
        assert not conn.scalar(
            text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"),
            {
                "role": f.ns.role("acquisition"),
                "fn": f.ns.schema("policy") + ".read_approved_synthetic_version_m33(text,text)",
            },
        )


@pytest.mark.parametrize("corruption", ["guard_create", "guard_inheritance", "function_owner"])
def test_gazette_preflight_refuses_corrupted_boundary_before_ddl(ready, corruption):
    from alembic import command
    from alembic.config import Config
    from test_postgres import migrate

    f, *_ = ready
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0009")
    with f.engine.connect() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        guard = quote(f.ns.role("guard"))
        acq = quote(f.ns.role("acquisition"))
        owner = quote(conn.scalar(text("SELECT current_user")))
        conn.rollback()
        tx = conn.begin()
        if corruption == "guard_create":
            conn.execute(text(f"GRANT CREATE ON SCHEMA {quote(f.ns.schema('policy'))} TO {guard}"))
        elif corruption == "guard_inheritance":
            conn.execute(text(f"GRANT {guard} TO {acq}"))
        else:
            conn.execute(
                text(
                    f"ALTER FUNCTION {f.ns.qualified(conn, 'policy', 'approved_version_current')}(text,text) OWNER TO {owner}"
                )
            )
        with pytest.raises(RuntimeError, match="boundary review"):
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        assert (
            conn.scalar(
                text("SELECT to_regclass(:name)"), {"name": f.ns.schema("staging") + ".gazette_item_binding"}
            )
            is None
        )
        tx.rollback()
    migrate(f.engine)


def test_nonempty_gazette_history_refuses_downgrade_without_discarding_identification(gazette):
    from alembic import command
    from alembic.config import Config

    g = gazette
    with pytest.raises(RuntimeError, match="forward repair"), g.f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0009")
    assert rows(g.f, "gazette_item_binding", "staging")[0]["payload"] is not None


@pytest.mark.parametrize("gazette", [17300], indirect=True)
def test_shared_collection_quota_rejects_correspondence_without_receipt_audit_or_head(gazette):
    g = gazette
    compared = dispatch(g, g.editor, g.review)
    assert compared.status == "applied"
    receipts = rows(g.f, "gazette_receipt", "policy")
    audits = rows(g.f, "audit_event", "policy")
    result = dispatch(g, g.maintainer, item_approval(g, g.review, compared))
    assert result.status == "capacity_exceeded", result
    assert not rows(g.f, "gazette_correspondence") and not rows(g.f, "gazette_correspondence_head")
    assert rows(g.f, "gazette_receipt", "policy") == receipts
    assert rows(g.f, "audit_event", "policy") == audits


def test_owner_representation_edit_quarantines_old_mapping_until_fresh_item_review(gazette):
    from test_postgres_approval import revised_review

    g = gazette
    initial_command, initial = published(g)
    run, snap, canonical, owner_review, plan = g.source
    nodes, anchors = rows(g.f, "version_node"), rows(g.f, "approved_anchor")
    changed = json.loads(json.dumps(plan, default=str))
    changed["blocks"][0]["blocks"][1]["headers"] = [run.candidates[1].id]
    _, _, reviewed, compared = revised_review(
        (g.f, None, g.worker, g.editor), run, snap, canonical, changed, owner_review
    )
    result = approve(
        g.f,
        g.maintainer,
        approval_command(
            g.f,
            reviewed,
            compared,
            version_id=g.version.version_id,
            prior_version_id=g.version.version_id,
            expected_rep_revision=1,
            mode="representation_revision",
        ),
    )
    assert result.status == "applied" and result.revision == 2, result
    with g.worker.engine.begin() as conn:
        assert SyntheticGazette(conn, g.f.ns).read(initial.correspondence_id, purpose=PURPOSE) is None
        assert SyntheticApproval(conn, g.f.ns).read(g.version.version_id, purpose=PURPOSE) is None
    assert dispatch(g, g.maintainer, initial_command).status == "forbidden"
    fresh = item_review(g, expected_revision=1)
    compared = dispatch(g, g.editor, fresh)
    assert compared.status == "applied", compared
    final = dispatch(g, g.maintainer, item_approval(g, fresh, compared))
    assert final.status == "applied" and final.revision == 2, final
    with g.worker.engine.begin() as conn:
        projection = SyntheticGazette(conn, g.f.ns).read(final.correspondence_id, purpose=PURPOSE)
        assert projection and projection.representation_id == result.representation_id
        assert SyntheticGazette(conn, g.f.ns).read(initial.correspondence_id, purpose=PURPOSE) is None
        assert SyntheticApproval(conn, g.f.ns).read(g.version.version_id, purpose=PURPOSE)
    assert rows(g.f, "version_node") == nodes and rows(g.f, "approved_anchor") == anchors
