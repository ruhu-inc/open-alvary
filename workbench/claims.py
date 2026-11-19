"""Atomic work claiming, lease renewal, release, blocked labels, and save guards (sections 4 & 5).

One claim covers a whole work (Review.details_key(document): all language versions and their shared
Details). The database enforces both "one holder per work" (primary key on work_claim.work_id) and
"one claim per agent label" (partial unique index on (account, label) WHERE label <> 'unlabelled').
"""

import os
import random
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from workbench import audit as audit_settings
from workbench.store import (
    Store,
    _aware,
    approval,
    audit,
    blocked_label,
    db_now,
    digest,
    now,
    work_claim,
    work_release,
)

LABEL_RE = re.compile(r"^[A-Za-z0-9-]{3,40}$")
TASKS = {"review", "status", "audit"}
OUTCOMES = {"done", "parked", "abandoned"}
STATUS_FAMILIES = {
    "constitution",
    "legislation",
    "subsidiary_legislation",
    "executive_instrument",
    "treaty",
    "regional_instrument",
}

# Exact user-facing messages (section 16).
MSG_HELD_BY_OTHER = (
    "This work is being reviewed by {label} ({account}) until {time} UTC. "
    "Nothing was saved. Claim another work."
)
MSG_APPROVED = "This document is approved. An agent account may not change it."
MSG_AGENT_NON_REVIEW = "Agent accounts may not change legal events, amendment instructions, or citations."
MSG_BLOCKED_LABEL = "The label {label} has been paused by the owner. Stop and report."
MSG_ONE_CLAIM_PER_LABEL = "{label} already holds {work}. Release it before claiming another."
MSG_NOTHING_TO_CLAIM = "Nothing to claim under these limits."
MSG_AGENT_NEEDS_CLAIM = "Claim this work first: press Claim next on the Work queue."
MSG_RATE_LIMIT = "Too many claim requests for {label}. Wait a minute and try again."
MSG_CLAIM_GONE = (
    "Your claim on this work has ended. Nothing was saved. Claim it again, or claim another work."
)
MSG_CLAIMS_OFF = "Claims are switched off."
MSG_AUDIT_NEEDS_ENFORCE = "Audits run only while claims are enforced."
MSG_LEFT_FOR_OWNER = "This work was left for the owner. Claim another work."

UNLABELLED = "unlabelled"  # a person's own claim, taken by their first save


class ClaimError(HTTPException):
    """A refusal by a claim operation or a save guard. Its body carries a code beside the message, so
    the page can tell it from a save that met a newer revision (a 409 too, with no code) and show these
    words instead of reloading the page."""

    def __init__(self, status_code: int, message: str, code: str = "claim"):
        super().__init__(status_code, {"code": code, "message": message})
        self.message = message
        self.code = code


def claims_mode() -> str:
    mode = os.environ.get("ALVARY_REVIEW_CLAIMS", "off").strip().lower()
    return mode if mode in {"off", "warn", "enforce"} else "off"


def approved_guard_mode() -> str:
    mode = os.environ.get("ALVARY_REVIEW_APPROVED_GUARD", "off").strip().lower()
    return mode if mode in {"off", "enforce"} else "off"


def check_settings() -> None:
    """Refuse to start on a setting that is set to something it cannot be. A mistyped switch would
    otherwise read as "off" without a word (ALVARY_REVIEW_APPROVED_GUARD=on would guard nothing), and a
    bad number would fail later: at every agent's sign-in, or after a release had been saved."""
    problems: list[str] = []

    def choice(name: str, allowed: set[str]) -> None:
        raw = (os.environ.get(name) or "").strip()
        if raw and raw.lower() not in allowed:
            problems.append(f"{name} must be one of {', '.join(sorted(allowed))}, not {raw!r}")

    def number(name: str, *, whole: bool) -> None:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return
        try:
            value = int(raw) if whole else float(raw)
        except ValueError:
            value = 0
        if not (value >= 1 if whole else value > 0):  # false for nan as well
            wanted = "a whole number of 1 or more" if whole else "a number above 0"
            problems.append(f"{name} must be {wanted}, not {raw!r}")

    choice("ALVARY_REVIEW_CLAIMS", {"off", "warn", "enforce"})
    choice("ALVARY_REVIEW_APPROVED_GUARD", {"off", "enforce"})
    choice("ALVARY_REVIEW_STATUS_GATE", {"off", "on"})
    choice("ALVARY_REVIEW_HAND_ON_LAPSED", {"off", "on"})
    number("ALVARY_REVIEW_CLAIM_MINUTES", whole=True)
    number("ALVARY_REVIEW_AGENT_IDLE_HOURS", whole=False)
    number("ALVARY_REVIEW_AGENT_MAX_HOURS", whole=False)
    try:
        audit_settings.audit_config()
    except ValueError as error:
        problems.append(str(error))
    if problems:
        raise SystemExit("workbench: " + "; ".join(problems))


