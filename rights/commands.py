"""Bounded, caller-owned operator transactions and safe command outcomes."""

import json

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from rights.database import PolicyNamespace
from schema.operator import CommandResult, OperatorCommand

MAX_COMMAND_BYTES = 64 * 1024
COMMAND_ADAPTER = TypeAdapter(OperatorCommand)


def parse_command(raw: bytes) -> OperatorCommand:
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("Command too large")
    return COMMAND_ADAPTER.validate_json(raw)


class OperatorCommands:
    def __init__(self, connection: Connection, namespace: PolicyNamespace):
        self.connection = connection
        self.namespace = namespace

    def apply(self, command: OperatorCommand) -> CommandResult:
        try:
            command = COMMAND_ADAPTER.validate_python(command)
        except ValidationError:
            return CommandResult(command_id=None, status="validation_failed")
        payload = json.dumps(command.model_dump(mode="json"), ensure_ascii=False)
        if len(payload.encode()) > MAX_COMMAND_BYTES:
            return CommandResult(command_id=command.command_id, status="validation_failed")
        function = self.namespace.qualified(self.connection, "policy", "apply_operator_command")
        try:
            with self.connection.begin_nested():
                row = (
                    self.connection.execute(
                        text(f"SELECT * FROM {function}(CAST(:payload AS jsonb))"), {"payload": payload}
                    )
                    .mappings()
                    .one()
                )
                return CommandResult(command_id=command.command_id, status="applied", **dict(row))
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None)
            status = (
                "forbidden"
                if code == "42501"
                else "conflict"
                if code in {"40001", "40P01", "23505"}
                else "validation_failed"
                if code and code[:2] in {"22", "23"}
                else "unavailable"
            )
            return CommandResult(command_id=command.command_id, status=status)
