"""The migration's restore (deployment/azure/restore.py, for both clouds): a dump restored into a new,
empty database gets back its roles (without login, but the access role, with its password), their
memberships and its grants, and every table's rows; a database that already has tables is refused."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from postgres_support import validated_test_url
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not shutil.which("pg_dump") or not shutil.which("pg_restore"), reason="no PostgreSQL tools"
    ),
]
SCRIPT = Path(__file__).resolve().parent.parent / "deployment" / "azure" / "restore.py"


def load():
    spec = importlib.util.spec_from_file_location("azure_restore", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_dump_comes_back_with_its_roles_grants_and_rows(tmp_path, monkeypatch):
    restore = load()
    url = validated_test_url()
    tag = uuid4().hex[:10]
    source, target, capability, access = f"src_{tag}_test", f"dst_{tag}_test", f"cap_{tag}", f"acc_{tag}"
    admin = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    libpq = lambda name: restore.libpq(url.set(database=name).render_as_string(hide_password=False))  # noqa: E731
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{source}"'))
            conn.execute(text(f'CREATE DATABASE "{target}"'))
            conn.execute(text(f'CREATE ROLE "{capability}" NOLOGIN'))
            conn.execute(text(f"CREATE ROLE \"{access}\" LOGIN PASSWORD 'old'"))
            conn.execute(text(f'GRANT "{capability}" TO "{access}"'))
        with create_engine(url.set(database=source), poolclass=NullPool).begin() as conn:
            conn.execute(text("CREATE TABLE ledger (id int PRIMARY KEY, note text)"))
            conn.execute(text("INSERT INTO ledger SELECT g, 'row ' || g FROM generate_series(1, 3) g"))
            conn.execute(text(f'GRANT SELECT ON ledger TO "{capability}"'))

        monkeypatch.setenv("SOURCE_URL", libpq(source))
        assert restore.main(["describe", str(tmp_path)]) == 0
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(tmp_path / "database.dump"), libpq(source)],
            check=True,
        )
        # A new server: the roles are not there.
        with create_engine(url.set(database=source), poolclass=NullPool).begin() as conn:
            conn.execute(text(f'REVOKE ALL ON ledger FROM "{capability}"'))
        with admin.connect() as conn:
            conn.execute(text(f'DROP ROLE "{access}"'))
            conn.execute(text(f'DROP ROLE "{capability}"'))

        monkeypatch.setenv("DATABASE_URL", libpq(target))
        monkeypatch.setenv("ACCESS_ROLE_NAME", access)
        monkeypatch.setenv("ACCESS_ROLE_PASSWORD", "new")
        assert restore.main(["local", str(tmp_path)]) == 0
        with create_engine(url.set(database=target), poolclass=NullPool).connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM ledger")) == 3
            assert conn.scalar(text(f"SELECT has_table_privilege('{capability}', 'ledger', 'SELECT')"))
            login, member = conn.execute(
                text(
                    f"SELECT r.rolcanlogin, pg_has_role('{access}', '{capability}', 'member')"
                    f" FROM pg_roles r WHERE r.rolname = '{access}'"
                )
            ).one()
            assert login and member
            assert not conn.scalar(text(f"SELECT rolcanlogin FROM pg_roles WHERE rolname = '{capability}'"))
        assert restore.main(["local", str(tmp_path)]) == 1  # the target has tables now: refused
    finally:
        with admin.connect() as conn:
            for name in (source, target):
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            for role in (access, capability):
                conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        os.environ.pop("SOURCE_URL", None)


def test_dump_writes_what_a_restore_reads(tmp_path, monkeypatch):
    """The job's dump mode (a move away from a cloud whose servers no one outside can reach) writes the
    three files the restore reads, and they restore with every row."""
    restore = load()
    url = validated_test_url()
    tag = uuid4().hex[:10]
    source, target = f"src_{tag}_test", f"dst_{tag}_test"
    admin = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    libpq = lambda name: restore.libpq(url.set(database=name).render_as_string(hide_password=False))  # noqa: E731
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{source}"'))
            conn.execute(text(f'CREATE DATABASE "{target}"'))
        with create_engine(url.set(database=source), poolclass=NullPool).begin() as conn:
            conn.execute(text("CREATE TABLE ledger (id int PRIMARY KEY, note text)"))
            conn.execute(text("INSERT INTO ledger SELECT g, 'row ' || g FROM generate_series(1, 5) g"))
        assert restore.dump(libpq(source), tmp_path) == 0
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(restore.FILES)
        monkeypatch.setenv("DATABASE_URL", libpq(target))
        assert restore.main(["local", str(tmp_path)]) == 0
        with create_engine(url.set(database=target), poolclass=NullPool).connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM ledger")) == 5
    finally:
        with admin.connect() as conn:
            for name in (source, target):
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
