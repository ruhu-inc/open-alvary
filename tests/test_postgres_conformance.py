"""Combined native M3 contract evidence using only original synthetic source material."""

import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_canonical import leaf
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE
from test_postgres_approval import approval_command, approve, bind, review_command, revised_review, rows
from test_postgres_assembly import apply_assembly, reconcile, run_command, snapshot_command
from test_postgres_canonical import call, canonical_command
from test_postgres_gazette import dispatch, item_approval, item_identity, item_review, make_gazette
from test_postgres_lifecycle import newer
from test_postgres_operators import apply, decision_command, verification_command
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.acquisition import SyntheticAcquisition
from rights.approval import SyntheticApproval
from rights.gazette import SyntheticGazette
from schema.identity import mint

pytestmark = pytest.mark.postgres


def scenario():
    candidates = [
        leaf(301, "  Cafe\u0301 لا\u2067RTL\u2069 😀 [n].\r\n"),
        leaf(
            302,
            "Amount",
            block_type="table_cell",
            cell=dict(table_local_id="t", row=0, column=0, column_span=2),
        ),
        leaf(
            303,
            "",
            block_type="table_cell",
            content_state="empty",
            cell=dict(table_local_id="t", row=0, column=2),
        ),
        leaf(
            304,
            "1,000.00 not waived",
            block_type="table_cell",
            cell=dict(table_local_id="t", row=1, column=0),
        ),
        leaf(305, "No", block_type="table_cell", cell=dict(table_local_id="t", row=1, column=1)),
        leaf(
            306,
            "",
            block_type="table_cell",
            content_state="empty",
            cell=dict(table_local_id="t", row=1, column=2),
        ),
        leaf(
            307,
            "Amount",
            block_type="table_cell",
            cell=dict(table_local_id="t", row=0, column=0, column_span=2),
        ),
        leaf(308, "First footnote — café.", block_type="footnote"),
        leaf(309, "Second notice: 2030-01-02 is not commencement [n]."),
        leaf(310, "Second footnote لا.", block_type="footnote"),
        leaf(311, "Third notice: § 3 — literal cross-reference."),
    ]

    def notice(key, candidate, **extra):
        return dict(
            kind="notice", local_id=key, blocks=[dict(kind="text", candidate_id=candidate.id)], **extra
        )

    def foot(candidate, target):
        start = candidate.text.encode().index(b"[n]")
        return dict(
            footnotes=[dict(local_id="n", candidates=[target.id])],
            markers=[
                dict(candidate_id=candidate.id, source_start=start, source_end=start + 3, footnote_id="n")
            ],
        )

    one = notice("one", candidates[0], **foot(candidates[0], candidates[7]))
    one["blocks"].append(
        dict(
            kind="table",
            table_local_id="t",
            rows=2,
            columns=3,
            cells=[dict(candidates=[c.id]) for c in candidates[1:6]],
            headers=[candidates[1].id],
        )
    )
    plan = dict(
        blocks=[
            one,
            notice("two", candidates[8], **foot(candidates[8], candidates[9])),
            notice("three", candidates[10]),
        ],
        exclusions=[
            dict(
                candidate_id=candidates[6].id,
                kind="repeated_header",
                target_candidate_id=candidates[1].id,
                reason="Original synthetic repeated visual header",
            )
        ],
    )
    return candidates, plan


EXPECTED = (
    "  Café لا\u2067RTL\u2069 😀 [n].\n\n\n"
    "Amount\t\t\n1,000.00 not waived\tNo\t\n\n"
    "First footnote — café.\n\n"
    "Second notice: 2030-01-02 is not commencement [n].\n\n"
    "Second footnote لا.\n\n"
    "Third notice: § 3 — literal cross-reference.\n"
).encode()


