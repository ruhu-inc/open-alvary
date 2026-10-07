"""Private proposal DTO and CLI input boundaries; no database claims."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rights.assembly import parse_assembly_command
from rights.commands import MAX_COMMAND_BYTES
from schema.assembly import CandidateInput, SnapshotBody
from schema.identity import mint


def run_data():
    return dict(
        command_id=str(uuid4()),
        collection_id=mint("scp"),
        reason="Original synthetic fixture",
        action="record_synthetic_run",
        run_id=str(uuid4()),
        artifact_id=str(uuid4()),
        assessment_id=str(uuid4()),
        assessment_revision=1,
        artifact_hash="a" * 64,
        purpose="synthetic",
        profile_key="synthetic_profile",
        candidates=[
            dict(
                id=str(uuid4()),
                adapter_local_id="local1",
                region_key="r1",
                artifact_class="native_text",
                block_type="paragraph",
                text="  Original synthetic text.\n",
                unavailable_page_reason="No physical page",
                unavailable_geometry_reason="No geometry",
            )
        ],
    )


def test_private_candidate_literal_unicode_and_whitespace_are_preserved():
    raw = run_data()
    raw["candidates"][0]["text"] = "  Original synthetic العربية \u2067RTL\u2069\n"
    parsed = parse_assembly_command(json.dumps(raw).encode())
    assert parsed.candidates[0].text == raw["candidates"][0]["text"]
    with pytest.raises(ValidationError):
        parsed.candidates[0].text = "changed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_id", str(uuid4())),
        ("role", "review"),
        ("approved", True),
        ("assessment_revision", True),
        ("profile_hash", "a" * 64),
    ],
)
def test_assembly_rejects_caller_authority_or_hashes(field, value):
    raw = run_data()
    raw[field] = value
    with pytest.raises(ValidationError):
        parse_assembly_command(json.dumps(raw).encode())


@pytest.mark.parametrize(
    "change",
    ["present_empty", "illegible_text", "missing_reason", "fake_geometry", "invalid_grid", "no_cell"],
)
def test_candidate_rejects_unknown_or_inconsistent_content_and_geometry(change):
    raw = run_data()["candidates"][0]
    if change == "present_empty":
        raw["text"] = ""
    elif change == "illegible_text":
        raw.update(content_state="illegible", content_reason="Unreadable")
    elif change == "missing_reason":
        raw.update(content_state="unsupported", text="")
    elif change == "fake_geometry":
        raw["polygon"] = [[0, 0], [1, 1]]
    elif change == "invalid_grid":
        raw.update(
            block_type="table_cell",
            cell=dict(table_local_id="table1", row=99, column=0, row_span=2, column_span=1),
        )
    else:
        raw["block_type"] = "table_cell"
    with pytest.raises(ValidationError):
        CandidateInput.model_validate(raw)


def test_snapshot_order_is_unique_complete_and_has_one_selection_per_region():
    first, second = uuid4(), uuid4()
    base = dict(
        selections=[dict(region_key="r1", candidate_id=first), dict(region_key="r2", candidate_id=second)],
        order=[first, second],
        reason="Synthetic order",
    )
    for bad in ([first, first], [first], [first, second, uuid4()]):
        with pytest.raises(ValidationError):
            SnapshotBody.model_validate(base | {"order": bad})
    base["selections"][1]["region_key"] = "r1"
    with pytest.raises(ValidationError):
        SnapshotBody.model_validate(base)


def test_assembly_bounds_command_before_parsing():
    with pytest.raises(ValueError, match="too large"):
        parse_assembly_command(b"x" * (MAX_COMMAND_BYTES + 1))


@pytest.mark.parametrize("mode", ["invalid_json", "missing_url", "sqlite_url", "oversize"])
def test_assembly_cli_fails_safely_without_echo(mode):
    env = dict(os.environ)
    env.pop("ALVARY_OPERATOR_DATABASE_URL", None)
    raw = json.dumps(run_data())
    if mode == "invalid_json":
        raw = "private-do-not-echo"
    elif mode == "oversize":
        raw = "private-do-not-echo" * MAX_COMMAND_BYTES
    elif mode == "sqlite_url":
        env["ALVARY_OPERATOR_DATABASE_URL"] = "sqlite:///do-not-create.db"
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--assembly"],
        input=raw,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 2 and not result.stderr
    assert json.loads(result.stdout)["status"] == (
        "validation_failed" if mode in ("invalid_json", "oversize") else "unavailable"
    )
    assert "private-do-not-echo" not in result.stdout
