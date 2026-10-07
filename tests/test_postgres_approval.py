"""Real native sessions approving original synthetic fixtures; public text stays disabled."""

import json
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE, RAW, declare, stage
from test_postgres_acquisition import chain as chain
from test_postgres_assembly import apply_assembly, candidate, run_command, snapshot_command
from test_postgres_assembly import assembly as assembly
from test_postgres_canonical import call, canonical_command
from test_postgres_canonical import projection as projection
from test_postgres_lifecycle import newer
from test_postgres_operators import apply, decision_command, verification_command
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.approval import SyntheticApproval, declare_synthetic_binding
from schema.approval import ApproveVersionCommand, SnapshotReviewCommand
from schema.identity import mint

pytestmark = pytest.mark.postgres


def rows(f, table, schema="corpus"):
    with f.engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM {f.ns.qualified(conn, schema, table)} ORDER BY 1")
            ).mappings()
        ]


def approve(f, subject, cmd):
    with subject.engine.begin() as conn:
        return SyntheticApproval(conn, f.ns).apply(cmd)


def review_command(f, binding_id, binding_hash, canonical, canonical_result, candidates):
    return SnapshotReviewCommand(
        command_id=uuid4(),
        collection_id=f.collection,
        purpose=PURPOSE,
        reason="Source and grid comparison of original synthetic fixture; no legal approval",
        action="record_snapshot_review",
        review_id=uuid4(),
        binding_id=binding_id,
        binding_hash=binding_hash,
        proposal_id=canonical.proposal_id,
        snapshot_hash=canonical.snapshot_hash,
        profile_hash=__import__("ingestion.canonical", fromlist=["PROFILE_HASH"]).PROFILE_HASH,
        content_hash=canonical_result.output_hash,
        verification_revision=1,
        evidence_id=f.evidence,
        reviewed_candidate_ids=[c.id for c in candidates],
        comparison_method="human_source_comparison",
        review_scope="collection_policy",
        requires_exception_review=False,
    )


def approval_command(f, review, result, **changes):
    data = dict(
        command_id=uuid4(),
        collection_id=f.collection,
        purpose=PURPOSE,
        reason="Original synthetic approval only; public serving disabled",
        action="approve_synthetic_version",
        review_id=review.review_id,
        review_hash=result.review_hash,
        rights_revision=1,
        verification_revision=1,
        version_id="ver_" + str(uuid4()),
        representation_id="prp_" + str(uuid4()),
        mode="new_version",
    )
    data.update(changes)
    return ApproveVersionCommand(**data)


def bind(f, artifact, *, expression=None, manifestation=None):
    expression = expression or mint("exp")
    manifestation = manifestation or mint("man")
    binding = uuid4()
    with f.engine.begin() as conn:
        conn.execute(
            text("UPDATE identity_works SET identity_status='identified' WHERE id=:id"), {"id": f.work}
        )
        conn.execute(
            text(
                "INSERT INTO identity_expressions(id,work_id,language_id,edition_kind,edition_key,edition_date_unknown_reason,authenticity_unknown_reason) VALUES(:id,:work,'en','original',:key,'No legal edition in fictional fixture','Fictional fixture, no legal authenticity claim') ON CONFLICT(id) DO NOTHING"
            ),
            {"id": expression, "work": f.work, "key": expression},
        )
        raw = conn.execute(
            text(
                f"SELECT artifact_hash,size_bytes FROM {f.ns.qualified(conn, 'staging', 'staged_artifact')} WHERE id=:id"
            ),
            {"id": artifact},
        ).one()
        conn.execute(
            text(
                "INSERT INTO identity_manifestations(id,raw_hash,media_type,size_bytes,provenance_reference) VALUES(:id,:hash,'text/plain',:size,'private://original-synthetic-fixture')"
            ),
            {"id": manifestation, "hash": raw.artifact_hash, "size": raw.size_bytes},
        )
        conn.execute(
            text("INSERT INTO identity_work_manifestations VALUES(:work,:manifestation)"),
            {"work": f.work, "manifestation": manifestation},
        )
        digest = declare_synthetic_binding(
            conn,
            f.ns,
            binding_id=binding,
            expression_id=expression,
            manifestation_id=manifestation,
            artifact_id=artifact,
            evidence_id=f.evidence,
            reason="Explicit fictional identity, exact original synthetic bytes",
        )
    return binding, digest, expression, manifestation