def claim_minutes() -> int:
    try:
        return max(1, int(os.environ.get("ALVARY_REVIEW_CLAIM_MINUTES", "60")))
    except ValueError:
        return 60


def validate_label(label: str) -> str:
    """A label as it is stored and compared: lower case, so CLAUDE-M1-02 is claude-m1-02 to a block."""
    cleaned = str(label or "").strip().lower()
    if cleaned == UNLABELLED or not LABEL_RE.fullmatch(cleaned):
        raise ClaimError(422, "Label must be 3 to 40 letters, digits, or hyphens.", "claim_label")
    return cleaned


@dataclass(frozen=True)
class Held:
    """A work's claim row as one transaction saw it."""

    account: str
    label: str
    token_hash: str
    task: str
    expires_at: datetime
    live: bool  # not yet lapsed
    due: bool  # last renewed a minute or more ago


def _held(db: Connection, work_id: str, *, lock: bool = True) -> Held | None:
    """The work's claim, whether it is live, and whether it is due a renewal. On PostgreSQL the row is
    locked until the caller's transaction ends and both answers are the database's (design 4.3): a
    takeover, a renewal or a release of the same work waits its turn, so nothing is decided on a row
    that has changed since it was read, or by the clock of one copy of the workbench."""
    if db.dialect.name == "postgresql":
        row = db.execute(
            text(
                "SELECT account, label, token_hash, task, expires_at, "
                "       expires_at > CURRENT_TIMESTAMP AS live, "
                "       CURRENT_TIMESTAMP - renewed_at >= INTERVAL '60 seconds' AS due "
                "  FROM work_claim WHERE work_id = :work_id" + (" FOR UPDATE" if lock else "")
            ),
            {"work_id": work_id},
        ).first()
        if row is None:
            return None
        return Held(
            row.account,
            row.label,
            row.token_hash,
            row.task,
            _aware(row.expires_at),
            bool(row.live),
            bool(row.due),
        )
    row = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
    if row is None:
        return None
    moment = now()
    expires_at = _aware(row.expires_at)
    return Held(
        row.account,
        row.label,
        row.token_hash,
        row.task,
        expires_at,
        expires_at > moment,
        moment - _aware(row.renewed_at) >= timedelta(seconds=60),
    )


def _extend(
    db: Connection, work_id: str, token_hash: str, minutes: int, *, new_hash: str | None = None
) -> datetime | None:
    """Renew the claim that still carries ``token_hash`` (and give it ``new_hash``, if any); when it
    now ends, or None if no such claim was there to renew: the caller then refuses, it never goes on."""
    if db.dialect.name == "postgresql":
        found = db.execute(
            text(
                "UPDATE work_claim "
                "   SET token_hash = :new_hash, renewed_at = CURRENT_TIMESTAMP, "
                "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                " WHERE work_id = :work_id AND token_hash = :token_hash "
                "RETURNING expires_at"
            ),
            {
                "new_hash": new_hash or token_hash,
                "minutes": minutes,
                "work_id": work_id,
                "token_hash": token_hash,
            },
        ).scalar()
        return _aware(found) if found is not None else None
    moment = now()
    expires_at = moment + timedelta(minutes=minutes)
    changed = db.execute(
        update(work_claim)
        .where(work_claim.c.work_id == work_id, work_claim.c.token_hash == token_hash)
        .values(token_hash=new_hash or token_hash, renewed_at=moment, expires_at=expires_at)
    ).rowcount
    return expires_at if changed == 1 else None


def _held_message(label: str, account: str, expires_at: datetime) -> str:
    clock = _aware(expires_at).astimezone(UTC).strftime("%H:%M")
    return MSG_HELD_BY_OTHER.format(label=label, account=account, time=clock)