@pytest.fixture
def three_notice(operators, request):
    g = make_gazette(operators, maximum=getattr(request, "param", 100 * 1024 * 1024), scenario=scenario())
    items = [g]
    for key in ("two", "three"):
        cid = mint("scp")
        evidence = uuid4()
        with g.f.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key) VALUES(:id,'ng','act',:provider)"
                ),
                dict(id=cid, provider="original-synthetic-" + key),
            )
            conn.execute(
                text(
                    f"INSERT INTO {g.f.ns.qualified(conn, 'policy', 'evidence')}(id,collection_id,private_reference,evidence_hash,observed_at) VALUES(:id,:cid,'private://original-synthetic-item',:hash,:now)"
                ),
                dict(id=evidence, cid=cid, hash="b" * 64, now=g.f.now),
            )
            for subject, caps in (
                (g.rights, ("rights",)),
                (g.editor, ("content", "acquire")),
                (g.maintainer, ("release", "acquire")),
                (g.worker, ("acquire",)),
            ):
                for cap in caps:
                    conn.execute(
                        text(
                            f"INSERT INTO {g.f.ns.qualified(conn, 'policy', 'assignment')}(actor_id,collection_id,capability) VALUES(:actor,:cid,:cap)"
                        ),
                        dict(actor=subject.actor, cid=cid, cap=cap),
                    )
        item = replace(
            g, item_collection=cid, item_evidence=evidence, binding=None, binding_hash="", review=None
        )
        rights = decision_command(g.f).model_copy(update={"collection_id": cid})
        rights = rights.model_copy(
            update={
                "payload": rights.payload.model_copy(
                    update=dict(
                        evidence_id=evidence,
                        redistribute_text="allow",
                        quote="allow",
                        derive="allow",
                        retain="allow",
                    )
                )
            }
        )
        assert apply(g.f, g.rights, rights).status == "applied"
        verification = verification_command(g.f).model_copy(update={"collection_id": cid})
        verification = verification.model_copy(
            update={
                "payload": verification.payload.model_copy(
                    update={
                        "evidence_id": evidence,
                        "materials": tuple(
                            m.model_copy(update={"competence_evidence_id": evidence})
                            for m in verification.payload.materials
                        ),
                    }
                )
            }
        )
        assert apply(g.f, g.editor, verification).status == "applied"
        item.binding, item.binding_hash = item_identity(item, "notice:" + key)
        item.review = item_review(item)
        items.append(item)
    return g, items


def publish(items, revision=0):
    results = []
    for item in items:
        review = item_review(item, expected_revision=revision)
        compared = dispatch(item, item.editor, review)
        assert compared.status == "applied", compared
        cmd = item_approval(item, review, compared)
        result = dispatch(item, item.maintainer, cmd)
        assert result.status == "applied", result
        results.append((cmd, result))
    return results


def read_all(g, results):
    with g.worker.engine.begin() as conn:
        issue = SyntheticApproval(conn, g.f.ns).read(g.version.version_id, purpose=PURPOSE)
        projections = [
            SyntheticGazette(conn, g.f.ns).read(result.correspondence_id, purpose=PURPOSE)
            for _, result in results
        ]
    return issue, projections