@pytest.fixture
def ready(chain, operators, request):
    f, rights, worker, ctl, pv, acq = chain
    _, create, _ = operators
    acq = newer(
        f,
        rights,
        acq,
        derive="allow",
        privacy_class="no_personal_data",
        privacy_review_id=None,
        privacy_review_revision=None,
    )
    editor = create("content", assignments=((f.collection, "content"), (f.collection, "acquire")))
    maintainer = create("release", assignments=((f.collection, "release"), (f.collection, "acquire")))
    decision = decision_command(f)
    decision = decision.model_copy(
        update={
            "payload": decision.payload.model_copy(
                update={"redistribute_text": "allow", "quote": "allow", "derive": "allow"}
            )
        }
    )
    assert apply(f, rights, decision).status == "applied"
    verification = verification_command(f)
    assert apply(f, editor, verification).status == "applied"
    declare(f, getattr(request, "param", 100 * 1024 * 1024))
    artifact, _ = stage(f, worker, acq)
    run = run_command(f, artifact, acq, candidates=(candidate(text=RAW.decode()),))
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    canonical = canonical_command(
        f, snap, saved.output_hash, dict(blocks=[dict(kind="text", candidate_id=run.candidates[0].id)])
    )
    canon_result = call(f, editor, canonical)
    assert canon_result.status == "applied"
    binding, binding_hash, expression, manifestation = bind(f, artifact)
    review = review_command(f, binding, binding_hash, canonical, canon_result, run.candidates)
    reviewed = approve(f, editor, review)
    assert reviewed.status == "applied", reviewed
    cmd = approval_command(f, review, reviewed)
    return (
        f,
        rights,
        worker,
        editor,
        maintainer,
        acq,
        artifact,
        run,
        snap,
        canonical,
        review,
        reviewed,
        cmd,
        expression,
    )


def test_approval_mints_one_private_version_with_exact_bytes_nodes_anchors_and_order(ready):
    f, _, worker, _, maintainer, *rest = ready
    cmd = rest[-2]
    expression = rest[-1]
    result = approve(f, maintainer, cmd)
    assert result.status == "applied", result
    assert approve(f, maintainer, cmd).replayed
    assert (
        approve(
            f,
            maintainer,
            cmd.model_copy(
                update={
                    "command_id": uuid4(),
                    "version_id": "ver_" + str(uuid4()),
                    "representation_id": "prp_" + str(uuid4()),
                }
            ),
        ).version_id
        == result.version_id
    )
    with worker.engine.begin() as conn:
        view = SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE)
        assert view and view.expression_id == expression and view.canonical_text.encode() == RAW + b"\n"
        assert view.content_hash == sha256(RAW + b"\n").hexdigest()
        assert view.publication_eligible is False
    assert (
        len(rows(f, "approved_version"))
        == len(rows(f, "version_node"))
        == len(rows(f, "approved_anchor"))
        == len(rows(f, "document_order"))
        == 1
    )
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM identity_observations")) == 0


def build_proposal(ready, candidates, plan, *, raw=RAW, expression=None):
    from test_postgres_acquisition import assessment

    f, rights, worker, editor, *_ = ready
    acq = assessment(f, derive="allow", artifact_hash=sha256(raw).hexdigest())
    assert apply(f, rights, acq).status == "applied"
    artifact, _ = stage(f, worker, acq, private_bytes=raw)
    run = run_command(f, artifact, acq, candidates=candidates).model_copy(
        update={"artifact_hash": sha256(raw).hexdigest()}
    )
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    canonical = canonical_command(f, snap, saved.output_hash, plan)
    canonical_result = call(f, editor, canonical)
    assert canonical_result.status == "applied", canonical_result
    binding, binding_hash, expression, manifestation = bind(f, artifact, expression=expression)
    review = review_command(f, binding, binding_hash, canonical, canonical_result, run.candidates)
    return artifact, run, snap, canonical, review, expression