def _is_unique_violation(error: IntegrityError) -> bool:
    orig = getattr(error, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code == "23505":
        return True
    return "UNIQUE constraint failed" in str(orig or error)


def _is_deadlock(error: OperationalError) -> bool:
    orig = getattr(error, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code == "40P01":
        return True
    text_msg = str(orig or error).lower()
    return "deadlock" in text_msg or "database is locked" in text_msg


def _audit_op(db: Connection, actor: str, action: str, detail: dict) -> None:
    """Operational audit event: always act_id=NULL so activity_stamps() reflects review mutations only."""
    db.execute(
        insert(audit).values(at=db_now(db), actor=actor, action=action, act_id=None, page=None, detail=detail)
    )


def _extract_work_id(work_or_doc: str | dict) -> tuple[str, str | None]:
    if isinstance(work_or_doc, dict):
        return str(work_or_doc.get("work") or work_or_doc["id"]), str(work_or_doc.get("id") or "")
    return str(work_or_doc), None


# --- blocked labels (section 3.2) ----------------------------------------------------------------


def is_label_blocked(db_or_store: Connection | Engine | Store, account: str, label: str) -> dict | None:
    account = account.strip().lower()
    query = select(blocked_label).where(
        blocked_label.c.account == account,
        blocked_label.c.label == label,
    )
    if isinstance(db_or_store, Connection):
        row = db_or_store.execute(query).first()
    else:
        engine = db_or_store.engine if isinstance(db_or_store, Store) else db_or_store
        with engine.connect() as db:
            row = db.execute(query).first()
    return dict(row._mapping) if row else None


def block_label(store: Store, account: str, label: str, *, blocked_by: str, reason: str = "") -> dict:
    account = account.strip().lower()
    blocked_by = blocked_by.strip().lower()
    label = validate_label(label)
    reason = str(reason or "").strip()[:1000]
    with store.engine.begin() as db:
        if db.dialect.name == "postgresql":
            db.execute(
                text(
                    "INSERT INTO blocked_label (account, label, blocked_at, blocked_by, reason) "
                    "VALUES (:account, :label, CURRENT_TIMESTAMP, :blocked_by, :reason) "
                    "ON CONFLICT (account, label) DO UPDATE "
                    "SET blocked_at = CURRENT_TIMESTAMP, blocked_by = :blocked_by, reason = :reason"
                ),
                {"account": account, "label": label, "blocked_by": blocked_by, "reason": reason},
            )
        else:
            db.execute(
                text(
                    "INSERT INTO blocked_label (account, label, blocked_at, blocked_by, reason) "
                    "VALUES (:account, :label, :now, :blocked_by, :reason) "
                    "ON CONFLICT (account, label) DO UPDATE "
                    "SET blocked_at = :now, blocked_by = :blocked_by, reason = :reason"
                ),
                {
                    "account": account,
                    "label": label,
                    "now": now(),
                    "blocked_by": blocked_by,
                    "reason": reason,
                },
            )
        _audit_op(
            db,
            blocked_by,
            "label_blocked",
            {"account": account, "label": label, "reason": reason},
        )
        row = db.execute(
            select(blocked_label).where(blocked_label.c.account == account, blocked_label.c.label == label)
        ).first()
    return {
        "account": row.account,
        "label": row.label,
        "blocked_at": _aware(row.blocked_at).isoformat(),
        "blocked_by": row.blocked_by,
        "reason": row.reason,
    }


def unblock_label(store: Store, account: str, label: str, *, actor: str) -> bool:
    account = account.strip().lower()
    actor = actor.strip().lower()
    label = str(label or "").strip().lower()
    with store.engine.begin() as db:
        done = db.execute(
            delete(blocked_label).where(blocked_label.c.account == account, blocked_label.c.label == label)
        ).rowcount
        if done:
            _audit_op(db, actor, "label_unblocked", {"account": account, "label": label})
    return bool(done)


# --- rate limiting (section 4.6) -----------------------------------------------------------------


def check_rate_limit(store: Store, account: str, label: str, *, limit: int = 5) -> None:
    """Enforce at most `limit` claim_next / claim_named calls per minute per (account, label)."""
    account = account.strip().lower()
    with store.engine.connect() as db:
        if db.dialect.name == "postgresql":
            rows = db.execute(
                text(
                    "SELECT detail FROM audit_event "
                    "WHERE actor = :actor "
                    "  AND action IN ('claim_next', 'claim_named') "
                    "  AND at >= CURRENT_TIMESTAMP - INTERVAL '60 seconds'"
                ),
                {"actor": account},
            ).all()
        else:
            cutoff = now() - timedelta(seconds=60)
            rows = db.execute(
                select(audit.c.detail).where(
                    audit.c.actor == account,
                    audit.c.action.in_(("claim_next", "claim_named")),
                    audit.c.at >= cutoff,
                )
            ).all()
    count = sum(1 for (detail,) in rows if isinstance(detail, dict) and detail.get("label") == label)
    if count >= limit:
        raise ClaimError(429, MSG_RATE_LIMIT.format(label=label), "claim_rate")


# --- atomic claim primitives (section 4.3) -------------------------------------------------------


def _cleanup_lapsed(engine: Engine, account: str, label: str) -> None:
    """Step 1: delete caller's own lapsed claim in its own autocommitted transaction."""
    with engine.begin() as db:
        if db.dialect.name == "postgresql":
            db.execute(
                text(
                    "DELETE FROM work_claim "
                    "WHERE account = :account AND label = :label AND expires_at <= CURRENT_TIMESTAMP"
                ),
                {"account": account, "label": label},
            )
        else:
            db.execute(
                delete(work_claim).where(
                    work_claim.c.account == account,
                    work_claim.c.label == label,
                    work_claim.c.expires_at <= now(),
                )
            )


def _try_claim_candidate(
    engine: Engine,
    work_id: str,
    account: str,
    label: str,
    task: str,
    minutes: int,
    *,
    allow_rotate: bool,
) -> dict | None:
    """Step 2: per-candidate atomic upsert with deadlock retry and lost-token rotation."""
    for attempt in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = digest(token)
        try:
            with engine.begin() as db:
                before = _held(db, work_id, lock=False)  # only to say, in the log, whose claim was taken
                if db.dialect.name == "postgresql":
                    res = db.execute(
                        text(
                            "INSERT INTO work_claim "
                            "  (work_id, account, label, token_hash, task, claimed_at, renewed_at, expires_at) "
                            "VALUES (:work_id, :account, :label, :token_hash, :task, "
                            "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                            "  CURRENT_TIMESTAMP + make_interval(mins => :minutes)) "
                            "ON CONFLICT (work_id) DO UPDATE "
                            "   SET account = :account, label = :label, token_hash = :token_hash, "
                            "       task = :task, claimed_at = CURRENT_TIMESTAMP, "
                            "       renewed_at = CURRENT_TIMESTAMP, "
                            "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                            " WHERE work_claim.expires_at <= CURRENT_TIMESTAMP"
                        ),
                        {
                            "work_id": work_id,
                            "account": account,
                            "label": label,
                            "token_hash": token_hash,
                            "task": task,
                            "minutes": minutes,
                        },
                    )
                else:
                    moment = now()
                    expires = moment + timedelta(minutes=minutes)
                    res = db.execute(
                        text(
                            "INSERT INTO work_claim "
                            "  (work_id, account, label, token_hash, task, claimed_at, renewed_at, expires_at) "
                            "VALUES (:work_id, :account, :label, :token_hash, :task, :now, :now, :expires) "
                            "ON CONFLICT (work_id) DO UPDATE "
                            "   SET account = :account, label = :label, token_hash = :token_hash, "
                            "       task = :task, claimed_at = :now, renewed_at = :now, expires_at = :expires "
                            " WHERE work_claim.expires_at <= :now"
                        ),
                        {
                            "work_id": work_id,
                            "account": account,
                            "label": label,
                            "token_hash": token_hash,
                            "task": task,
                            "now": moment,
                            "expires": expires,
                        },
                    )
                if res.rowcount == 1:
                    row = db.execute(
                        select(work_claim.c.expires_at, work_claim.c.task).where(
                            work_claim.c.work_id == work_id
                        )
                    ).first()
                    return {
                        "work_id": work_id,
                        "account": account,
                        "label": label,
                        "task": row.task,
                        "token": token,
                        "expires_at": _aware(row.expires_at),
                        "rotated": False,
                        "took_over": (
                            {"account": before.account, "label": before.label}
                            if before is not None and (before.account, before.label) != (account, label)
                            else None
                        ),
                    }

                # 0 rows changed: live claim exists on work_id.
                if allow_rotate:
                    live = _held(db, work_id)
                    if live and live.account == account and live.label == label:
                        expires_at = _extend(db, work_id, live.token_hash, minutes, new_hash=token_hash)
                        if expires_at is not None:
                            return {
                                "work_id": work_id,
                                "account": account,
                                "label": label,
                                "task": live.task,
                                "token": token,
                                "expires_at": expires_at,
                                "rotated": True,
                                "took_over": None,
                            }
                return None
        except IntegrityError as error:
            if _is_unique_violation(error):
                with engine.connect() as db:
                    held = db.execute(
                        select(work_claim.c.work_id).where(
                            work_claim.c.account == account, work_claim.c.label == label
                        )
                    ).scalar()
                raise ClaimError(
                    409,
                    MSG_ONE_CLAIM_PER_LABEL.format(label=label, work=held or "another work"),
                    "claim_label_busy",
                ) from error
            raise
        except OperationalError as error:
            if _is_deadlock(error) and attempt < 3:
                time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                continue
            raise
    return None


def _acquired(work_id: str, label: str, task: str, claimed: dict) -> dict:
    """What the log keeps of a claim: with whose lapsed claim it took over, if it took one."""
    detail = {"work_id": work_id, "label": label, "task": task}
    if claimed.get("took_over"):
        detail["took_over"] = claimed["took_over"]
    return detail


def claim_work(
    store: Store,
    work_id: str,
    account: str,
    label: str,
    task: str = "review",
    *,
    check_limit: bool = True,
) -> dict:
    """Claim one named work, or rotate/re-issue the token if the same (account, label) holds it."""
    account = account.strip().lower()
    label = validate_label(label)
    if task not in TASKS:
        raise ClaimError(422, f"task must be one of {sorted(TASKS)}")
    if is_label_blocked(store, account, label):
        store.audit(
            account,
            "claim_refused",
            detail={"work_id": work_id, "label": label, "reason": "blocked_label"},
        )
        raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=label), "label_blocked")
    if check_limit:
        check_rate_limit(store, account, label)
    store.audit(account, "claim_named", detail={"work_id": work_id, "label": label, "task": task})

    _cleanup_lapsed(store.engine, account, label)
    claimed = _try_claim_candidate(
        store.engine, work_id, account, label, task, claim_minutes(), allow_rotate=True
    )
    if claimed is None:
        with store.engine.connect() as db:
            holder = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
        if holder:
            store.audit(
                account,
                "claim_refused",
                detail={
                    "work_id": work_id,
                    "label": label,
                    "holder_account": holder.account,
                    "holder_label": holder.label,
                    "reason": "held_by_other",
                },
            )
            raise ClaimError(
                409, _held_message(holder.label, holder.account, holder.expires_at), "claim_held"
            )
        raise ClaimError(409, "Could not claim this work; try again.")

    action = "claim_rotated" if claimed["rotated"] else "claim_acquired"
    store.audit(account, action, detail=_acquired(work_id, label, task, claimed))
    return {
        "work_id": claimed["work_id"],
        "task": claimed["task"],
        "token": claimed["token"],
        "expires_at": claimed["expires_at"].isoformat(),
        "rotated": claimed["rotated"],
    }


