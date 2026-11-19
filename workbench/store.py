"""Workflow database for the review workbench.

Every review action is an immutable row: a page revision is never updated, only superseded by
the next revision number for that page; approvals and audit events are append-only. PostgreSQL
in production (ALVARY_REVIEW_DATABASE_URL); SQLite is accepted for a local trial and tests.
"""

import hashlib
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    false,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

ROLES = {"reviewer", "approver", "admin", "access_admin"}
SESSION_IDLE = timedelta(hours=1)
SESSION_MAX = timedelta(hours=12)
CHALLENGE_TTL = timedelta(minutes=5)
ENROLMENT_TTL = timedelta(hours=24)

metadata = MetaData()

operator = Table(
    "operator",
    metadata,
    Column("email", String(320), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("roles", String(100), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    # The database's own default too: a copy of the release before, which does not know these columns,
    # can then still add an operator to a database this code made.
    Column("agent", Boolean, nullable=False, default=False, server_default=false()),
    Column("may_change_approved", Boolean, nullable=False, default=False, server_default=false()),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

session = Table(
    "session",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("csrf", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("passkey_at", DateTime(timezone=True)),  # the last passkey check in this session
    Column("challenge", String(64)),  # one WebAuthn challenge at a time, used once
    Column("challenge_purpose", String(20)),
    Column("challenge_at", DateTime(timezone=True)),
)

# Operators' passkeys for the Access section (workbench/passkeys.py); never deleted, only revoked.
passkey = Table(
    "operator_passkey",
    metadata,
    Column("credential_id", String(1400), primary_key=True),
    Column("email", String(320), nullable=False, index=True),
    Column("name", String(100), nullable=False),
    Column("public_key", Text, nullable=False),
    Column("algorithm", Integer, nullable=False),
    Column("sign_count", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

# One-time codes, issued on the server, that let an operator register a first passkey.
enrolment = Table(
    "passkey_enrolment",
    metadata,
    Column("code_hash", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
)

auth_flow = Table(
    "auth_flow",
    metadata,
    Column("state", String(64), primary_key=True),
    Column("nonce", String(64), nullable=False),
    Column("verifier", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

page_revision = Table(
    "page_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("act_id", String(140), nullable=False),
    Column("page", Integer, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("reading_sha256", String(64), nullable=False),  # the machine reading this was based on
    Column("units", JSON, nullable=False),
    Column("status", String(20), nullable=False),  # checked | attention
    Column("resolutions", JSON, nullable=False),  # flag id -> {decision, note}
    Column("comment", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("label", String(40)),
    Column("task", String(20)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("act_id", "page", "revision"),
)

detail_revision = Table(
    "detail_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("act_id", String(140), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("details", JSON, nullable=False),  # field -> value (workbench/details.py, DETAILS-1)
    Column("absent", JSON, nullable=False),  # field -> why it is not given
    Column("status", String(20), nullable=False),  # draft | confirmed
    Column("comment", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("label", String(40)),
    Column("task", String(20)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("act_id", "revision"),
)

approval = Table(
    "approval",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("act_id", String(140), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("decision", String(20), nullable=False),  # approved | revoked
    Column("content_sha256", String(64), nullable=False),
    Column("details_sha256", String(64)),  # the confirmed details approved with the text
    # What publishing will write (TXP-01): the canonical text and structure, and the rules that made them.
    Column("text_sha256", String(64)),
    Column("structure_sha256", String(64)),
    Column("serialization_profile", String(60)),
    Column("structure_rules", String(60)),
    Column("rights_basis", Text, nullable=False),
    Column("materials", JSON, nullable=False),
    Column("note", Text, nullable=False, default=""),
    # Under a verification-routing decision (CPR-01, D28): the routes, audit sample and escalation approved.
    Column("routing", JSON),
    Column("author", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("act_id", "revision"),
)

# Legal events read from a work's text (CPR-02, workbench.editorial): a proposal, each edit and the
# review are revisions; an event is never updated in place.
legal_event_revision = Table(
    "legal_event_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(60), nullable=False),  # evt_<uuid4>: its id when published
    Column("work", String(160), nullable=False),  # the work whose text it was read from
    Column("act_id", String(140), nullable=False),  # the document (language version) it was saved on
    Column("revision", Integer, nullable=False),
    Column("event", JSON, nullable=False),  # operations.events.EventIn fields, less its id and review
    Column("status", String(20), nullable=False),  # proposed | reviewed | rejected
    Column("proposed_from", String(400)),  # what the text proposed (type, provision, title), if it did
    Column("comment", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", "revision"),
)

# Consolidation instructions read from an amending work (CPR-03), reviewed by two editors (EDT-04).
instruction_revision = Table(
    "instruction_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("instruction_id", String(80), nullable=False),
    Column("work", String(160), nullable=False),  # the amending work
    Column("act_id", String(140), nullable=False),  # the document (language version) it was saved on
    Column("target_work", String(160), nullable=False),  # the work it amends
    Column("revision", Integer, nullable=False),
    Column("instruction", JSON, nullable=False),  # workbench.consolidation.Instruction, less its id
    Column("status", String(20), nullable=False),  # proposed | reviewed | second_reviewed | rejected
    Column("comment", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("instruction_id", "revision"),
)

# A judgment's citations (EDT-01, workbench.citations): a proposal, its confirmation or rejection, its
# withdrawal and each treatment recorded or withdrawn are revisions; a citation is never updated in place.
citation_revision = Table(
    "citation_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("citation_id", String(60), nullable=False),  # cit_<uuid4>: its id when published
    Column("work", String(160), nullable=False),  # the citing judgment
    Column("act_id", String(140), nullable=False),  # the document (language version) it was saved on
    Column("revision", Integer, nullable=False),
    Column("citation", JSON, nullable=False),  # what it cites, its words, its review and its treatments
    Column("status", String(20), nullable=False),  # proposed | confirmed | rejected | withdrawn
    Column("proposed_from", String(400)),  # the proposal's paragraph, work and part
    Column("comment", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("citation_id", "revision"),
)

audit = Table(
    "audit_event",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(320), nullable=False),
    Column("action", String(60), nullable=False),
    Column("act_id", String(140)),
    Column("page", Integer),
    Column("detail", JSON, nullable=False),
)

# Multi-agent review claims, releases, blocked labels and page audits (docs/architecture/multi-agent-review-design.md).
work_claim = Table(
    "work_claim",
    metadata,
    Column("work_id", String(140), primary_key=True),
    Column("account", String(320), nullable=False),
    Column("label", String(40), nullable=False),
    Column("token_hash", String(64), nullable=False),
    Column("task", String(20), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("renewed_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

work_release = Table(
    "work_release",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", String(140), nullable=False),
    Column("account", String(320), nullable=False),
    Column("label", String(40), nullable=False),
    Column("task", String(20), nullable=False),
    Column("outcome", String(20), nullable=False),  # done | parked | abandoned
    Column("reason", Text, nullable=False, default=""),
    Column("released_at", DateTime(timezone=True), nullable=False),
)

blocked_label = Table(
    "blocked_label",
    metadata,
    Column("account", String(320), primary_key=True),
    Column("label", String(40), primary_key=True),
    Column("blocked_at", DateTime(timezone=True), nullable=False),
    Column("blocked_by", String(320), nullable=False),
    Column("reason", Text, nullable=False, default=""),
)

review_audit = Table(
    "review_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", String(140), nullable=False),
    Column("act_id", String(140), nullable=False),
    Column("page", Integer, nullable=False),
    Column("release_id", Integer, nullable=False),
    Column("reviewer", String(320), nullable=False),
    Column("reviewer_label", String(40), nullable=False),
    Column("reviewer_revision", Integer, nullable=False),
    Column("drawn_at", DateTime(timezone=True), nullable=False),
    Column("auditor", String(320)),
    Column("auditor_label", String(40)),
    Column("auditor_units", JSON),
    Column("auditor_resolutions", JSON),
    Column("auditor_status", String(20)),
    Column("audited_at", DateTime(timezone=True)),
    Column("paragraphs_changed", Integer),
    Column("words_changed", Integer),
    Column("other_changes", Integer),
    Column("verdict", String(20)),  # clean | minor | material
    Column("owner_decision", String(20)),  # accepted_auditor | kept_reviewer
    Column("owner_decided_by", String(320)),
    Column("owner_decided_at", DateTime(timezone=True)),
)


# Publishing (operations.publish run): every run, asked for from the workbench or started by the nightly
# schedule, and where each document considered stands in public view after the latest run. The
# publisher's own login (PUBLISHER) writes these two tables and only reads the review tables it needs
# (PUBLISHER_READS); the workbench asks for a run and reads them.
publication_run = Table(
    "publication_run",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("trigger", String(20), nullable=False),  # manual | schedule
    Column("requested_by", String(320)),  # who asked, for a manual run
    Column("status", String(20), nullable=False),  # requested | running | succeeded | failed
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("summary", JSON, nullable=False),  # per jurisdiction: decisions and what was done
    Column("error", Text),
)
publication_status = Table(
    "publication_status",
    metadata,
    Column("document_id", String(140), primary_key=True),
    Column("work", String(140), nullable=False),
    Column("jurisdiction", String(8), nullable=False),
    Column("details", String(20), nullable=False),  # public | waiting | blocked | taken_down
    # public | held | not_covered | no_decision | blocked | withdrawn | waiting
    Column("text", String(20), nullable=False),
    Column("reason", Text, nullable=False),
    Column("details_since", DateTime(timezone=True)),
    Column("text_since", DateTime(timezone=True)),
    Column("run_id", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
PUBLISHER = "review_publisher"
PUBLISHER_READS = ("operator", "page_revision", "detail_revision", "approval")
PUBLISHER_WRITES = ("publication_run", "publication_status")


class Conflict(Exception):
    """Someone saved first: the client must reload and reapply."""


def now() -> datetime:
    return datetime.now(UTC)


def db_now(db):
    """Database clock on PostgreSQL (avoids cross-instance skew); UTC Python clock on SQLite."""
    return func.now() if db.dialect.name == "postgresql" else now()


def session_limits(person: dict | None) -> tuple[timedelta, timedelta]:
    """Idle and maximum session lifetimes for an operator (section 3.3)."""
    if (
        person
        and person.get("active", True)
        and bool(person.get("agent"))
        and set(person.get("roles") or ()) == {"reviewer"}
    ):
        idle_hours = float(os.environ.get("ALVARY_REVIEW_AGENT_IDLE_HOURS", "24"))
        max_hours = float(os.environ.get("ALVARY_REVIEW_AGENT_MAX_HOURS", "168"))
        return timedelta(hours=idle_hours), timedelta(hours=max_hours)
    return SESSION_IDLE, SESSION_MAX


def session_max_age(person: dict | None) -> int:
    """Cookie max_age in seconds matching the account's maximum session lifetime."""
    return int(session_limits(person)[1].total_seconds())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def engine_from_environment() -> Engine:
    url = os.environ.get("ALVARY_REVIEW_DATABASE_URL")
    if not url:
        raise SystemExit("Set ALVARY_REVIEW_DATABASE_URL (PostgreSQL in production)")
    return connect(url)


def connect(url: str) -> Engine:
    pool_kwargs = (
        {}
        if make_url(url).drivername.startswith("sqlite")
        else {"pool_size": 4, "max_overflow": 4, "pool_recycle": 1800}
    )
    engine = create_engine(url, future=True, pool_pre_ping=True, **pool_kwargs)
    _make_schema(engine)
    publisher = os.environ.get("ALVARY_REVIEW_PUBLISHER_DATABASE_URL")
    if publisher and engine.dialect.name == "postgresql":
        try:
            provision_publisher(engine, make_url(publisher).password or "")
        except (SystemExit, Exception) as error:  # noqa: BLE001 - reviewing goes on; publishing waits
            print(
                f"workbench: the publisher's login was not made: {said(error)}", file=sys.stderr, flush=True
            )
    return engine


def said(error: BaseException) -> str:
    """What went wrong, without the statement: a database error carries the SQL it refused, and the
    publisher's password is written into one."""
    diag = getattr(getattr(error, "orig", None), "diag", None)
    primary = getattr(diag, "message_primary", None)
    if primary:
        return f"{type(error.orig).__name__}: {primary}"
    return type(error).__name__ if hasattr(error, "statement") else str(error)


def provision_publisher(engine: Engine, password: str) -> None:
    """The publisher's login (operations.publish run): it reads approvals, details, page revisions and
    operators' names, and writes only the publishing tables; never sessions, passkeys or enrolment
    codes, and never a review. Made or brought up to date each time the workbench starts, so a table
    added later is granted with it."""
    # Generated letters and digits only (Terraform's random_password): a role's password cannot be a bound
    # parameter, so it is written into the statement, and nothing else can be written there with it.
    if not re.fullmatch(r"[A-Za-z0-9]{20,}", password):
        raise SystemExit("The publisher's password must be generated: 20 or more letters and digits")
    with engine.begin() as db:
        exists = db.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": PUBLISHER}).first()
        # Made with no attribute beyond signing in (a role's defaults: no superuser, databases or roles);
        # afterwards only its password is set again: naming the other attributes needs a superuser, which
        # the workbench's own login on Cloud SQL is not.
        if exists:
            db.execute(text(f"ALTER ROLE {PUBLISHER} LOGIN PASSWORD '{password}'"))
        else:
            db.execute(text(f"CREATE ROLE {PUBLISHER} LOGIN NOINHERIT PASSWORD '{password}'"))
        database = db.execute(text("SELECT current_database()")).scalar()
        db.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {PUBLISHER}'))
        db.execute(text(f"GRANT USAGE ON SCHEMA public TO {PUBLISHER}"))
        db.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {PUBLISHER}"))
        db.execute(text(f"GRANT SELECT ON {', '.join(PUBLISHER_READS)} TO {PUBLISHER}"))
        db.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(PUBLISHER_WRITES)} TO {PUBLISHER}"))
        db.execute(text(f"GRANT USAGE, SELECT ON SEQUENCE publication_run_id_seq TO {PUBLISHER}"))


# Copies of the workbench that start together (a release with several instances) must not each try to
# create the same new table: PostgreSQL then refuses all but one with a duplicate in its own catalogue,
# and that copy fails to start. One session-level advisory lock makes them take turns; the later ones
# find everything made. SQLite has one writer and needs no lock.
SCHEMA_LOCK = 0x6F61726576696577  # "oareview"


def _make_schema(engine: Engine) -> None:
    """The tables, then the columns and indexes added since a database was first made, one copy at a time."""
    if engine.dialect.name != "postgresql":
        metadata.create_all(engine)
        _upgrade(engine)
        return
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_LOCK})
        try:
            metadata.create_all(engine)
            _upgrade(engine)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_LOCK})
            holder.commit()


def _upgrade(engine: Engine) -> None:
    """Columns and indexes added after a database was first created (create_all adds only missing tables).

    The catalogue is read first and a statement is issued only for what is missing. On PostgreSQL even
    ``ADD COLUMN IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` take their lock on the table before
    they look, so issued at every start they queue every reader and writer of the table behind whatever
    transaction is in flight, and the start with them. What does have to be added goes one statement to
    a transaction and waits five seconds for its lock at most: a start that cannot get it fails, and is
    tried again, rather than hold the others up."""
    added = {
        "approval": {
            "details_sha256": "VARCHAR(64)",
            "text_sha256": "VARCHAR(64)",
            "structure_sha256": "VARCHAR(64)",
            "serialization_profile": "VARCHAR(60)",
            "structure_rules": "VARCHAR(60)",
            "routing": "JSON",
        },
        "session": {
            "passkey_at": "TIMESTAMP WITH TIME ZONE",
            "challenge": "VARCHAR(64)",
            "challenge_purpose": "VARCHAR(20)",
            "challenge_at": "TIMESTAMP WITH TIME ZONE",
        },
        "operator": {
            "agent": "BOOLEAN NOT NULL DEFAULT FALSE",
            "may_change_approved": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "page_revision": {
            "label": "VARCHAR(40)",
            "task": "VARCHAR(20)",
        },
        "detail_revision": {
            "label": "VARCHAR(40)",
            "task": "VARCHAR(20)",
        },
    }
    indexes = {
        "work_claim": {
            "work_claim_account_label_uq": "CREATE UNIQUE INDEX IF NOT EXISTS work_claim_account_label_uq "
            "ON work_claim (account, label) WHERE label <> 'unlabelled'",
            "work_claim_expires_at_idx": "CREATE INDEX IF NOT EXISTS work_claim_expires_at_idx "
            "ON work_claim (expires_at)",
        },
        "work_release": {
            "work_release_work_id_id_idx": "CREATE INDEX IF NOT EXISTS work_release_work_id_id_idx "
            "ON work_release (work_id, id)",
        },
        "page_revision": {
            "page_revision_author_label_idx": "CREATE INDEX IF NOT EXISTS page_revision_author_label_idx "
            "ON page_revision (author, label)",
        },
        "detail_revision": {
            "detail_revision_author_label_idx": "CREATE INDEX IF NOT EXISTS detail_revision_author_label_idx "
            "ON detail_revision (author, label)",
        },
        "audit_event": {
            "audit_event_actor_at_idx": "CREATE INDEX IF NOT EXISTS audit_event_actor_at_idx "
            "ON audit_event (actor, at)",
            "audit_event_act_id_id_idx": "CREATE INDEX IF NOT EXISTS audit_event_act_id_id_idx "
            "ON audit_event (act_id, id)",
        },
        "review_audit": {
            "review_audit_work_id_audited_at_idx": "CREATE INDEX IF NOT EXISTS "
            "review_audit_work_id_audited_at_idx ON review_audit (work_id, audited_at)",
            "review_audit_reviewer_label_audited_at_idx": "CREATE INDEX IF NOT EXISTS "
            "review_audit_reviewer_label_audited_at_idx ON review_audit (reviewer, reviewer_label, audited_at)",
            "review_audit_reviewer_revision_idx": "CREATE INDEX IF NOT EXISTS "
            "review_audit_reviewer_revision_idx ON review_audit (reviewer_revision)",
        },
    }
    found = inspect(engine)
    wanted: list[str] = []
    for table, columns in added.items():
        present = {column["name"] for column in found.get_columns(table)}
        wanted += [
            f"ALTER TABLE {table} ADD COLUMN {name} {kind}"
            for name, kind in columns.items()
            if name not in present
        ]
    for table, named in indexes.items():
        present = {index["name"] for index in found.get_indexes(table)}
        wanted += [statement for name, statement in named.items() if name not in present]
    for statement in wanted:
        with engine.begin() as db:
            if db.dialect.name == "postgresql":
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text(statement))


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- operators and audit -----------------------------------------------------------------

    def add_operator(
        self,
        email: str,
        name: str,
        roles: set[str],
        actor: str = "admin-cli",
        *,
        agent: bool | None = None,
        may_change_approved: bool | None = None,
    ) -> None:
        if not roles or not roles <= ROLES:
            raise ValueError(f"roles must be among {sorted(ROLES)}")
        email = email.strip().lower()
        with self.engine.begin() as db:
            existing = db.execute(select(operator).where(operator.c.email == email)).first()
            new_agent = bool(existing.agent) if (existing and agent is None) else bool(agent)
            new_may_change = (
                bool(existing.may_change_approved)
                if (existing and may_change_approved is None)
                else bool(may_change_approved)
            )
            if new_agent and roles != {"reviewer"}:
                raise ValueError("An agent account may hold only the reviewer role.")
            if (
                existing
                and new_agent != bool(existing.agent)
                and set(existing.roles.split(",")) != {"reviewer"}
            ):
                raise ValueError("An account with privileged roles may not be marked as an agent account.")
            values = {
                "name": name,
                "roles": ",".join(sorted(roles)),
                "agent": new_agent,
                "may_change_approved": new_may_change,
            }
            if existing:
                old_roles = set(existing.roles.split(","))
                # An edit of name or roles leaves a deactivated operator deactivated: only reactivate
                # brings one back (findings 2026-11-16, item 24).
                db.execute(update(operator).where(operator.c.email == email).values(**values))
                if (
                    old_roles != roles
                    or bool(existing.agent) != new_agent
                    or bool(existing.may_change_approved) != new_may_change
                ):
                    db.execute(
                        update(session)
                        .where(session.c.email == email, session.c.ended_at.is_(None))
                        .values(ended_at=now())
                    )
            else:
                db.execute(insert(operator).values(email=email, created_at=now(), active=True, **values))
            self._audit(
                db,
                actor,
                "operator_saved",
                detail={
                    "email": email,
                    "roles": sorted(roles),
                    "agent": new_agent,
                    "may_change_approved": new_may_change,
                },
            )

    def deactivate(self, email: str, actor: str = "admin-cli") -> None:
        with self.engine.begin() as db:
            db.execute(update(operator).where(operator.c.email == email.lower()).values(active=False))
            db.execute(
                update(session)
                .where(session.c.email == email.lower(), session.c.ended_at.is_(None))
                .values(ended_at=now())
            )
            self._audit(db, actor, "operator_deactivated", detail={"email": email.lower()})

    def operators(self) -> list[dict]:
        """Every operator, active or not, with their last sign-in and how many passkeys they hold."""
        with self.engine.connect() as db:
            people = db.execute(select(operator).order_by(operator.c.active.desc(), operator.c.name)).all()
            last = dict(
                db.execute(
                    select(session.c.email, func.max(session.c.created_at)).group_by(session.c.email)
                ).all()
            )
            keys = dict(
                db.execute(
                    select(passkey.c.email, func.count())
                    .where(passkey.c.revoked_at.is_(None))
                    .group_by(passkey.c.email)
                ).all()
            )
        return [
            {
                "email": row.email,
                "name": row.name,
                "roles": sorted(row.roles.split(",")),
                "active": bool(row.active),
                "agent": bool(row.agent),
                "may_change_approved": bool(row.may_change_approved),
                "created_at": row.created_at,
                "last_sign_in": last.get(row.email),
                "passkeys": keys.get(row.email, 0),
            }
            for row in people
        ]

    def reactivate(self, email: str, actor: str = "admin-cli") -> None:
        with self.engine.begin() as db:
            db.execute(update(operator).where(operator.c.email == email.lower()).values(active=True))
            self._audit(db, actor, "operator_reactivated", detail={"email": email.lower()})

    def names(self, emails) -> dict[str, str]:
        """Display names of operators, active or not, for crediting work already done."""
        wanted = {email.lower() for email in emails if email}
        if not wanted:
            return {}
        with self.engine.connect() as db:
            rows = db.execute(select(operator.c.email, operator.c.name).where(operator.c.email.in_(wanted)))
            return {row.email: row.name for row in rows}

    def operator(self, email: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(
                select(operator).where(operator.c.email == email.strip().lower(), operator.c.active.is_(True))
            ).first()
        if not row:
            return None
        return {
            **row._mapping,
            "roles": set(row.roles.split(",")),
            "active": bool(row.active),
            "agent": bool(row.agent),
            "may_change_approved": bool(row.may_change_approved),
        }

    def _audit(self, db, actor: str, action: str, act_id=None, page=None, detail=None) -> None:
        db.execute(
            insert(audit).values(
                at=now(), actor=actor, action=action, act_id=act_id, page=page, detail=detail or {}
            )
        )

    def audit(self, actor: str, action: str, act_id=None, page=None, detail=None) -> None:
        with self.engine.begin() as db:
            self._audit(db, actor, action, act_id, page, detail)

    # --- publishing ------------------------------------------------------------------------------

    def request_publication(self, email: str) -> int:
        """A run asked for now (the workbench then starts the publish job, which takes it up)."""
        with self.engine.begin() as db:
            run = db.execute(
                insert(publication_run).values(
                    requested_at=now(), trigger="manual", requested_by=email, status="requested", summary={}
                )
            ).inserted_primary_key[0]
            self._audit(db, email, "publication_requested", detail={"run": run})
        return run

    def approved_ids(self) -> set[str]:
        """Every document an approval (or its revocation) names: the only ones publishing looks at."""
        with self.engine.connect() as db:
            return {row.act_id for row in db.execute(select(approval.c.act_id).distinct())}

    def publication_runs(self, limit: int = 10) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(publication_run).order_by(publication_run.c.id.desc()).limit(limit)
            ).all()
        return [dict(row._mapping) for row in rows]

    def publication_statuses(self) -> dict[str, dict]:
        """Each document considered by a run, as the latest run left it."""
        with self.engine.connect() as db:
            rows = db.execute(select(publication_status)).all()
        return {row.document_id: dict(row._mapping) for row in rows}

    def events(self, act_id: str | None = None, limit: int = 200) -> list[dict]:
        query = select(audit).order_by(audit.c.id.desc()).limit(limit)
        if act_id:
            query = query.where(audit.c.act_id == act_id)
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]

    def activity_stamps(self) -> dict[str, int]:
        """For each document anyone has worked on, its latest audited event: a document's review
        state changes only with such an event (a page saved, an approval recorded or revoked)."""
        query = (
            select(audit.c.act_id, func.max(audit.c.id))
            .where(audit.c.act_id.is_not(None))
            .group_by(audit.c.act_id)
        )
        with self.engine.connect() as db:
            return {act_id: stamp for act_id, stamp in db.execute(query)}

    def detail_stamps(self) -> dict[str, int]:
        """For each details record anyone has saved, its latest revision."""
        query = select(detail_revision.c.act_id, func.max(detail_revision.c.revision)).group_by(
            detail_revision.c.act_id
        )
        with self.engine.connect() as db:
            return {key: revision for key, revision in db.execute(query)}

    def activity_began(self) -> dict[str, datetime]:
        """For each document anyone has worked on, when its first audited event was recorded."""
        query = select(audit.c.act_id, func.min(audit.c.at)).where(audit.c.act_id.is_not(None))
        with self.engine.connect() as db:
            return {act_id: _aware(at) for act_id, at in db.execute(query.group_by(audit.c.act_id))}

    def last_event(self, actor: str, action: str) -> dict | None:
        query = (
            select(audit)
            .where(audit.c.actor == actor, audit.c.action == action)
            .order_by(audit.c.id.desc())
            .limit(1)
        )
        with self.engine.connect() as db:
            row = db.execute(query).first()
        return dict(row._mapping) if row else None

    # --- sessions and sign-in flows -------------------------------------------------------------

    def start_session(self, email: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.engine.begin() as db:
            db.execute(
                insert(session).values(
                    token_hash=digest(token),
                    email=email.lower(),
                    csrf=csrf,
                    created_at=now(),
                    last_seen=now(),
                )
            )
            self._audit(db, email.lower(), "signed_in")
        return token, csrf

    def resume(self, token: str) -> dict | None:
        """The session behind a cookie, if live; enforces per-account idle/max limits and throttles
        last_seen updates to at most once every 60 seconds."""
        with self.engine.begin() as db:
            row = db.execute(
                select(
                    session,
                    operator.c.name.label("op_name"),
                    operator.c.roles.label("op_roles"),
                    operator.c.active.label("op_active"),
                    operator.c.agent.label("op_agent"),
                    operator.c.may_change_approved.label("op_may_change_approved"),
                    operator.c.created_at.label("op_created_at"),
                )
                .select_from(session.join(operator, session.c.email == operator.c.email))
                .where(session.c.token_hash == digest(token))
            ).first()
            if not row or row.ended_at or not row.op_active:
                return None
            person = {
                "email": row.email,
                "name": row.op_name,
                "roles": set(row.op_roles.split(",")),
                "active": bool(row.op_active),
                "agent": bool(row.op_agent),
                "may_change_approved": bool(row.op_may_change_approved),
                "created_at": row.op_created_at,
            }
            idle_limit, max_limit = session_limits(person)
            moment = now()
            if moment - _aware(row.last_seen) > idle_limit or moment - _aware(row.created_at) > max_limit:
                db.execute(
                    update(session).where(session.c.token_hash == row.token_hash).values(ended_at=moment)
                )
                return None
            if moment - _aware(row.last_seen) >= timedelta(seconds=60):
                db.execute(
                    update(session).where(session.c.token_hash == row.token_hash).values(last_seen=moment)
                )
        return {
            "email": row.email,
            "csrf": row.csrf,
            "operator": person,
            "token_hash": row.token_hash,
            "passkey_at": row.passkey_at and _aware(row.passkey_at),
        }

    def end_session(self, token: str) -> None:
        with self.engine.begin() as db:
            row = db.execute(select(session).where(session.c.token_hash == digest(token))).first()
            if row:
                db.execute(
                    update(session).where(session.c.token_hash == row.token_hash).values(ended_at=now())
                )
                self._audit(db, row.email, "signed_out")

    def list_sessions(self, email: str) -> list[dict]:
        """Live sessions for an operator: short id (first 12 chars of token_hash), created_at, last_seen."""
        email = email.strip().lower()
        person = self.operator(email)
        idle_limit, max_limit = session_limits(person)
        moment = now()
        with self.engine.connect() as db:
            rows = db.execute(
                select(session)
                .where(session.c.email == email, session.c.ended_at.is_(None))
                .order_by(session.c.created_at.desc())
            ).all()
        live = []
        for row in rows:
            created = _aware(row.created_at)
            seen = _aware(row.last_seen)
            if moment - seen <= idle_limit and moment - created <= max_limit:
                live.append(
                    {
                        "id": row.token_hash[:12],
                        "created_at": created.isoformat(),
                        "last_seen": seen.isoformat(),
                    }
                )
        return live

    def end_session_by_id(self, email: str, short_id: str, actor: str = "admin-cli") -> bool:
        """End one live session identified by the first 12 characters of its token hash."""
        email = email.strip().lower()
        if not short_id or len(short_id) < 12 or not re.fullmatch(r"[0-9a-f]+", short_id):
            return False
        with self.engine.begin() as db:
            done = db.execute(
                update(session)
                .where(
                    session.c.email == email,
                    session.c.token_hash.startswith(short_id),
                    session.c.ended_at.is_(None),
                )
                .values(ended_at=now())
            ).rowcount
            if done:
                self._audit(
                    db,
                    actor,
                    "session_ended",
                    detail={"email": email, "session_id": short_id[:12]},
                )
        return bool(done)

    def end_all_sessions(self, email: str, actor: str = "admin-cli") -> int:
        """End every live session for an operator."""
        email = email.strip().lower()
        with self.engine.begin() as db:
            done = db.execute(
                update(session)
                .where(session.c.email == email, session.c.ended_at.is_(None))
                .values(ended_at=now())
            ).rowcount
            self._audit(
                db,
                actor,
                "sessions_ended_all",
                detail={"email": email, "count": int(done or 0)},
            )
        return int(done or 0)

    def has_agent_accounts(self) -> bool:
        """Whether any active account is marked as an agent's."""
        with self.engine.connect() as db:
            return (
                db.execute(
                    select(operator.c.email)
                    .where(operator.c.agent.is_(True), operator.c.active.is_(True))
                    .limit(1)
                ).first()
                is not None
            )

    def pause_all_agents(self, actor: str = "admin-cli") -> list[str]:
        """Deactivate every active agent account and end all of their live sessions."""
        with self.engine.begin() as db:
            rows = db.execute(
                select(operator.c.email).where(operator.c.agent.is_(True), operator.c.active.is_(True))
            ).all()
            emails = sorted(row.email for row in rows)
            if emails:
                db.execute(update(operator).where(operator.c.email.in_(emails)).values(active=False))
                db.execute(
                    update(session)
                    .where(session.c.email.in_(emails), session.c.ended_at.is_(None))
                    .values(ended_at=now())
                )
            self._audit(
                db,
                actor,
                "agents_paused_all",
                detail={"accounts": emails, "count": len(emails)},
            )
        return emails

    # --- passkeys (Access section) --------------------------------------------------------------

    def issue_enrolment(self, email: str, actor: str = "admin-cli") -> str:
        """A one-time code (24 hours) with which an operator registers a passkey; printed once."""
        email = email.strip().lower()
        if not self.operator(email):
            raise ValueError("No active operator with that email")
        alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
        code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))
        with self.engine.begin() as db:
            db.execute(insert(enrolment).values(code_hash=digest(code), email=email, created_at=now()))
            self._audit(db, actor, "passkey_enrolment_issued", detail={"email": email})
        return code

    def take_enrolment(self, email: str, code: str) -> bool:
        """Spend a code: only the operator it was issued to, once, within 24 hours. Spending it is one
        conditional write, so two requests racing with the same code cannot both succeed."""
        letters = "".join(c for c in code.upper() if c.isalnum())
        code = "-".join(letters[i : i + 4] for i in range(0, len(letters), 4))
        with self.engine.begin() as db:
            row = db.execute(
                update(enrolment)
                .where(
                    enrolment.c.code_hash == digest(code),
                    enrolment.c.email == email,
                    enrolment.c.used_at.is_(None),
                )
                .values(used_at=now())
                .returning(enrolment.c.created_at)
            ).first()
        # A code past its time is spent all the same: it was never going to be good again.
        return row is not None and now() - _aware(row.created_at) <= ENROLMENT_TTL

    def passkeys(self, email: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(passkey)
                .where(passkey.c.email == email, passkey.c.revoked_at.is_(None))
                .order_by(passkey.c.created_at)
            )
            return [dict(row._mapping) for row in rows]

    def add_passkey(self, email: str, record: dict, name: str) -> None:
        with self.engine.begin() as db:
            db.execute(insert(passkey).values(email=email, name=name, created_at=now(), **record))
            self._audit(db, email, "passkey_added", detail={"name": name})

    def revoke_passkey(self, email: str, credential_id: str) -> bool:
        with self.engine.begin() as db:
            done = db.execute(
                update(passkey)
                .where(
                    passkey.c.credential_id == credential_id,
                    passkey.c.email == email,
                    passkey.c.revoked_at.is_(None),
                )
                .values(revoked_at=now())
            ).rowcount
            if done:
                self._audit(db, email, "passkey_removed", detail={"credential": credential_id[:16]})
            return bool(done)

    def begin_challenge(self, token_hash: str, purpose: str) -> str:
        challenge = secrets.token_urlsafe(32)
        with self.engine.begin() as db:
            db.execute(
                update(session)
                .where(session.c.token_hash == token_hash)
                .values(challenge=challenge, challenge_purpose=purpose, challenge_at=now())
            )
        return challenge

    def take_challenge(self, token_hash: str, purpose: str) -> str | None:
        """The session's open challenge for this purpose, once, within five minutes. Taking it clears it,
        whatever its purpose, and only where it is still the one read: of two requests racing for it, the
        second finds it gone and gets nothing."""
        with self.engine.begin() as db:
            row = db.execute(select(session).where(session.c.token_hash == token_hash)).first()
            if not row or not row.challenge:
                return None
            taken = db.execute(
                update(session)
                .where(session.c.token_hash == token_hash, session.c.challenge == row.challenge)
                .values(challenge=None, challenge_purpose=None, challenge_at=None)
            ).rowcount
        if not taken or row.challenge_purpose != purpose:
            return None
        return row.challenge if now() - _aware(row.challenge_at) <= CHALLENGE_TTL else None

    def passkey_checked(self, token_hash: str, email: str, credential_id: str, counter: int) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(passkey)
                .where(passkey.c.credential_id == credential_id)
                .values(sign_count=counter, last_used_at=now())
            )
            db.execute(update(session).where(session.c.token_hash == token_hash).values(passkey_at=now()))
            self._audit(db, email, "passkey_checked")

    def begin_flow(self, nonce: str, verifier: str) -> str:
        state = secrets.token_urlsafe(32)
        with self.engine.begin() as db:
            db.execute(
                insert(auth_flow).values(state=state, nonce=nonce, verifier=verifier, created_at=now())
            )
        return state

    def take_flow(self, state: str) -> dict | None:
        """One use only, within ten minutes: deleted by the write that reads it, so two callbacks with the
        same state cannot both have it."""
        with self.engine.begin() as db:
            row = db.execute(
                auth_flow.delete().where(auth_flow.c.state == state).returning(*auth_flow.c)
            ).first()
        if not row:
            return None
        return dict(row._mapping) if now() - _aware(row.created_at) < timedelta(minutes=10) else None

    # --- page revisions ------------------------------------------------------------------------

    def latest_pages(self, act_id: str) -> dict[int, dict]:
        latest = (
            select(page_revision.c.page, func.max(page_revision.c.revision).label("revision"))
            .where(page_revision.c.act_id == act_id)
            .group_by(page_revision.c.page)
            .subquery()
        )
        query = (
            select(page_revision)
            .join(
                latest,
                (page_revision.c.page == latest.c.page) & (page_revision.c.revision == latest.c.revision),
            )
            .where(page_revision.c.act_id == act_id)
        )
        with self.engine.connect() as db:
            return {row.page: dict(row._mapping) for row in db.execute(query)}

    def page_history(self, act_id: str, page: int) -> list[dict]:
        query = (
            select(page_revision)
            .where(page_revision.c.act_id == act_id, page_revision.c.page == page)
            .order_by(page_revision.c.revision.desc())
        )
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]

    def save_page(
        self,
        act_id: str,
        page: int,
        expected: int,
        author: str,
        *,
        label: str | None = None,
        task: str | None = None,
        db=None,
        **values,
    ) -> int:
        """Write revision expected+1; Conflict if someone else saved since the client loaded."""

        def _write(conn) -> int:
            current = (
                conn.execute(
                    select(func.max(page_revision.c.revision)).where(
                        page_revision.c.act_id == act_id, page_revision.c.page == page
                    )
                ).scalar()
                or 0
            )
            if current != expected:
                raise Conflict(f"page {page} is at revision {current}, not {expected}")
            try:
                conn.execute(
                    insert(page_revision).values(
                        act_id=act_id,
                        page=page,
                        revision=expected + 1,
                        author=author,
                        label=label,
                        task=task,
                        created_at=db_now(conn),
                        **values,
                    )
                )
            except IntegrityError as error:
                raise Conflict(str(error)) from error
            self._audit(
                conn,
                author,
                "page_saved",
                act_id,
                page,
                {
                    "revision": expected + 1,
                    "status": values["status"],
                    "resolved": len(values["resolutions"]),
                },
            )
            return expected + 1

        if db is not None:
            return _write(db)
        with self.engine.begin() as conn:
            return _write(conn)

    # --- document details ----------------------------------------------------------------------

    def detail_history(self, act_id: str) -> list[dict]:
        query = (
            select(detail_revision)
            .where(detail_revision.c.act_id == act_id)
            .order_by(detail_revision.c.revision.desc())
        )
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]

    def latest_details(self, act_id: str) -> dict | None:
        history = self.detail_history(act_id)
        return history[0] if history else None

    def save_details(
        self,
        act_id: str,
        expected: int,
        author: str,
        *,
        label: str | None = None,
        task: str | None = None,
        db=None,
        **values,
    ) -> int:
        """Write details revision expected+1; Conflict if someone else saved since the client loaded."""

        def _write(conn) -> int:
            current = (
                conn.execute(
                    select(func.max(detail_revision.c.revision)).where(detail_revision.c.act_id == act_id)
                ).scalar()
                or 0
            )
            if current != expected:
                raise Conflict(f"details are at revision {current}, not {expected}")
            try:
                conn.execute(
                    insert(detail_revision).values(
                        act_id=act_id,
                        revision=expected + 1,
                        author=author,
                        label=label,
                        task=task,
                        created_at=db_now(conn),
                        **values,
                    )
                )
            except IntegrityError as error:
                raise Conflict(str(error)) from error
            self._audit(
                conn,
                author,
                "details_saved",
                act_id,
                None,
                {"revision": expected + 1, "status": values["status"], "given": len(values["details"])},
            )
            return expected + 1

        if db is not None:
            return _write(db)
        with self.engine.begin() as conn:
            return _write(conn)

    # --- legal events, amendment instructions (workbench.editorial) and citations (workbench.citations) ---

    def _revisions(self, table, work: str | None) -> list[dict]:
        """Every revision, oldest first: of one work's items, or of all."""
        query = select(table).order_by(table.c.id)
        if work is not None:
            query = query.where(table.c.work == work)
        with self.engine.connect() as db:
            return [{**row._mapping, "created_at": _aware(row.created_at)} for row in db.execute(query)]

    def _save_revision(self, table, key: str, item: str, expected: int, author: str, action: str, **values):
        """Write the item's revision expected+1; Conflict if someone else saved since the client loaded,
        or if the item belongs to another work."""
        with self.engine.begin() as db:
            held = db.execute(
                select(table.c.revision, table.c.work)
                .where(table.c[key] == item)
                .order_by(table.c.revision.desc())
                .limit(1)
            ).first()
            current = held.revision if held else 0
            if current != expected:
                raise Conflict(f"{item} is at revision {current}, not {expected}")
            if held and held.work != values["work"]:
                raise Conflict(f"{item} belongs to {held.work}")
            try:
                db.execute(
                    insert(table).values(
                        **{key: item}, revision=expected + 1, author=author, created_at=now(), **values
                    )
                )
            except IntegrityError as error:
                raise Conflict(str(error)) from error
            self._audit(
                db,
                author,
                action,
                values["act_id"],
                None,
                {key: item, "revision": expected + 1, "status": values["status"]},
            )
        return expected + 1

    def legal_event_revisions(self, work: str | None = None) -> list[dict]:
        return self._revisions(legal_event_revision, work)

    def save_legal_event(self, event_id: str, expected: int, author: str, **values) -> int:
        return self._save_revision(
            legal_event_revision, "event_id", event_id, expected, author, "legal_event_saved", **values
        )

    def instruction_revisions(self, work: str | None = None) -> list[dict]:
        return self._revisions(instruction_revision, work)

    def citation_revisions(self, work: str | None = None) -> list[dict]:
        return self._revisions(citation_revision, work)

    def save_citation(self, citation_id: str, expected: int, author: str, **values) -> int:
        return self._save_revision(
            citation_revision, "citation_id", citation_id, expected, author, "citation_saved", **values
        )

    def save_instruction(self, instruction_id: str, expected: int, author: str, **values) -> int:
        table, action = instruction_revision, "instruction_saved"
        return self._save_revision(
            table, "instruction_id", instruction_id, expected, author, action, **values
        )

    # --- approvals -----------------------------------------------------------------------------

    def approvals(self, act_id: str) -> list[dict]:
        query = select(approval).where(approval.c.act_id == act_id).order_by(approval.c.revision.desc())
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]

    def record_approval(self, act_id: str, expected: int, author: str, **values) -> int:
        with self.engine.begin() as db:
            current = (
                db.execute(select(func.max(approval.c.revision)).where(approval.c.act_id == act_id)).scalar()
                or 0
            )
            if current != expected:
                raise Conflict(f"approval is at revision {current}, not {expected}")
            db.execute(
                insert(approval).values(
                    act_id=act_id, revision=expected + 1, author=author, created_at=now(), **values
                )
            )
            self._audit(
                db,
                author,
                f"act_{values['decision']}",
                act_id,
                None,
                {
                    "revision": expected + 1,
                    "content_sha256": values["content_sha256"],
                    "text_sha256": values.get("text_sha256"),
                },
            )
        return expected + 1

    # --- review audits (section 7.2) -----------------------------------------------------------

    def create_audit_draw(
        self,
        *,
        work_id: str,
        act_id: str,
        page: int,
        release_id: int,
        reviewer: str,
        reviewer_label: str,
        reviewer_revision: int,
        db=None,
    ) -> int:
        """Record a drawn page in review_audit."""

        def _write(conn) -> int:
            return int(
                conn.execute(
                    insert(review_audit).values(
                        work_id=work_id,
                        act_id=act_id,
                        page=page,
                        release_id=release_id,
                        reviewer=reviewer.strip().lower(),
                        reviewer_label=reviewer_label,
                        reviewer_revision=reviewer_revision,
                        drawn_at=db_now(conn),
                    )
                ).inserted_primary_key[0]
            )

        if db is not None:
            return _write(db)
        with self.engine.begin() as conn:
            return _write(conn)

    def save_audit_submission(
        self,
        audit_id: int,
        *,
        auditor: str,
        auditor_label: str,
        units: list[dict],
        resolutions: dict,
        status: str,
        paragraphs_changed: int | None = None,
        words_changed: int | None = None,
        other_changes: int | None = None,
        verdict: str | None = None,
        db=None,
    ) -> dict:
        """Store an auditor's page submission on review_audit without touching live page_revision."""
        auditor = auditor.strip().lower()

        def _write(conn) -> dict:
            row = conn.execute(select(review_audit).where(review_audit.c.id == audit_id)).first()
            if row is None:
                raise KeyError(f"No review_audit row {audit_id}")
            if row.audited_at is not None:
                raise Conflict(f"Audit {audit_id} was already submitted")
            if row.reviewer == auditor:
                raise ValueError("Auditor account must differ from reviewer account")
            conn.execute(
                update(review_audit)
                .where(review_audit.c.id == audit_id)
                .values(
                    auditor=auditor,
                    auditor_label=auditor_label,
                    auditor_units=units,
                    auditor_resolutions=resolutions,
                    auditor_status=status,
                    audited_at=db_now(conn),
                    paragraphs_changed=paragraphs_changed,
                    words_changed=words_changed,
                    other_changes=other_changes,
                    verdict=verdict,
                )
            )
            self._audit(
                conn,
                auditor,
                "audit_submitted",
                detail={
                    "audit_id": audit_id,
                    "work_id": row.work_id,
                    "act_id": row.act_id,
                    "page": row.page,
                    "label": auditor_label,
                    "verdict": verdict,
                },
            )
            updated = conn.execute(select(review_audit).where(review_audit.c.id == audit_id)).first()
            return dict(updated._mapping)

        if db is not None:
            return _write(db)
        with self.engine.begin() as conn:
            return _write(conn)

    def resolve_audit(
        self,
        audit_id: int,
        *,
        decision: str,
        decided_by: str,
        reading_sha256: str | None = None,
        comment: str = "",
    ) -> dict:
        """Record an owner's decision on an audited page. Only accepted_auditor writes a live
        page_revision (under the owner's account)."""
        if decision not in {"accepted_auditor", "kept_reviewer"}:
            raise ValueError("decision must be accepted_auditor or kept_reviewer")
        decided_by = decided_by.strip().lower()
        with self.engine.begin() as conn:
            row = conn.execute(select(review_audit).where(review_audit.c.id == audit_id)).first()
            if row is None:
                raise KeyError(f"No review_audit row {audit_id}")
            if row.audited_at is None:
                raise ValueError(f"Audit {audit_id} has not been submitted yet")
            if row.owner_decision is not None:
                raise Conflict(f"Audit {audit_id} has already been resolved")
            conn.execute(
                update(review_audit)
                .where(review_audit.c.id == audit_id)
                .values(
                    owner_decision=decision,
                    owner_decided_by=decided_by,
                    owner_decided_at=db_now(conn),
                )
            )
            new_revision = None
            if decision == "accepted_auditor":
                latest = conn.execute(
                    select(page_revision)
                    .where(page_revision.c.act_id == row.act_id, page_revision.c.page == row.page)
                    .order_by(page_revision.c.revision.desc())
                    .limit(1)
                ).first()
                expected = latest.revision if latest else 0
                if expected != int(row.reviewer_revision or 0):
                    # The auditor's text would go over an edit made after the audit was drawn.
                    raise Conflict(
                        f"Page {row.page} has been changed since this audit was drawn (it is at revision "
                        f"{expected}, the audit read revision {row.reviewer_revision}). Nothing was changed: "
                        "correct the page itself, or keep the reviewer's text."
                    )
                approved = conn.execute(
                    select(approval.c.decision)
                    .where(approval.c.act_id == row.act_id)
                    .order_by(approval.c.revision.desc())
                    .limit(1)
                ).scalar()
                if approved == "approved":
                    # Changing an approved document's text ends its approval, and its publication that night.
                    raise Conflict(
                        "This document has been approved since the audit was drawn. Nothing was changed: "
                        "correct the page itself if it must change, or keep the reviewer's text."
                    )
                sha = reading_sha256 or (latest.reading_sha256 if latest else "")
                new_revision = self.save_page(
                    row.act_id,
                    row.page,
                    expected,
                    decided_by,
                    reading_sha256=sha,
                    units=row.auditor_units or [],
                    status=row.auditor_status or "checked",
                    resolutions=row.auditor_resolutions or {},
                    comment=comment or f"Accepted audit #{audit_id}",
                    db=conn,
                )
            self._audit(
                conn,
                decided_by,
                "audit_resolved",
                detail={
                    "audit_id": audit_id,
                    "work_id": row.work_id,
                    "act_id": row.act_id,
                    "page": row.page,
                    "decision": decision,
                    "new_revision": new_revision,
                },
            )
            updated = conn.execute(select(review_audit).where(review_audit.c.id == audit_id)).first()
            return {**updated._mapping, "new_revision": new_revision}

    def list_audits(
        self,
        *,
        work_id: str | None = None,
        open_only: bool = False,
        verdict: str | None = None,
        unresolved_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """List review_audit rows matching the given filters."""
        query = select(review_audit).order_by(review_audit.c.id.desc()).limit(limit)
        if work_id is not None:
            query = query.where(review_audit.c.work_id == work_id)
        if open_only:
            query = query.where(review_audit.c.audited_at.is_(None))
        if verdict is not None:
            query = query.where(review_audit.c.verdict == verdict)
        if unresolved_only:
            query = query.where(review_audit.c.owner_decision.is_(None))
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
