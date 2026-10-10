"""Workflow database for the review workbench.

Every review action is an immutable row: a page revision is never updated, only superseded by
the next revision number for that page; approvals and audit events are append-only. PostgreSQL
in production (ALVARY_REVIEW_DATABASE_URL); SQLite is accepted for a local trial and tests.
"""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
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
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

ROLES = {"reviewer", "approver", "admin"}
SESSION_IDLE = timedelta(hours=1)
SESSION_MAX = timedelta(hours=12)

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

approval = Table(
    "approval",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("act_id", String(140), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("decision", String(20), nullable=False),  # approved | revoked
    Column("content_sha256", String(64), nullable=False),
    Column("rights_basis", Text, nullable=False),
    Column("materials", JSON, nullable=False),
    Column("note", Text, nullable=False, default=""),
    Column("author", String(320), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("act_id", "revision"),
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
    return engine


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

    def events(self, act_id: str | None = None, limit: int = 200) -> list[dict]:
        query = select(audit).order_by(audit.c.id.desc()).limit(limit)
        if act_id:
            query = query.where(audit.c.act_id == act_id)
        with self.engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(query)]

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
        return {"email": row.email, "csrf": row.csrf, "operator": person} if person else None

    def end_session(self, token: str) -> None:
        with self.engine.begin() as db:
            row = db.execute(select(session).where(session.c.token_hash == digest(token))).first()
            if row:
                db.execute(
                    update(session).where(session.c.token_hash == row.token_hash).values(ended_at=now())
                )
                self._audit(db, row.email, "signed_out")

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
                {"revision": expected + 1, "content_sha256": values["content_sha256"]},
            )
        return expected + 1


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