def claim_next(
    store: Store,
    candidates: list[str],
    account: str,
    label: str,
    task: str = "review",
    *,
    own_lapsed: list[str] | None = None,
    jitter_window: int = 8,
    check_limit: bool = True,
    on_evict=None,
) -> dict:
    """Atomically claim the first available work from own_lapsed (in order) then candidates (with jitter)."""
    account = account.strip().lower()
    label = validate_label(label)
    if task not in TASKS:
        raise ClaimError(422, f"task must be one of {sorted(TASKS)}")
    if is_label_blocked(store, account, label):
        store.audit(account, "claim_refused", detail={"label": label, "reason": "blocked_label"})
        raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=label), "label_blocked")
    if check_limit:
        check_rate_limit(store, account, label)
    store.audit(account, "claim_next", detail={"label": label, "task": task})

    _cleanup_lapsed(store.engine, account, label)
    minutes = claim_minutes()

    # Priority 1: caller's own lapsed works, tried in order (no jitter).
    for work_id in own_lapsed or ():
        claimed = _try_claim_candidate(
            store.engine, work_id, account, label, task, minutes, allow_rotate=False
        )
        if on_evict is not None:
            on_evict(work_id)
        if claimed is not None:
            store.audit(account, "claim_acquired", detail=_acquired(work_id, label, task, claimed))
            return {
                "work_id": claimed["work_id"],
                "task": claimed["task"],
                "token": claimed["token"],
                "expires_at": claimed["expires_at"].isoformat(),
            }

    # Priority 2+: remaining candidates with random jitter within the top window.
    remaining = list(candidates)
    while remaining:
        idx = random.randrange(min(jitter_window, len(remaining))) if jitter_window > 1 else 0
        work_id = remaining.pop(idx)
        claimed = _try_claim_candidate(
            store.engine, work_id, account, label, task, minutes, allow_rotate=False
        )
        if on_evict is not None:
            on_evict(work_id)
        if claimed is not None:
            store.audit(account, "claim_acquired", detail=_acquired(work_id, label, task, claimed))
            return {
                "work_id": claimed["work_id"],
                "task": claimed["task"],
                "token": claimed["token"],
                "expires_at": claimed["expires_at"].isoformat(),
            }

    raise ClaimError(404, MSG_NOTHING_TO_CLAIM, "claim_none")