@pytest.mark.parametrize("shape", ["merged", "empty", "footnotes", "notice"])
def test_approved_structures_preserve_source_derived_ranges_and_geometry_unavailability(ready, shape):
    from test_canonical import leaf

    f, _, worker, editor, maintainer, *_ = ready
    if shape in {"merged", "empty"}:
        candidates = [
            leaf(
                101,
                "Amount",
                block_type="table_cell",
                cell=dict(table_local_id="t", row=0, column=0, column_span=2),
            ),
            leaf(
                102,
                "" if shape == "empty" else "1,000.00 not waived",
                block_type="table_cell",
                cell=dict(table_local_id="t", row=1, column=0, column_span=2),
                content_state="empty" if shape == "empty" else "present",
            ),
        ]
        plan = dict(
            blocks=[
                dict(
                    kind="table",
                    table_local_id="t",
                    rows=2,
                    columns=2,
                    cells=[dict(candidates=[c.id]) for c in candidates],
                    headers=[candidates[0].id],
                )
            ]
        )
    else:
        candidates = [
            leaf(101, "Original [b] then [a]."),
            leaf(102, "Note A", block_type="footnote"),
            leaf(103, "Note B", block_type="footnote"),
        ]
        scope = dict(
            blocks=[dict(kind="text", candidate_id=candidates[0].id)],
            footnotes=[
                dict(local_id="a", candidates=[candidates[1].id]),
                dict(local_id="b", candidates=[candidates[2].id]),
            ],
            markers=[
                dict(candidate_id=candidates[0].id, source_start=9, source_end=12, footnote_id="b"),
                dict(candidate_id=candidates[0].id, source_start=18, source_end=21, footnote_id="a"),
            ],
        )
        plan = (
            scope if shape == "footnotes" else dict(blocks=[dict(kind="notice", local_id="notice1", **scope)])
        )
    *_, review, _ = build_proposal(ready, candidates, plan)
    compared = approve(f, editor, review)
    assert compared.status == "applied", compared
    cmd = approval_command(f, review, compared)
    result = approve(f, maintainer, cmd)
    assert result.status == "applied", result
    with worker.engine.begin() as conn:
        view = SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE)
    assert view
    nodes = [r for r in rows(f, "version_node") if r["version_id"] == result.version_id]
    for node in nodes:
        if node["start_byte"] is not None:
            span = view.canonical_text.encode()[node["start_byte"] : node["end_byte"]]
            assert sha256(span).hexdigest() == node["span_hash"]
            span.decode()
    mappings = [r for r in rows(f, "unavailable_mapping") if r["version_id"] == result.version_id]
    assert len(mappings) == len(nodes) and all(r["quality"] == "unavailable" for r in mappings)
    if shape in {"merged", "empty"}:
        cells = rows(f, "approved_cell")
        assert len(cells) == 2 and all(r["column_span"] == 2 for r in cells)
    else:
        assert len(rows(f, "footnote_reference")) == 2
        assert view.canonical_text.endswith("Note B\n\nNote A\n")


@pytest.mark.parametrize(
    "kind", ["rights", "content", "worker", "release_without_acquire", "other_collection", "revoked"]
)
def test_private_approval_requires_scoped_native_release_and_acquisition(ready, operators, kind):
    from rights.operators import revoke_operator

    f, rights, worker, editor, maintainer, *rest = ready
    _, create, _ = operators
    subject = {"rights": rights, "content": editor, "worker": worker}.get(kind)
    if kind == "release_without_acquire":
        subject = create("release")
    if kind == "other_collection":
        subject = create("release", assignments=((f.other_collection, "release"), (f.collection, "acquire")))
    if kind == "revoked":
        subject = maintainer
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, maintainer.actor, reason="Original synthetic revocation")
    assert approve(f, subject, rest[-2]).status == "forbidden"
    assert not rows(f, "approved_version")


@pytest.mark.parametrize(
    "field",
    [
        "snapshot_hash",
        "profile_hash",
        "content_hash",
        "binding_hash",
        "reviewed_candidate_ids",
        "evidence_id",
    ],
)
def test_snapshot_comparison_rejects_unpinned_or_incomplete_review(ready, field):
    f, _, _, editor, *rest = ready
    review = rest[-4]
    value = (
        uuid4() if field == "evidence_id" else (uuid4(),) if field == "reviewed_candidate_ids" else "f" * 64
    )
    changed = review.model_copy(update={field: value, "command_id": uuid4(), "review_id": uuid4()})
    assert approve(f, editor, changed).status in {"forbidden", "validation_failed"}
    assert len(rows(f, "snapshot_review", "staging")) == 1


def test_staging_edit_invalidates_uncommitted_review_before_mint(ready):
    f, _, _, editor, maintainer, _, _, run, snap, canonical, review, reviewed, cmd, _ = ready
    edit = snapshot_command(
        f, run, revision=1, parent_hash=canonical.snapshot_hash, snapshot_id=snap.snapshot_id
    )
    assert apply_assembly(f, editor, edit).status == "applied"
    assert approve(f, maintainer, cmd).status == "forbidden"
    assert not rows(f, "approved_version")


