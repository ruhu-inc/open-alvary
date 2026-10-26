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
    engine = create_engine(url, future=True, pool_pre_ping=True)
    metadata.create_all(engine)
    _upgrade(engine)
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


def _upgrade(engine: Engine) -> None:
    """Columns added after a database was first created (create_all adds only missing tables)."""
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
    }
    for table, columns in added.items():
        present = {column["name"] for column in inspect(engine).get_columns(table)}
        for name, kind in columns.items():
            if name not in present:
                with engine.begin() as db:
                    db.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- operators and audit -----------------------------------------------------------------

    def add_operator(self, email: str, name: str, roles: set[str], actor: str = "admin-cli") -> None:
        if not roles or not roles <= ROLES:
            raise ValueError(f"roles must be among {sorted(ROLES)}")
        email = email.strip().lower()
        with self.engine.begin() as db:
            existing = db.execute(select(operator).where(operator.c.email == email)).first()
            values = {"name": name, "roles": ",".join(sorted(roles)), "active": True}
            if existing:
                db.execute(update(operator).where(operator.c.email == email).values(**values))
            else:
                db.execute(insert(operator).values(email=email, created_at=now(), **values))
            self._audit(db, actor, "operator_saved", detail={"email": email, "roles": sorted(roles)})

    def deactivate(self, email: str, actor: str = "admin-cli") -> None:
        with self.engine.begin() as db:
            db.execute(update(operator).where(operator.c.email == email.lower()).values(active=False))
            db.execute(update(session).where(session.c.email == email.lower()).values(ended_at=now()))
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
        return {**row._mapping, "roles": set(row.roles.split(","))} if row else None

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
        """The session behind a cookie, if live; refreshes its idle timer."""
        with self.engine.begin() as db:
            row = db.execute(select(session).where(session.c.token_hash == digest(token))).first()
            if not row or row.ended_at:
                return None
            moment = now()
            if moment - _aware(row.last_seen) > SESSION_IDLE or moment - _aware(row.created_at) > SESSION_MAX:
                db.execute(
                    update(session).where(session.c.token_hash == row.token_hash).values(ended_at=moment)
                )
                return None
            db.execute(update(session).where(session.c.token_hash == row.token_hash).values(last_seen=moment))
        person = self.operator(row.email)
        if not person:
            return None
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
        """Spend a code: only the operator it was issued to, once, within 24 hours."""
        letters = "".join(c for c in code.upper() if c.isalnum())
        code = "-".join(letters[i : i + 4] for i in range(0, len(letters), 4))
        with self.engine.begin() as db:
            row = db.execute(
                select(enrolment).where(enrolment.c.code_hash == digest(code), enrolment.c.email == email)
            ).first()
            if not row or row.used_at or now() - _aware(row.created_at) > ENROLMENT_TTL:
                return False
            db.execute(update(enrolment).where(enrolment.c.code_hash == row.code_hash).values(used_at=now()))
        return True

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
        """The session's open challenge for this purpose, once, within five minutes."""
        with self.engine.begin() as db:
            row = db.execute(select(session).where(session.c.token_hash == token_hash)).first()
            db.execute(
                update(session)
                .where(session.c.token_hash == token_hash)
                .values(challenge=None, challenge_purpose=None, challenge_at=None)
            )
        if not row or not row.challenge or row.challenge_purpose != purpose:
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
        """One use only, within ten minutes."""
        with self.engine.begin() as db:
            row = db.execute(select(auth_flow).where(auth_flow.c.state == state)).first()
            if not row:
                return None
            db.execute(auth_flow.delete().where(auth_flow.c.state == state))
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

    def save_page(self, act_id: str, page: int, expected: int, author: str, **values) -> int:
        """Write revision expected+1; Conflict if someone else saved since the client loaded."""
        with self.engine.begin() as db:
            current = (
                db.execute(
                    select(func.max(page_revision.c.revision)).where(
                        page_revision.c.act_id == act_id, page_revision.c.page == page
                    )
                ).scalar()
                or 0
            )
            if current != expected:
                raise Conflict(f"page {page} is at revision {current}, not {expected}")
            try:
                db.execute(
                    insert(page_revision).values(
                        act_id=act_id,
                        page=page,
                        revision=expected + 1,
                        author=author,
                        created_at=now(),
                        **values,
                    )
                )
            except IntegrityError as error:
                raise Conflict(str(error)) from error
            self._audit(
                db,
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

    def save_details(self, act_id: str, expected: int, author: str, **values) -> int:
        """Write details revision expected+1; Conflict if someone else saved since the client loaded."""
        with self.engine.begin() as db:
            current = (
                db.execute(
                    select(func.max(detail_revision.c.revision)).where(detail_revision.c.act_id == act_id)
                ).scalar()
                or 0
            )
            if current != expected:
                raise Conflict(f"details are at revision {current}, not {expected}")
            try:
                db.execute(
                    insert(detail_revision).values(
                        act_id=act_id, revision=expected + 1, author=author, created_at=now(), **values
                    )
                )
            except IntegrityError as error:
                raise Conflict(str(error)) from error
            self._audit(
                db,
                author,
                "details_saved",
                act_id,
                None,
                {"revision": expected + 1, "status": values["status"], "given": len(values["details"])},
            )
        return expected + 1

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


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