def renew_claim(store: Store, work_id: str, account: str, token: str) -> dict:
    """Extend a claim's lease (also revives a lapsed claim nobody else took); refuses blocked labels."""
    account = account.strip().lower()
    if not token:
        raise ClaimError(409, MSG_AGENT_NEEDS_CLAIM, "claim_required")
    token_hash = digest(token)
    with store.engine.begin() as db:
        held = _held(db, work_id)
        if held is None:
            raise ClaimError(404, "No claim found for this work.", "claim_lost")
        if held.account != account or not secrets.compare_digest(held.token_hash, token_hash):
            if held.live:
                raise ClaimError(409, _held_message(held.label, held.account, held.expires_at), "claim_held")
            raise ClaimError(409, MSG_CLAIM_GONE, "claim_lost")
        if is_label_blocked(db, account, held.label):
            raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=held.label), "label_blocked")
        expires_at = _extend(db, work_id, token_hash, claim_minutes())
        if expires_at is None:  # the row is locked, so this is not reached; never report a renewal not made
            raise ClaimError(409, MSG_CLAIM_GONE, "claim_lost")
    return {
        "work_id": work_id,
        "label": held.label,
        "task": held.task,
        "expires_at": expires_at.isoformat(),
    }


def touch(engine: Engine, work_id: str, account: str, token: str) -> None:
    """Renew the caller's claim because they are at work on it (a page or image read, a save): once a
    minute at most, never for a blocked label, and only while the claim still carries their token."""
    account = account.strip().lower()
    token_hash = digest(token)
    minutes = claim_minutes()
    with engine.begin() as db:
        if db.dialect.name == "postgresql":
            db.execute(
                text(
                    "UPDATE work_claim AS c "
                    "   SET renewed_at = CURRENT_TIMESTAMP, "
                    "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                    " WHERE c.work_id = :work_id AND c.account = :account AND c.token_hash = :token_hash "
                    "   AND (c.expires_at <= CURRENT_TIMESTAMP "
                    "        OR CURRENT_TIMESTAMP - c.renewed_at >= INTERVAL '60 seconds') "
                    "   AND NOT EXISTS (SELECT 1 FROM blocked_label b "
                    "                    WHERE b.account = c.account AND b.label = c.label)"
                ),
                {"minutes": minutes, "work_id": work_id, "account": account, "token_hash": token_hash},
            )
            return
        held = _held(db, work_id)
        if held is None or held.account != account:
            return
        if not secrets.compare_digest(held.token_hash, token_hash):
            return
        if (held.live and not held.due) or is_label_blocked(db, account, held.label):
            return
        _extend(db, work_id, token_hash, minutes)


