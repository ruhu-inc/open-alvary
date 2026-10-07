"""Private gazette command schema boundaries; no authorization claims without PostgreSQL."""

import json
from uuid import uuid4

import pytest

from rights.gazette import parse_gazette_command
from schema.gazette import GazetteContracts
from schema.identity import mint


def review():
    return dict(
        command_id=str(uuid4()),
        issue_collection_id=mint("scp"),
        item_collection_id=mint("scp"),
        binding_id=str(uuid4()),
        purpose="original-synthetic",
        reason="Original synthetic identification",
        action="record_gazette_item_review",
        review_id=str(uuid4()),
        binding_hash="a" * 64,
        issue_version_id="ver_" + str(uuid4()),
        representation_id="prp_" + str(uuid4()),
        record_set_hash="b" * 64,
        content_hash="c" * 64,
        profile_hash="d" * 64,
        ranges=[dict(start_byte=0, end_byte=5, span_hash="e" * 64)],
        item_rights_revision=1,
        item_verification_revision=1,
        expected_revision=0,
        evidence_id=str(uuid4()),
        identification_method="human_source_identification",
        review_scope="collection_policy",
        requires_exception_review=False,
    )


@pytest.mark.parametrize("field", ["actor_id", "projected_text", "item_version_id", "anchor_id", "geometry"])
def test_client_cannot_supply_authority_copied_text_or_new_byte_owner(field):
    body = review()
    body[field] = "injected"
    with pytest.raises(ValueError):
        parse_gazette_command(json.dumps(body).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", True),
        ("item_rights_revision", 0),
        ("requires_exception_review", True),
        ("identification_method", "regex"),
        ("review_scope", "exception"),
        ("ranges", []),
    ],
)
def test_unreviewed_or_unbounded_projection_paths_rejected(field, value):
    body = review()
    body[field] = value
    with pytest.raises(ValueError):
        parse_gazette_command(json.dumps(body).encode())


def test_one_explicit_full_notice_range_contract_roundtrips():
    body = review()
    command = parse_gazette_command(json.dumps(body).encode())
    assert GazetteContracts(commands=(command,)).schema_version == "synthetic-gazette-1"
    body["ranges"] *= 2
    with pytest.raises(ValueError):
        parse_gazette_command(json.dumps(body).encode())


def test_cli_does_not_echo_invalid_projection_payload(capsys, monkeypatch):
    import io
    import sys

    from rights.cli import main

    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b'{"action":"publish","projected_text":"private-do-not-echo"}')),
    )
    assert main(["--gazette"]) == 2
    output = capsys.readouterr()
    assert "private-do-not-echo" not in output.out + output.err
    assert json.loads(output.out)["status"] == "validation_failed"