def test_three_independent_notices_exact_projection_correction_withholding_and_erasure(three_notice):
    g, items = three_notice
    f = g.f
    results = publish(items[:2])
    assert read_all(g, results) == (None, [None, None])  # Pending third notice blocks the owning bytes.
    results += publish(items[2:])
    issue, projections = read_all(g, results)
    assert (
        issue
        and issue.canonical_text.encode() == EXPECTED
        and issue.content_hash == sha256(EXPECTED).hexdigest()
    )
    assert issue.current_rep_id.startswith("rep_")
    assert len(rows(f, "approved_version")) == 1 and len(rows(f, "gazette_correspondence")) == 3
    assert len({p.item_work_id for p in projections}) == 3
    assert len({p.item_expression_id for p in projections}) == 3
    assert len({p.anchor_id for p in projections}) == 3
    for p in projections:
        span = p.ranges[0]
        assert p.projected_text.encode() == EXPECTED[span.start_byte : span.end_byte]
        assert sha256(p.projected_text.encode()).hexdigest() == span.span_hash and not p.publication_eligible
        assert p.issue_version_id == issue.version_id
    assert projections[0].projected_text.count("Amount") == 1
    assert (
        "Second footnote" not in projections[0].projected_text
        and "First footnote" not in projections[1].projected_text
    )
    cells = rows(f, "approved_cell")
    assert len(cells) == 5 and sum(c["content_state"] == "empty" for c in cells) == 2
    nodes, anchors, versions = (
        rows(f, "version_node"),
        rows(f, "approved_anchor"),
        rows(f, "approved_version"),
    )
    run, snap, canonical, owner_review, plan = g.source
    changed = json.loads(json.dumps(plan, default=str))
    changed["blocks"][0]["blocks"][1]["headers"] = [run.candidates[1].id, run.candidates[4].id]
    _, _, review, compared = revised_review(
        (f, None, g.worker, g.editor), run, snap, canonical, changed, owner_review
    )
    correction = approval_command(
        f,
        review,
        compared,
        version_id=issue.version_id,
        prior_version_id=issue.version_id,
        expected_rep_revision=1,
        mode="representation_revision",
    )
    revised = approve(f, g.maintainer, correction)
    assert revised.status == "applied" and revised.revision == 2
    assert (
        rows(f, "version_node") == nodes
        and rows(f, "approved_anchor") == anchors
        and rows(f, "approved_version") == versions
    )
    assert read_all(g, results) == (None, [None, None, None])
    fresh = publish(items, revision=1)
    current, views = read_all(g, fresh)
    assert (
        current
        and current.canonical_text.encode() == EXPECTED
        and all(p.representation_id == revised.representation_id for p in views)
    )
    # Only the middle item's collection is withdrawn. No sibling can bypass the owning issue.
    middle = items[1]
    old = decision_command(f).model_copy(update={"collection_id": middle.item_collection})
    old = old.model_copy(
        update={"payload": old.payload.model_copy(update={"evidence_id": middle.item_evidence})}
    )
    newer(f, g.rights, old, redistribute_text="deny")
    assert read_all(g, fresh) == (None, [None, None, None])
    assert reconcile(f, g.worker).held >= 2
    assert (
        rows(f, "approved_version") == versions
    )  # Item withdrawal does not permanently hold the valid owner.
    with g.worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(g.artifact)
    for schema, table in [
        ("staging", "staged_artifact"),
        ("staging", "adapter_candidate"),
        ("staging", "staging_snapshot"),
        ("staging", "canonical_proposal"),
        ("staging", "synthetic_expression_binding"),
        ("staging", "snapshot_review"),
        ("staging", "gazette_item_binding"),
        ("staging", "gazette_item_review"),
        ("corpus", "representation_revision"),
        ("corpus", "gazette_correspondence"),
        ("corpus", "approved_version"),
    ]:
        for row in rows(f, table, schema):
            assert row["state"] == "erased" and row.get("payload") is None
            if table == "staged_artifact":
                assert row["private_bytes"] is None
            if table == "approved_version":
                assert row["canonical_bytes"] is None
    assert rows(f, "version_node") == nodes and rows(f, "approved_anchor") == anchors
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT {f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(:cid)"),
                dict(cid=f.collection),
            )
            == 0
        )
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM identity_observations")) == 0


def test_three_notice_atomic_caller_rollback_and_native_role_denial(three_notice):
    g, items = three_notice
    f = g.f
    compared = dispatch(items[0], g.editor, items[0].review)
    assert compared.status == "applied"
    cmd = item_approval(items[0], items[0].review, compared)
    before = {
        (s, t): rows(f, t, s)
        for s, t in [
            ("corpus", "gazette_correspondence"),
            ("policy", "gazette_receipt"),
            ("policy", "audit_event"),
        ]
    }
    with g.maintainer.engine.connect() as conn:
        tx = conn.begin()
        result = SyntheticGazette(conn, f.ns).apply(cmd)
        assert result.status == "applied"
        tx.rollback()
    for (s, t), data in before.items():
        assert rows(f, t, s) == data
    assert dispatch(g, g.worker, cmd).status == "forbidden"
    with g.worker.engine.begin() as conn:
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text(f"SELECT * FROM {f.ns.qualified(conn, 'corpus', 'approved_version')}"))