def release_claim(
    store: Store,
    work_id: str,
    who: dict,
    *,
    outcome: str = "done",
    reason: str = "",
    token: str | None = None,
    label: str | None = None,
) -> dict:
    """Release a claim and append a work_release row (done | parked | abandoned)."""
    if outcome not in OUTCOMES:
        raise ClaimError(422, f"outcome must be one of {sorted(OUTCOMES)}")
    account = who["email"].strip().lower()
    roles = set(who["operator"]["roles"])
    is_privileged = bool(roles & {"approver", "admin"})
    reason = str(reason or "").strip()[:2000]

    with store.engine.begin() as db:
        row = _held(db, work_id)
        if row is None:
            raise ClaimError(404, "No live claim on this work.", "claim_lost")
        own = row.account == account
        by_token = bool(token) and own and secrets.compare_digest(row.token_hash, digest(token))
        by_label = bool(label) and own and row.label == str(label).strip().lower() and row.label != UNLABELLED
        # A person's own claim, taken by a save: theirs to release from any browser.
        by_person = own and row.label == UNLABELLED and not who["operator"].get("agent")
        if not (is_privileged or by_token or by_label or by_person):
            raise ClaimError(403, "Only the claim holder or an approver/admin may release this claim.")
        if not is_privileged and outcome != "abandoned" and is_label_blocked(db, row.account, row.label):
            # A paused label hands nothing in as done: it may only put the work back.
            raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=row.label), "label_blocked")
        gone = db.execute(
            delete(work_claim).where(
                work_claim.c.work_id == work_id, work_claim.c.token_hash == row.token_hash
            )
        ).rowcount
        if gone != 1:  # the row is locked, so this is not reached; never release a claim not seen
            raise ClaimError(409, MSG_CLAIM_GONE, "claim_lost")
        release_id = int(
            db.execute(
                insert(work_release).values(
                    work_id=work_id,
                    account=row.account,
                    label=row.label,
                    task=row.task,
                    outcome=outcome,
                    reason=reason,
                    released_at=db_now(db),
                )
            ).inserted_primary_key[0]
        )
        _audit_op(
            db,
            account,
            "claim_released",
            {
                "work_id": work_id,
                "account": row.account,
                "label": row.label,
                "task": row.task,
                "outcome": outcome,
                "reason": reason,
                "released_by": account,
                "release_id": release_id,
            },
        )
    return {
        "release_id": release_id,
        "work_id": work_id,
        "account": row.account,
        "label": row.label,
        "task": row.task,
        "outcome": outcome,
        "reason": reason,
    }


