"""Private command parser and CLI failures must not disclose submitted data."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rights.commands import MAX_COMMAND_BYTES, parse_command
from rights.lifecycle import parse_lifecycle_request
from schema.identity import mint


def original_command():
    return {
        "command_id": str(uuid4()),
        "action": "record_evidence",
        "collection_id": mint("scp"),
        "expected_revision": 0,
        "reason": "Original synthetic private reason",
        "payload": {
            "id": str(uuid4()),
            "private_reference": "private://do-not-echo",
            "evidence_hash": "a" * 64,
            "observed_at": "2026-01-01T00:00:00Z",
        },
    }


@pytest.mark.parametrize("placement", ["actor_id", "roles", "payload_actor"])
def test_command_rejects_submitted_actor_or_privileges(placement):
    command = original_command()
    if placement == "payload_actor":
        command["payload"]["actor_id"] = str(uuid4())
    else:
        command[placement] = str(uuid4())
    with pytest.raises(ValidationError):
        parse_command(json.dumps(command).encode())


def test_command_size_is_checked_before_json_parse():
    with pytest.raises(ValueError, match="too large"):
        parse_command(b"x" * (MAX_COMMAND_BYTES + 1))


@pytest.mark.parametrize("mode", ["invalid_json", "missing_url", "sqlite_url", "oversize"])
def test_cli_rejects_invalid_input_or_configuration_without_echo(mode):
    env = dict(os.environ)
    env.pop("ALVARY_OPERATOR_DATABASE_URL", None)
    command = json.dumps(original_command())
    if mode == "invalid_json":
        command = "private://do-not-echo invalid input"
    if mode == "sqlite_url":
        env["ALVARY_OPERATOR_DATABASE_URL"] = "sqlite:///do-not-create.db"
    if mode == "oversize":
        command = "private://do-not-echo" * MAX_COMMAND_BYTES
    completed = subprocess.run(
        [sys.executable, "-m", "rights.cli"],
        input=command,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert completed.returncode == 2
    response = json.loads(completed.stdout)
    assert response["status"] == (
        "validation_failed" if mode in {"invalid_json", "oversize"} else "unavailable"
    )
    assert "do-not-echo" not in completed.stdout and not completed.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("limit", 0),
        ("limit", 101),
        ("limit", True),
        ("limit", "1"),
        ("erase_due", "true"),
        ("actor_id", str(uuid4())),
    ],
)
def test_lifecycle_parser_enforces_bounds_and_rejects_actor_input(field, value):
    request = dict(command_id=str(uuid4()), collection_id=mint("scp"), reason="Synthetic request")
    request[field] = value
    with pytest.raises(ValidationError):
        parse_lifecycle_request(json.dumps(request).encode())


def test_lifecycle_parser_bounds_bytes_before_parsing():
    with pytest.raises(ValueError, match="too large"):
        parse_lifecycle_request(b"x" * (MAX_COMMAND_BYTES + 1))


@pytest.mark.parametrize("mode", ["invalid_json", "missing_url", "sqlite_url", "oversize"])
def test_lifecycle_cli_safe_failures(mode):
    env = dict(os.environ)
    env.pop("ALVARY_OPERATOR_DATABASE_URL", None)
    raw = json.dumps(dict(command_id=str(uuid4()), collection_id=mint("scp"), reason="private://do-not-echo"))
    if mode == "sqlite_url":
        env["ALVARY_OPERATOR_DATABASE_URL"] = "sqlite:///do-not-create.db"
    elif mode == "invalid_json":
        raw = "private://do-not-echo"
    elif mode == "oversize":
        raw = "private://do-not-echo" * MAX_COMMAND_BYTES
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--reconcile-staging"],
        input=raw,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 2 and not result.stderr
    assert json.loads(result.stdout)["status"] == (
        "validation_failed" if mode in {"invalid_json", "oversize"} else "unavailable"
    )
    assert "do-not-echo" not in result.stdout