@pytest.mark.parametrize("change", ["rights", "verification", "rights_actor", "content_actor"])
def test_superseded_or_revoked_reviews_deny_approval_and_private_bytes(ready, change):
    from rights.operators import revoke_operator

    f, rights, worker, editor, maintainer, *rest = ready
    cmd = rest[-2]
    result = approve(f, maintainer, cmd)
    assert result.status == "applied"
    if change == "rights":
        newer(f, rights, decision_command(f), redistribute_text="deny")
    elif change == "verification":
        newer(f, editor, verification_command(f), state="revoked")
    else:
        with f.engine.begin() as conn:
            revoke_operator(
                conn,
                f.ns,
                (rights if change == "rights_actor" else editor).actor,
                reason="Original synthetic revoke",
            )
    assert approve(f, maintainer, cmd).status == "forbidden"
    with worker.engine.begin() as conn:
        assert SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE) is None


def revised_review(ready, run, snap, canonical, plan, review):
    f, _, _, editor, *_ = ready
    edit = snapshot_command(
        f,
        run,
        revision=snap.expected_revision + 1,
        parent_hash=canonical.snapshot_hash,
        snapshot_id=snap.snapshot_id,
    )
    saved = apply_assembly(f, editor, edit)
    assert saved.status == "applied"
    proposal = canonical_command(f, edit, saved.output_hash, plan)
    output = call(f, editor, proposal)
    assert output.status == "applied"
    revised = review.model_copy(
        update={
            "command_id": uuid4(),
            "review_id": uuid4(),
            "proposal_id": proposal.proposal_id,
            "snapshot_hash": proposal.snapshot_hash,
            "content_hash": output.output_hash,
        }
    )
    compared = approve(f, editor, revised)
    assert compared.status == "applied", compared
    return edit, proposal, revised, compared


def test_header_only_representation_revision_preserves_bytes_nodes_and_anchor_ids(ready):
    from test_canonical import leaf

    f, _, worker, editor, maintainer, *_ = ready
    candidates = [
        leaf(101, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
        leaf(
            102, "Literal amount 10", block_type="table_cell", cell=dict(table_local_id="t", row=1, column=0)
        ),
    ]
    plan = dict(
        blocks=[
            dict(
                kind="table",
                table_local_id="t",
                rows=2,
                columns=1,
                cells=[dict(candidates=[c.id]) for c in candidates],
                headers=[candidates[0].id],
            )
        ]
    )
    _, run, snap, canonical, review, _ = build_proposal(ready, candidates, plan)
    compared = approve(f, editor, review)
    initial = approve(f, maintainer, approval_command(f, review, compared))
    assert initial.status == "applied"
    nodes = rows(f, "version_node")
    anchors = rows(f, "approved_anchor")
    version = rows(f, "approved_version")
    changed = json.loads(json.dumps(plan, default=str))
    changed["blocks"][0]["headers"] = []
    _, _, review, compared = revised_review(ready, run, snap, canonical, changed, review)
    cmd = approval_command(
        f,
        review,
        compared,
        version_id=initial.version_id,
        prior_version_id=initial.version_id,
        expected_rep_revision=1,
        mode="representation_revision",
    )
    result = approve(f, maintainer, cmd)
    assert result.status == "applied" and result.revision == 2, result
    assert (
        rows(f, "version_node") == nodes
        and rows(f, "approved_anchor") == anchors
        and rows(f, "approved_version") == version
    )
    assert len(rows(f, "representation_revision")) == 2
    with worker.engine.begin() as conn:
        view = SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE)
        assert view and view.current_rep_id == result.representation_id and view.rep_revision == 2


def test_byte_correction_mints_new_version_and_anchors_without_rewriting_history(ready):
    f, _, worker, editor, maintainer, *rest = ready
    initial = approve(f, maintainer, rest[-2])
    expression = rest[-1]
    assert initial.status == "applied"
    original = rows(f, "approved_version")
    anchors = rows(f, "approved_anchor")
    from test_canonical import leaf, texts

    raw = "New original synthetic text. No amount waived. café 😀".encode()
    candidates = [leaf(101, raw.decode())]
    *_, review, _ = build_proposal(ready, candidates, texts(candidates), raw=raw, expression=expression)
    compared = approve(f, editor, review)
    changed = approval_command(
        f, review, compared, prior_version_id=initial.version_id, expected_rep_revision=1
    )
    result = approve(f, maintainer, changed)
    assert result.status == "applied" and result.version_id != initial.version_id, result
    assert original[0] in rows(f, "approved_version") and anchors[0] in rows(f, "approved_anchor")
    assert len(rows(f, "approved_version")) == len(rows(f, "approved_anchor")) == 2
    with worker.engine.begin() as conn:
        assert (
            SyntheticApproval(conn, f.ns).read(initial.version_id, purpose=PURPOSE).canonical_text.encode()
            == RAW + b"\n"
        )
        assert (
            SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE).canonical_text.encode()
            == raw + b"\n"
        )