@pytest.mark.parametrize("three_notice", [30500], indirect=True)
def test_three_notice_shared_budget_denies_without_receipt_audit_or_head(three_notice):
    g, items = three_notice
    f = g.f
    reviewed = dispatch(items[0], g.editor, items[0].review)
    assert reviewed.status == "applied"
    cmd = item_approval(items[0], items[0].review, reviewed)
    before = {
        (s, t): rows(f, t, s)
        for s, t in [
            ("corpus", "gazette_correspondence"),
            ("corpus", "gazette_correspondence_head"),
            ("policy", "gazette_receipt"),
            ("policy", "audit_event"),
        ]
    }
    assert dispatch(g, g.maintainer, cmd).status == "capacity_exceeded"
    for (s, t), data in before.items():
        assert rows(f, t, s) == data


def changed_candidates(g, **changes):
    original = g.source[0]
    candidates = [c.model_copy(update={"id": uuid4()}) for c in original.candidates]
    if changes:
        index = changes.pop("index")
        candidates[index] = candidates[index].model_copy(update=changes)
    plan = json.dumps(g.source[4], default=str)
    for old, new in zip(original.candidates, candidates, strict=True):
        plan = plan.replace(str(old.id), str(new.id))
    return candidates, json.loads(plan)


def assemble(g, run, plan):
    f = g.f
    assert apply_assembly(f, g.worker, run).status == "applied"
    snapshot = snapshot_command(f, run)
    saved = apply_assembly(f, g.editor, snapshot)
    assert saved.status == "applied"
    canonical = canonical_command(f, snapshot, saved.output_hash, plan)
    output = call(f, g.editor, canonical)
    assert output.status == "applied"
    return canonical, output


def test_three_notice_incomplete_operative_cell_remains_explicit_and_cannot_be_approved(three_notice):
    g, _ = three_notice
    f = g.f
    candidates, plan = changed_candidates(
        g,
        index=2,
        content_state="illegible",
        content_reason="Original synthetic unavailable cell; no replacement words",
    )
    run = g.source[0].model_copy(
        update={
            "command_id": uuid4(),
            "run_id": uuid4(),
            "profile_key": "incomplete_fixture",
            "candidates": tuple(candidates),
        }
    )
    canonical, output = assemble(g, run, plan)
    proposal = next(r for r in rows(f, "canonical_proposal", "staging") if r["id"] == canonical.proposal_id)
    assert proposal["payload"]["projection"]["incomplete_tables"] == ["t"]
    assert proposal["canonical_bytes"] == EXPECTED
    owner_review = g.source[3]
    review = review_command(
        f, owner_review.binding_id, owner_review.binding_hash, canonical, output, run.candidates
    )
    before = {t: rows(f, t) for t in ("approved_version", "representation_revision", "approved_anchor")}
    receipts = rows(f, "approval_receipt", "policy")
    assert approve(f, g.editor, review).status == "validation_failed"
    assert rows(f, "approval_receipt", "policy") == receipts
    for t, data in before.items():
        assert rows(f, t) == data


