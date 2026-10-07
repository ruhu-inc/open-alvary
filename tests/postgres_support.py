"""Owned disposable databases; no SQL/schema rewriting or shared test transactions."""

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def validated_test_url():
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.fail("PostgreSQL acceptance requires TEST_DATABASE_URL; use --fast only for fast checks")
    try:
        url = make_url(raw)
        valid = url.drivername == "postgresql+psycopg" and url.database and url.database.endswith("_test")
    except Exception:
        valid = False
    if not valid:
        pytest.fail("TEST_DATABASE_URL must use postgresql+psycopg and a database ending in _test")
    return url


def migrate(engine, revision="head"):
    config = Config("alembic.ini")
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


@contextmanager
def database(url, namespace, template=None):
    name = "oa_" + uuid4().hex + "_test"
    admin = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            source = " TEMPLATE " + quote(template) if template else ""
            conn.execute(text("CREATE DATABASE " + quote(name) + source))
        created = True
        engine = create_engine(
            url.set(database=name),
            poolclass=NullPool,
            connect_args={"options": f"-csearch_path={namespace}"},
        )
        if not template:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA "{namespace}"'))
        yield engine, name
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as conn:
                # Only the random database created by this context is eligible for cleanup.
                conn.execute(text('DROP DATABASE "' + name + '" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def migrated_template():
    url = validated_test_url()
    namespace = "test_" + uuid4().hex
    with database(url, namespace) as (engine, name):
        migrate(engine)
        # No seed, roles, appointments or payloads are shared. Database cloning preserves
        # trigger/function OIDs and qualified names without rewriting security-definer SQL.
        yield url, namespace, name