def test_identical_new_snapshot_cannot_silently_mint_a_duplicate_byte_version(ready):
    f, _, _, editor, maintainer, _, _, run, snap, canonical, review, _, cmd, _ = ready
    initial = approve(f, maintainer, cmd)
    plan = canonical.plan.model_dump(mode="json")
    _, _, review, compared = revised_review(ready, run, snap, canonical, plan, review)
    changed = approval_command(
        f, review, compared, prior_version_id=initial.version_id, expected_rep_revision=1
    )
    assert approve(f, maintainer, changed).status == "validation_failed"
    assert len(rows(f, "approved_version")) == 1


@pytest.mark.parametrize("fault", ["caller_rollback", "domain_insert"])
def test_approval_failure_rolls_back_record_set_audit_heads_and_receipt(ready, fault):
    f, _, _, _, maintainer, *rest = ready
    cmd = rest[-2]
    if fault == "caller_rollback":
        with maintainer.engine.connect() as conn:
            tx = conn.begin()
            result = SyntheticApproval(conn, f.ns).apply(cmd)
            assert result.status == "applied"
            tx.rollback()
    else:
        with f.engine.begin() as conn:
            fn = f.ns.qualified(conn, "policy", "reject_test_anchor")
            conn.execute(
                text(
                    f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Original synthetic injected mint failure'; END $$"
                )
            )
            conn.execute(
                text(
                    f"CREATE TRIGGER fail_test BEFORE INSERT ON {f.ns.qualified(conn, 'corpus', 'approved_anchor')} FOR EACH ROW EXECUTE FUNCTION {fn}()"
                )
            )
        assert approve(f, maintainer, cmd).status == "unavailable"
    for table in (
        "approved_version",
        "representation_revision",
        "version_node",
        "approved_anchor",
        "version_head",
        "representation_head",
        "node_alignment",
    ):
        assert not rows(f, table)
    assert not rows(f, "approval_commit", "policy")
    assert len(rows(f, "approval_receipt", "policy")) == 1
    if fault == "domain_insert":
        with f.engine.begin() as conn:
            conn.execute(
                text(f"DROP TRIGGER fail_test ON {f.ns.qualified(conn, 'corpus', 'approved_anchor')}")
            )
    assert approve(f, maintainer, cmd).status == "applied"


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_approval_mints_one_record_set_without_duplicate_nodes(ready, identical):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f, _, _, _, maintainer, *rest = ready
    cmd = rest[-2]
    other = (
        cmd
        if identical
        else cmd.model_copy(
            update={
                "command_id": uuid4(),
                "version_id": "ver_" + str(uuid4()),
                "representation_id": "prp_" + str(uuid4()),
            }
        )
    )
    barrier = Barrier(2)

    def submit(command):
        barrier.wait(timeout=10)
        return approve(f, maintainer, command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (cmd, other)))
    assert all(r.status == "applied" for r in results), results
    assert results[0].version_id == results[1].version_id and sum(r.replayed for r in results) == 1
    for table in ("approved_version", "representation_revision", "version_node", "approved_anchor"):
        assert len(rows(f, table)) == 1


def test_erasure_clears_every_new_payload_and_keeps_immutable_range_hash_metadata(ready):
    from rights.acquisition import SyntheticAcquisition

    f, _, worker, _, maintainer, _, artifact, *rest = ready
    result = approve(f, maintainer, rest[-2])
    assert result.status == "applied"
    anchors = rows(f, "approved_anchor")
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    for schema, table in [
        ("staging", "synthetic_expression_binding"),
        ("staging", "snapshot_review"),
        ("corpus", "approved_version"),
        ("corpus", "representation_revision"),
    ]:
        row = rows(f, table, schema)[0]
        assert row["state"] == "erased" and row["payload"] is None
        if table == "approved_version":
            assert row["canonical_bytes"] is None
    assert rows(f, "approved_anchor") == anchors
    with worker.engine.begin() as conn:
        assert SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE) is None
    with f.engine.connect() as conn:
        fn = f.ns.qualified(conn, "policy", "staging_live_bytes")
        assert conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": f.collection}) == 0


def test_stale_collection_materializer_holds_approved_bytes_before_explicit_erasure(ready):
    from test_postgres_assembly import reconcile

    f, rights, worker, _, maintainer, *rest = ready
    result = approve(f, maintainer, rest[-2])
    assert result.status == "applied"
    newer(f, rights, decision_command(f), redistribute_text="deny")
    assert reconcile(f, worker).held == 2
    assert rows(f, "approved_version")[0]["state"] == "held"
    assert rows(f, "representation_revision")[0]["state"] == "held"
    assert rows(f, "approved_version")[0]["canonical_bytes"] == RAW + b"\n"


