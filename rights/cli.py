"""Private single-command CLI; native DB credentials come from environment, not argv."""

import argparse
import os
import sys

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from rights.approval import SyntheticApproval, parse_approval_command
from rights.assembly import SyntheticAssembly, parse_assembly_command
from rights.commands import MAX_COMMAND_BYTES, OperatorCommands, parse_command
from rights.database import PolicyNamespace
from rights.gazette import SyntheticGazette, parse_gazette_command
from rights.lifecycle import AcquisitionLifecycle, parse_lifecycle_request
from schema.approval import ApprovalResult
from schema.assembly import AssemblyResult
from schema.gazette import GazetteResult
from schema.operator import CommandResult, LifecycleResult


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply one private JSON operator command from stdin")
    parser.add_argument("--schema", default="public", help="Maintenance-selected foundation base schema")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--reconcile-staging", action="store_true", help="Apply one bounded private lifecycle request"
    )
    modes.add_argument("--assembly", action="store_true", help="Apply one private synthetic assembly command")
    modes.add_argument(
        "--approval", action="store_true", help="Apply one private original-synthetic approval command"
    )
    modes.add_argument(
        "--gazette", action="store_true", help="Apply one private synthetic gazette correspondence command"
    )
    args = parser.parse_args(argv)
    result_type = (
        GazetteResult
        if args.gazette
        else ApprovalResult
        if args.approval
        else AssemblyResult
        if args.assembly
        else LifecycleResult
        if args.reconcile_staging
        else CommandResult
    )
    result = result_type(command_id=None, status="validation_failed")
    try:
        parse = (
            parse_gazette_command
            if args.gazette
            else parse_approval_command
            if args.approval
            else parse_assembly_command
            if args.assembly
            else parse_lifecycle_request
            if args.reconcile_staging
            else parse_command
        )
        command = parse(sys.stdin.buffer.read(MAX_COMMAND_BYTES + 1))
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
            namespace = PolicyNamespace(args.schema)
            result = (
                SyntheticGazette(connection, namespace).apply(command)
                if args.gazette
                else SyntheticApproval(connection, namespace).apply(command)
                if args.approval
                else SyntheticAssembly(connection, namespace).apply(command)
                if args.assembly
                else AcquisitionLifecycle(connection, namespace).reconcile(command)
                if args.reconcile_staging
                else OperatorCommands(connection, namespace).apply(command)
            )
    except (SQLAlchemyError, ValueError):
        result = result_type(command_id=command.command_id, status="unavailable")
    finally:
        if engine is not None:
            engine.dispose()
    print(result.model_dump_json())
    return 0 if result.status == "applied" else 2


if __name__ == "__main__":
    raise SystemExit(main())