@pytest.mark.parametrize(
    "field,value",
    [
        ("requires_exception_review", True),
        ("review_scope", "per_version_exception"),
        ("comparison_method", "automated_confidence"),
    ],
)
def test_three_notice_native_source_review_rejects_unsupported_routes(three_notice, field, value):
    g, _ = three_notice
    command = g.source[3].model_dump(mode="json")
    command.update(command_id=str(uuid4()), review_id=str(uuid4()))
    command[field] = value
    before = rows(g.f, "approval_receipt", "policy")
    with g.editor.engine.begin() as conn:
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.execute(
                text(
                    f"SELECT {g.f.ns.qualified(conn, 'policy', 'apply_synthetic_approval')}(CAST(:body AS jsonb))"
                ),
                dict(body=json.dumps(command)),
            )
        assert failure.value.orig.sqlstate == "22023"
    assert rows(g.f, "approval_receipt", "policy") == before


def test_three_notice_byte_correction_gets_new_version_and_reuses_reviewed_item_identities(three_notice):
    from test_postgres_acquisition import assessment, stage

    from rights.gazette import declare_synthetic_item_binding

    g, items = three_notice
    f = g.f
    initial = publish(items)
    original_versions = rows(f, "approved_version")
    original_anchors = rows(f, "approved_anchor")
    candidates, plan = changed_candidates(
        g, index=0, text=g.source[0].candidates[0].text.replace("Cafe", "Kafe")
    )
    raw = "\n".join(c.text for c in candidates).encode()
    acq = assessment(f, derive="allow", artifact_hash=sha256(raw).hexdigest())
    assert apply(f, g.rights, acq).status == "applied"
    artifact, _ = stage(f, g.worker, acq, private_bytes=raw)
    run = run_command(f, artifact, acq, candidates=candidates).model_copy(
        update={"artifact_hash": sha256(raw).hexdigest()}
    )
    canonical, output = assemble(g, run, plan)
    expression = original_versions[0]["expression_id"]
    binding, binding_hash, _, manifestation = bind(f, artifact, expression=expression)
    review = review_command(f, binding, binding_hash, canonical, output, run.candidates)
    compared = approve(f, g.editor, review)
    assert compared.status == "applied"
    corrected = approve(
        f,
        g.maintainer,
        approval_command(f, review, compared, prior_version_id=g.version.version_id, expected_rep_revision=1),
    )
    assert corrected.status == "applied" and corrected.version_id != g.version.version_id
    assert original_versions[0] in rows(f, "approved_version")
    assert all(a in rows(f, "approved_anchor") for a in original_anchors)
    new_anchors = {a["id"] for a in rows(f, "approved_anchor") if a["version_id"] == corrected.version_id}
    assert new_anchors and not new_anchors.intersection(a["id"] for a in original_anchors)
    new_items = []
    for item, key in zip(items, ("one", "two", "three"), strict=True):
        old_binding = next(b for b in rows(f, "gazette_item_binding", "staging") if b["id"] == item.binding)
        bid = uuid4()
        with f.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO identity_work_manifestations VALUES(:work,:man)"),
                dict(work=old_binding["item_work_id"], man=manifestation),
            )
            node = next(
                n
                for n in rows(f, "version_node")
                if n["version_id"] == corrected.version_id and n["local_key"] == "notice:" + key
            )
            digest = declare_synthetic_item_binding(
                conn,
                f.ns,
                binding_id=bid,
                item_expression_id=old_binding["item_expression_id"],
                issue_version_id=corrected.version_id,
                notice_node_id=node["id"],
                evidence_id=item.item_evidence,
                reason="Explicit synthetic correspondence after byte correction",
            )
        new_items.append(
            replace(item, version=corrected, artifact=artifact, binding=bid, binding_hash=digest, review=None)
        )
    new_results = publish(new_items)
    new_g = new_items[0]
    issue, projections = read_all(new_g, new_results)
    assert issue and issue.canonical_text.encode() == EXPECTED.replace("Café".encode(), "Kafé".encode())
    _, old_projections = read_all(g, initial)
    assert {p.item_work_id for p in projections} == {p.item_work_id for p in old_projections}
    assert {p.item_expression_id for p in projections} == {p.item_expression_id for p in old_projections}
    assert all(p.issue_version_id == corrected.version_id for p in projections)
    assert all(p.issue_version_id == g.version.version_id for p in old_projections)
