"""Private command boundaries, without claiming database authorization or legal review."""

import json
from uuid import uuid4

import pytest

from rights.approval import parse_approval_command
from schema.approval import ApprovalContracts
from schema.identity import mint


def review():
    return dict(
        command_id=str(uuid4()),
        collection_id=mint("scp"),
        purpose="original-synthetic",
        reason="Original synthetic source comparison",
        action="record_snapshot_review",
        review_id=str(uuid4()),
        binding_id=str(uuid4()),
        binding_hash="a" * 64,
        proposal_id=str(uuid4()),
        snapshot_hash="b" * 64,
        profile_hash="c" * 64,
        content_hash="d" * 64,
        verification_revision=1,
        evidence_id=str(uuid4()),
        reviewed_candidate_ids=[str(uuid4())],
        comparison_method="human_source_comparison",
        review_scope="collection_policy",
        requires_exception_review=False,
    )


def approval():
    return dict(
        command_id=str(uuid4()),
        collection_id=mint("scp"),
        purpose="original-synthetic",
        reason="Original synthetic private approval",
        action="approve_synthetic_version",
        review_id=str(uuid4()),
        review_hash="a" * 64,
        rights_revision=1,
        verification_revision=1,
        version_id="ver_" + str(uuid4()),
        representation_id="prp_" + str(uuid4()),
        mode="new_version",
    )


@pytest.mark.parametrize("make", [review, approval])
def test_private_commands_round_trip_in_separate_versioned_contract(make):
    command = parse_approval_command(json.dumps(make()).encode())
    assert parse_approval_command(command.model_dump_json().encode()) == command
    assert ApprovalContracts(commands=(command,)).schema_version == "synthetic-approval-1"


@pytest.mark.parametrize("field", ["actor_id", "canonical_text", "published", "record_set", "geometry"])
def test_callers_cannot_supply_authority_text_or_minted_records(field):
    for make in (review, approval):
        body = make()
        body[field] = "injected"
        with pytest.raises(ValueError):
            parse_approval_command(json.dumps(body).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("requires_exception_review", True),
        ("comparison_method", "automated_confidence"),
        ("review_scope", "per_version_exception"),
        ("verification_revision", True),
        ("reviewed_candidate_ids", []),
    ],
)
def test_unsupported_review_paths_are_explicitly_rejected(field, value):
    body = review()
    body[field] = value
    with pytest.raises(ValueError):
        parse_approval_command(json.dumps(body).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("version_id", "legacy-version"),
        ("representation_id", "rep-" + str(uuid4())),
        ("expected_rep_revision", True),
        ("rights_revision", 0),
        ("mode", "publish"),
    ],
)
def test_approval_requires_canonical_identity_and_strict_revision_fields(field, value):
    body = approval()
    body[field] = value
    with pytest.raises(ValueError):
        parse_approval_command(json.dumps(body).encode())


def test_cli_rejects_invalid_private_command_without_echoing_it(capsys, monkeypatch):
    import io
    import sys

    from rights.cli import main

    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b'{"action":"publish","canonical_text":"private-do-not-echo"}')),
    )
    assert main(["--approval"]) == 2
    output = capsys.readouterr()
    assert "private-do-not-echo" not in output.out + output.err
    assert json.loads(output.out)["status"] == "validation_failed"
