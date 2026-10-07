"""Private single-command CLI; native DB credentials come from environment, not argv."""

import argparse
import os
import sys

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from rights.commands import MAX_COMMAND_BYTES, OperatorCommands, parse_command
from rights.database import PolicyNamespace
from schema.operator import CommandResult


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply one private JSON operator command from stdin")
    parser.add_argument("--schema", default="public", help="Maintenance-selected foundation base schema")
    args = parser.parse_args(argv)
    result = CommandResult(command_id=None, status="validation_failed")
    try:
        command = parse_command(sys.stdin.buffer.read(MAX_COMMAND_BYTES + 1))
    except (ValidationError, ValueError):
        print(result.model_dump_json())
        return 2
    engine = None
    try:
        raw = os.environ.get("ALVARY_OPERATOR_DATABASE_URL")
        url = make_url(raw) if raw else None
        if url is None or url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("Explicit PostgreSQL operator URL required")
        engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='30s'"))
            connection.execute(text("SET LOCAL lock_timeout='5s'"))
            connection.execute(text("SET LOCAL idle_in_transaction_session_timeout='30s'"))
            result = OperatorCommands(connection, PolicyNamespace(args.schema)).apply(command)
    except (SQLAlchemyError, ValueError):
        result = CommandResult(command_id=command.command_id, status="unavailable")
    finally:
        if engine is not None:
            engine.dispose()
    print(result.model_dump_json())
    return 0 if result.status == "applied" else 2


if __name__ == "__main__":
    raise SystemExit(main())