def test_freeze_denies_positive_mint_and_still_allows_whole_family_erasure(ready):
    from api.identity import freeze
    from rights.acquisition import SyntheticAcquisition

    f, _, worker, _, maintainer, _, artifact, *rest = ready
    with f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert approve(f, maintainer, rest[-2]).status in {"forbidden", "unavailable"}
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert rows(f, "snapshot_review", "staging")[0]["payload"] is None


@pytest.mark.parametrize("ready", [4000], indirect=True)
def test_declared_budget_denies_whole_mint_without_partial_storage_or_receipt(ready):
    f, _, _, _, maintainer, *rest = ready
    before = rows(f, "approval_receipt", "policy")
    audit = rows(f, "audit_event", "policy")
    assert approve(f, maintainer, rest[-2]).status == "capacity_exceeded"
    assert not rows(f, "approved_version") and not rows(f, "version_node")
    assert rows(f, "approval_receipt", "policy") == before and rows(f, "audit_event", "policy") == audit


def test_all_new_payloads_count_toward_shared_raw_and_derived_budget(ready):
    f, _, _, _, maintainer, *rest = ready
    assert approve(f, maintainer, rest[-2]).status == "applied"
    with f.engine.connect() as conn:
        total = conn.scalar(
            text(f"SELECT {f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(:cid)"),
            {"cid": f.collection},
        )
        counted = 0
        for schema, table, columns in [
            ("staging", "staged_artifact", ["private_bytes"]),
            ("staging", "adapter_candidate", ["payload"]),
            ("staging", "staging_snapshot", ["payload"]),
            ("staging", "canonical_proposal", ["canonical_bytes", "payload"]),
            ("staging", "synthetic_expression_binding", ["payload"]),
            ("staging", "snapshot_review", ["payload"]),
            ("corpus", "approved_version", ["canonical_bytes", "payload"]),
            ("corpus", "representation_revision", ["payload"]),
        ]:
            terms = [
                "octet_length(payload::text)" if field == "payload" else "octet_length(" + field + ")"
                for field in columns
            ]
            counted += conn.scalar(
                text(f"SELECT coalesce(sum({'+'.join(terms)}),0) FROM {f.ns.qualified(conn, schema, table)}")
            )
        assert counted == total and counted > len(RAW)


def test_erasure_injected_failure_restores_raw_bytes_all_new_payloads_and_audit(ready):
    from rights.acquisition import AcquisitionError, SyntheticAcquisition

    f, _, worker, _, maintainer, _, artifact, *rest = ready
    assert approve(f, maintainer, rest[-2]).status == "applied"
    before = {
        (schema, table): rows(f, table, schema)
        for schema, table in [
            ("staging", "staged_artifact"),
            ("staging", "canonical_proposal"),
            ("staging", "synthetic_expression_binding"),
            ("staging", "snapshot_review"),
            ("corpus", "approved_version"),
            ("corpus", "representation_revision"),
            ("policy", "audit_event"),
            ("policy", "assembly_lifecycle_event"),
        ]
    }
    with f.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "reject_test_erasure")
        conn.execute(
            text(
                f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Original synthetic erasure fault'; END $$"
            )
        )
        conn.execute(
            text(
                f"CREATE TRIGGER fail_test BEFORE UPDATE ON {f.ns.qualified(conn, 'corpus', 'representation_revision')} FOR EACH ROW EXECUTE FUNCTION {fn}()"
            )
        )
    with pytest.raises(AcquisitionError), worker.engine.begin() as conn:
        SyntheticAcquisition(conn, f.ns).erase(artifact)
    for (schema, table), records in before.items():
        assert rows(f, table, schema) == records


def test_one_native_actor_holding_both_review_roles_cannot_supply_independent_approval(ready, operators):
    f, _, _, _, maintainer, _, _, _, _, _, review, _, _, _ = ready
    _, create, _ = operators
    dual = create(
        "content",
        assignments=((f.collection, "rights"), (f.collection, "content"), (f.collection, "acquire")),
    )
    decision = decision_command(f, expected_revision=1)
    decision = decision.model_copy(
        update={
            "payload": decision.payload.model_copy(
                update={"redistribute_text": "allow", "derive": "allow", "quote": "allow"}
            )
        }
    )
    assert apply(f, dual, decision).status == "applied"
    assert apply(f, dual, verification_command(f, expected_revision=1)).status == "applied"
    review = review.model_copy(
        update={"command_id": uuid4(), "review_id": uuid4(), "verification_revision": 2}
    )
    compared = approve(f, dual, review)
    assert compared.status == "applied"
    cmd = approval_command(f, review, compared, rights_revision=2, verification_revision=2)
    assert approve(f, maintainer, cmd).status == "forbidden"
    assert not rows(f, "approved_version")