def list_claims(store: Store, *, include_expired: bool = False) -> list[dict]:
    """List live claims (or all claims if include_expired=True)."""
    with store.engine.connect() as db:
        query = select(work_claim).order_by(work_claim.c.claimed_at.desc())
        if not include_expired:
            cutoff = text("CURRENT_TIMESTAMP") if db.dialect.name == "postgresql" else now()
            query = query.where(work_claim.c.expires_at > cutoff)
        rows = db.execute(query).all()
    return [
        {
            "work_id": row.work_id,
            "account": row.account,
            "label": row.label,
            "task": row.task,
            "claimed_at": _aware(row.claimed_at).isoformat(),
            "renewed_at": _aware(row.renewed_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
        }
        for row in rows
    ]


# --- save guards (sections 4.7 & 5) --------------------------------------------------------------


def check_approved_guard(db: Connection, act_ids: list[str] | set[str], who: dict) -> None:
    """Refuse saves on approved documents (or shared Details of a work with an approved sibling)
    for agent accounts without may_change_approved, when ALVARY_REVIEW_APPROVED_GUARD=enforce."""
    if approved_guard_mode() != "enforce":
        return
    person = who["operator"]
    if not person.get("agent") or person.get("may_change_approved"):
        return
    for act_id in act_ids:
        if not act_id:
            continue
        decision = db.execute(
            select(approval.c.decision)
            .where(approval.c.act_id == act_id)
            .order_by(approval.c.revision.desc())
            .limit(1)
        ).scalar()
        if decision == "approved":
            raise ClaimError(403, MSG_APPROVED, "approved_guard")


def guard(
    db: Connection,
    document: str | dict,
    who: dict,
    token: str | None = None,
    write: bool = True,
    *,
    act_id: str | None = None,
    approved_act_ids: list[str] | set[str] | None = None,
) -> dict | None:
    """Run inside the save transaction (after payload validation).

    1. Enforces ALVARY_REVIEW_APPROVED_GUARD when approved_act_ids or act_id is supplied.
    2. Enforces ALVARY_REVIEW_CLAIMS (off / warn / enforce):
       - Matching token (live or lapsed-and-untaken): checks blocked_label, extends lease (throttled
         to once/min unless reviving a lapsed lease).
       - Held by another: approver/admin allowed (logged as saved_over_claim); others 409.
       - Free or lapsed without matching token: agents 409; humans on write take an implicit
         'unlabelled' claim inside `db` and receive claim_token + claim_expires_at.
    """
    work_id, doc_act_id = _extract_work_id(document)
    effective_act_id = act_id or doc_act_id

    if approved_act_ids is not None:
        check_approved_guard(db, approved_act_ids, who)
    elif effective_act_id:
        check_approved_guard(db, [effective_act_id], who)

    mode = claims_mode()
    if mode == "off":
        return None

    account = who["email"].strip().lower()
    person = who["operator"]
    is_agent = bool(person.get("agent"))
    is_privileged = bool(set(person.get("roles") or ()) & {"approver", "admin"})
    minutes = claim_minutes()
    token_hash = digest(token) if token else None

    held = _held(db, work_id)

    def refuse_or_warn(
        status_code: int,
        message: str,
        reason: str,
        code: str,
        *,
        label: str | None = None,
        holder: Held | None = None,
    ) -> dict:
        """Refuse, or under "warn" only log that it would have been refused. ``label`` is the caller's own
        label where they have one: a save let through under "warn" never carries the holder's."""
        if mode == "warn":
            _audit_op(
                db,
                account,
                "claim_would_refuse",
                {
                    "work_id": work_id,
                    "act_id": effective_act_id,
                    "account": account,
                    "label": label,
                    "holder_account": holder.account if holder else None,
                    "holder_label": holder.label if holder else None,
                    "reason": reason,
                },
            )
            return {
                "work_id": work_id,
                "account": account,
                "label": label,
                "task": None,
                "would_refuse": True,
                "reason": reason,
            }
        raise ClaimError(status_code, message, code)

    def over_claim(holder: Held) -> dict:
        """The owner's save on a work someone holds: let through and logged (design 4.7)."""
        _audit_op(
            db,
            account,
            "saved_over_claim",
            {
                "work_id": work_id,
                "act_id": effective_act_id,
                "holder_account": holder.account,
                "holder_label": holder.label,
            },
        )
        return {
            "work_id": work_id,
            "account": account,
            "label": None,
            "task": None,
            "saved_over_claim": True,
        }

    def implicit(new_token: str, expires_at: datetime) -> dict:
        return {
            "work_id": work_id,
            "account": account,
            "label": UNLABELLED,
            "task": "review",
            "claim_token": new_token,
            "claim_expires_at": expires_at.isoformat(),
            "implicit": True,
        }

    # 1. The caller's token is the claim's, live or lapsed and not taken: renewed (a live one once a
    #    minute at most) on the locked row, so a takeover cannot slip in between the look and the save.
    if (
        held is not None
        and token_hash is not None
        and held.account == account
        and secrets.compare_digest(held.token_hash, token_hash)
    ):
        if is_label_blocked(db, account, held.label):
            return refuse_or_warn(
                403,
                MSG_BLOCKED_LABEL.format(label=held.label),
                "blocked_label",
                "label_blocked",
                label=held.label,
            )
        expires_at = held.expires_at
        if not held.live or held.due:
            renewed = _extend(db, work_id, token_hash, minutes)
            if renewed is None:  # not reached while the row is locked; never save for a claim not held
                return refuse_or_warn(409, MSG_CLAIM_GONE, "claim_lost", "claim_lost", label=held.label)
            expires_at = renewed
        return {
            "work_id": work_id,
            "account": account,
            "label": held.label,
            "task": held.task,
            "expires_at": expires_at.isoformat(),
            "implicit": False,
        }

    # 2. A live claim, and not by this token.
    if held is not None and held.live:
        if held.label == UNLABELLED and held.account == account and not is_agent:
            # The person's own claim, taken by an earlier save in another browser, or before this one
            # had stored its token: theirs to carry on with, under a token this browser can keep.
            if not write:
                return {
                    "work_id": work_id,
                    "account": account,
                    "label": UNLABELLED,
                    "task": "review",
                    "implicit": False,
                }
            new_token = secrets.token_urlsafe(32)
            renewed = _extend(db, work_id, held.token_hash, minutes, new_hash=digest(new_token))
            if renewed is not None:
                return implicit(new_token, renewed)
        if is_privileged:
            return over_claim(held)
        return refuse_or_warn(
            409,
            _held_message(held.label, held.account, held.expires_at),
            "held_by_other",
            "claim_held",
            holder=held,
        )

    # 3. Free, or lapsed with no matching token.
    if is_agent:
        return refuse_or_warn(409, MSG_AGENT_NEEDS_CLAIM, "agent_no_claim", "claim_required")
    if not write:
        return None

    # A person's first save takes the claim by itself, inside the save's transaction.
    new_token = secrets.token_urlsafe(32)
    values = {"work_id": work_id, "account": account, "token_hash": digest(new_token)}
    if db.dialect.name == "postgresql":
        taken = db.execute(
            text(
                "INSERT INTO work_claim "
                "  (work_id, account, label, token_hash, task, claimed_at, renewed_at, expires_at) "
                "VALUES (:work_id, :account, 'unlabelled', :token_hash, 'review', "
                "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "  CURRENT_TIMESTAMP + make_interval(mins => :minutes)) "
                "ON CONFLICT (work_id) DO UPDATE "
                "   SET account = :account, label = 'unlabelled', token_hash = :token_hash, "
                "       task = 'review', claimed_at = CURRENT_TIMESTAMP, "
                "       renewed_at = CURRENT_TIMESTAMP, "
                "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                " WHERE work_claim.expires_at <= CURRENT_TIMESTAMP "
                "RETURNING expires_at"
            ),
            {**values, "minutes": minutes},
        ).scalar()
        new_expires = _aware(taken) if taken is not None else None
    else:
        moment = now()
        expires = moment + timedelta(minutes=minutes)
        changed = db.execute(
            text(
                "INSERT INTO work_claim "
                "  (work_id, account, label, token_hash, task, claimed_at, renewed_at, expires_at) "
                "VALUES (:work_id, :account, 'unlabelled', :token_hash, 'review', :now, :now, :expires) "
                "ON CONFLICT (work_id) DO UPDATE "
                "   SET account = :account, label = 'unlabelled', token_hash = :token_hash, "
                "       task = 'review', claimed_at = :now, renewed_at = :now, expires_at = :expires "
                " WHERE work_claim.expires_at <= :now"
            ),
            {**values, "now": moment, "expires": expires},
        ).rowcount
        new_expires = expires if changed == 1 else None
    if new_expires is None:
        # Someone took the work between this transaction's look and its write. No token is handed
        # out for a claim that was not made.
        holder = _held(db, work_id, lock=False)
        if holder is None:
            return refuse_or_warn(409, "Could not claim this work; try again.", "claim_race", "claim")
        if is_privileged:
            return over_claim(holder)
        return refuse_or_warn(
            409,
            _held_message(holder.label, holder.account, holder.expires_at),
            "held_by_other",
            "claim_held",
            holder=holder,
        )
    detail = {"work_id": work_id, "act_id": effective_act_id, "label": UNLABELLED}
    if held is not None and (held.account, held.label) != (account, UNLABELLED):
        detail["took_over"] = {"account": held.account, "label": held.label}
    _audit_op(db, account, "claim_implicit", detail)
    return implicit(new_token, new_expires)