@pytest.mark.parametrize(
    "table,field",
    [
        ("identity_works", "title"),
        ("identity_expressions", "edition_key"),
        ("identity_manifestations", "provenance_reference"),
    ],
)
def test_bound_identity_facts_cannot_change_under_frozen_review(ready, table, field):
    from sqlalchemy.exc import DBAPIError

    f, *_ = ready
    with pytest.raises(DBAPIError) as failure, f.engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET {field}='Changed fictional identity'"))
    assert failure.value.orig.sqlstate == "55000"


def test_review_and_release_subjects_cannot_declare_identity_or_select_private_versions(ready):
    from sqlalchemy.exc import DBAPIError

    f, _, worker, editor, maintainer, _, artifact, *rest = ready
    assert approve(f, maintainer, rest[-2]).status == "applied"
    for subject in (worker, editor, maintainer):
        with pytest.raises(ValueError), subject.engine.begin() as conn:
            declare_synthetic_binding(
                conn,
                f.ns,
                binding_id=uuid4(),
                expression_id=rest[-1],
                manifestation_id=mint("man"),
                artifact_id=artifact,
                evidence_id=f.evidence,
                reason="Original synthetic forbidden declaration",
            )
        with pytest.raises(DBAPIError), subject.engine.begin() as conn:
            conn.execute(
                text(f"SELECT canonical_bytes FROM {f.ns.qualified(conn, 'corpus', 'approved_version')}")
            )
    with f.as_role("serving") as conn:
        from rights.database import PolicyRepository

        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_text").allowed


def test_native_approval_cli_prints_only_safe_metadata(ready):
    import os
    import subprocess
    import sys

    f, _, _, _, maintainer, *rest = ready
    cmd = rest[-2]
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=maintainer.engine.url.render_as_string(hide_password=False)
    )
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--approval", "--schema", f.ns.base],
        input=cmd.model_dump_json(),
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout
    parsed = json.loads(result.stdout)
    assert parsed["status"] == "applied" and parsed["version_id"] == cmd.version_id
    assert (
        RAW.decode() not in result.stdout + result.stderr
        and maintainer.password not in result.stdout + result.stderr
    )


def test_migration_with_existing_canonical_payloads_restores_exact_helpers_when_empty(projection):
    from alembic import command
    from alembic.config import Config
    from test_postgres import migrate

    f, *_ = projection
    before = rows(f, "canonical_proposal", "staging")
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0008")
        definitions = {
            fn: conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                {"fn": f.ns.schema("policy") + "." + fn},
            )
            for fn in [
                "staging_live_bytes(text)",
                "reserve_assembly_capacity(uuid,text,bigint,integer)",
                "transition_assembly(uuid,text,text)",
                "apply_assembly_command(jsonb)",
            ]
        }
    migrate(f.engine)
    assert rows(f, "canonical_proposal", "staging") == before
    with f.engine.connect() as conn:
        for fn, definition in definitions.items():
            alias = fn.replace("(", "_m32(", 1)
            assert (
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                    {"fn": f.ns.schema("policy") + "." + alias},
                )
                == definition
            )


def test_nonempty_approval_foundation_refuses_downgrade_without_losing_review(ready):
    from alembic import command
    from alembic.config import Config

    f, *_ = ready
    with f.engine.connect() as conn:
        before = conn.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0008")
    assert rows(f, "snapshot_review", "staging")[0]["payload"] is not None
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == before


@pytest.mark.parametrize("change", ["expiry", "snapshot_edit"])
def test_authority_changes_before_caller_commit_roll_back_entire_approval(ready, change):
    from datetime import timedelta
    from time import sleep

    from sqlalchemy.exc import DBAPIError

    f, _, _, editor, maintainer, _, _, run, snap, canonical, _, _, cmd, _ = ready
    if change == "expiry":
        # Synthetic collection record is the short-lived authority; issue a fresh exact review.
        rights = ready[1]
        with f.engine.connect() as conn:
            deadline = conn.scalar(text("SELECT clock_timestamp()")) + timedelta(seconds=4)
        decision = decision_command(f, expected_revision=1)
        decision = decision.model_copy(
            update={
                "payload": decision.payload.model_copy(
                    update={
                        "expires_at": deadline,
                        "redistribute_text": "allow",
                        "derive": "allow",
                        "quote": "allow",
                    }
                )
            }
        )
        assert apply(f, rights, decision).status == "applied"
        cmd = cmd.model_copy(update={"rights_revision": 2})
    if change == "snapshot_edit":
        with f.engine.begin() as admin:
            quote = admin.dialect.identifier_preparer.quote_identifier
            admin.execute(
                text(
                    f"INSERT INTO {f.ns.qualified(admin, 'policy', 'assignment')}(actor_id,collection_id,capability) VALUES(:actor,:cid,'content')"
                ),
                {"actor": maintainer.actor, "cid": f.collection},
            )
            admin.execute(text(f"GRANT {quote(f.ns.role('review'))} TO {quote(maintainer.role)}"))
    with maintainer.engine.connect() as conn:
        tx = conn.begin()
        result = SyntheticApproval(conn, f.ns).apply(cmd)
        assert result.status == "applied", result
        if change == "expiry":
            remaining = (
                deadline - __import__("datetime").datetime.now(__import__("datetime").UTC)
            ).total_seconds()
            if remaining > 0:
                sleep(remaining + 0.05)
        else:
            edit = snapshot_command(
                f, run, revision=1, parent_hash=canonical.snapshot_hash, snapshot_id=snap.snapshot_id
            )
            from rights.assembly import SyntheticAssembly

            assert SyntheticAssembly(conn, f.ns).apply(edit).status == "applied"
        with pytest.raises(DBAPIError) as failure:
            tx.commit()
        assert failure.value.orig.sqlstate == "42501"
    assert (
        not rows(f, "approved_version")
        and not rows(f, "approved_anchor")
        and not rows(f, "approval_commit", "policy")
    )
    assert len(rows(f, "approval_receipt", "policy")) == 1


def test_mint_transaction_locks_collection_revision_until_commit(ready):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    f, rights, worker, _, maintainer, *rest = ready
    started = Event()

    def supersede():
        started.set()
        return newer(f, rights, decision_command(f), redistribute_text="deny")

    with maintainer.engine.connect() as conn:
        tx = conn.begin()
        result = SyntheticApproval(conn, f.ns).apply(rest[-2])
        assert result.status == "applied"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supersede)
            assert started.wait(5)
            sleep(0.2)
            assert not future.done()
            tx.commit()
            future.result(timeout=10)
    with worker.engine.begin() as conn:
        assert SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE) is None


@pytest.mark.parametrize("corruption", ["guard_create", "guard_inheritance", "function_owner"])
def test_approval_upgrade_rejects_existing_boundary_corruption_before_ddl(projection, corruption):
    from alembic import command
    from alembic.config import Config
    from test_postgres import migrate

    f, *_ = projection
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0008")
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
                    f"ALTER FUNCTION {f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(text) OWNER TO {owner}"
                )
            )
        with pytest.raises(RuntimeError, match="boundary review"):
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        assert (
            conn.scalar(
                text("SELECT to_regclass(:name)"), {"name": f.ns.schema("corpus") + ".approved_version"}
            )
            is None
        )
        tx.rollback()
    migrate(f.engine)


@pytest.mark.parametrize("bad", ["span_hash", "span_outside", "partial_null", "cycle", "source_mismatch"])
def test_database_rejects_invalid_version_node_integrity(ready, bad):
    from sqlalchemy.exc import DBAPIError

    f, _, _, _, maintainer, *rest = ready
    assert approve(f, maintainer, rest[-2]).status == "applied"
    node = rows(f, "version_node")[0]
    node.update(id="nod_" + str(uuid4()), local_key="synthetic-extra")
    if bad == "span_hash":
        node["span_hash"] = "f" * 64
    elif bad == "span_outside":
        node["end_byte"] = 1000000
    elif bad == "partial_null":
        node["end_byte"] = None
    elif bad == "cycle":
        node["parent_id"] = node["id"]
    else:
        node["end_byte"] = 8
        node["span_hash"] = sha256(RAW[:8]).hexdigest()
    if bad != "source_mismatch":
        node["candidate_id"] = None
        node["node_kind"] = "notice"
    with pytest.raises(DBAPIError) as failure, f.engine.begin() as conn:
        columns = ",".join(node)
        values = ",".join(":" + key for key in node)
        conn.execute(
            text(f"INSERT INTO {f.ns.qualified(conn, 'corpus', 'version_node')}({columns}) VALUES({values})"),
            node,
        )
    assert failure.value.orig.sqlstate in {"23514", "23503"}
