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
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

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


class ClaimError(HTTPException):
    """HTTP error raised by claim operations and save guards."""


def claims_mode() -> str:
    mode = os.environ.get("ALVARY_REVIEW_CLAIMS", "off").strip().lower()
    return mode if mode in {"off", "warn", "enforce"} else "off"


def approved_guard_mode() -> str:
    mode = os.environ.get("ALVARY_REVIEW_APPROVED_GUARD", "off").strip().lower()
    return mode if mode in {"off", "enforce"} else "off"


def claim_minutes() -> int:
    try:
        return max(1, int(os.environ.get("ALVARY_REVIEW_CLAIM_MINUTES", "60")))
    except ValueError:
        return 60


def validate_label(label: str) -> str:
    cleaned = str(label or "").strip()
    if cleaned == "unlabelled" or not LABEL_RE.fullmatch(cleaned):
        raise ClaimError(422, "Label must be 3 to 40 letters, digits, or hyphens.")
    return cleaned


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
        raise ClaimError(429, MSG_RATE_LIMIT.format(label=label))


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
                    }

                # 0 rows changed: live claim exists on work_id.
                if allow_rotate:
                    live = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
                    if live and live.account == account and live.label == label:
                        if db.dialect.name == "postgresql":
                            db.execute(
                                text(
                                    "UPDATE work_claim "
                                    "   SET token_hash = :token_hash, "
                                    "       renewed_at = CURRENT_TIMESTAMP, "
                                    "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                                    " WHERE work_id = :work_id AND account = :account AND label = :label"
                                ),
                                {
                                    "token_hash": token_hash,
                                    "minutes": minutes,
                                    "work_id": work_id,
                                    "account": account,
                                    "label": label,
                                },
                            )
                        else:
                            moment = now()
                            expires = moment + timedelta(minutes=minutes)
                            db.execute(
                                update(work_claim)
                                .where(
                                    work_claim.c.work_id == work_id,
                                    work_claim.c.account == account,
                                    work_claim.c.label == label,
                                )
                                .values(token_hash=token_hash, renewed_at=moment, expires_at=expires)
                            )
                        updated = db.execute(
                            select(work_claim.c.expires_at, work_claim.c.task).where(
                                work_claim.c.work_id == work_id
                            )
                        ).first()
                        return {
                            "work_id": work_id,
                            "account": account,
                            "label": label,
                            "task": updated.task,
                            "token": token,
                            "expires_at": _aware(updated.expires_at),
                            "rotated": True,
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
                ) from error
            raise
        except OperationalError as error:
            if _is_deadlock(error) and attempt < 3:
                time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                continue
            raise
    return None


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
        raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=label))
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
            raise ClaimError(409, _held_message(holder.label, holder.account, holder.expires_at))
        raise ClaimError(409, "Could not claim this work; try again.")

    action = "claim_rotated" if claimed["rotated"] else "claim_acquired"
    store.audit(account, action, detail={"work_id": work_id, "label": label, "task": task})
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
        raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=label))
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
            store.audit(account, "claim_acquired", detail={"work_id": work_id, "label": label, "task": task})
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
            store.audit(account, "claim_acquired", detail={"work_id": work_id, "label": label, "task": task})
            return {
                "work_id": claimed["work_id"],
                "task": claimed["task"],
                "token": claimed["token"],
                "expires_at": claimed["expires_at"].isoformat(),
            }

    raise ClaimError(404, MSG_NOTHING_TO_CLAIM)


def renew_claim(store: Store, work_id: str, account: str, token: str) -> dict:
    """Extend a claim's lease (also revives a lapsed claim nobody else took); refuses blocked labels."""
    account = account.strip().lower()
    if not token:
        raise ClaimError(409, MSG_AGENT_NEEDS_CLAIM)
    token_hash = digest(token)
    minutes = claim_minutes()
    with store.engine.begin() as db:
        row = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
        if row is None:
            raise ClaimError(404, "No claim found for this work.")
        if row.account != account or not secrets.compare_digest(row.token_hash, token_hash):
            if _aware(row.expires_at) > now():
                raise ClaimError(409, _held_message(row.label, row.account, row.expires_at))
            raise ClaimError(409, "Your claim token is no longer valid for this work.")
        if is_label_blocked(db, account, row.label):
            raise ClaimError(403, MSG_BLOCKED_LABEL.format(label=row.label))
        if db.dialect.name == "postgresql":
            db.execute(
                text(
                    "UPDATE work_claim "
                    "   SET renewed_at = CURRENT_TIMESTAMP, "
                    "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                    " WHERE work_id = :work_id AND token_hash = :token_hash"
                ),
                {"minutes": minutes, "work_id": work_id, "token_hash": token_hash},
            )
        else:
            moment = now()
            db.execute(
                update(work_claim)
                .where(work_claim.c.work_id == work_id, work_claim.c.token_hash == token_hash)
                .values(renewed_at=moment, expires_at=moment + timedelta(minutes=minutes))
            )
        updated = db.execute(select(work_claim.c.expires_at).where(work_claim.c.work_id == work_id)).scalar()
    return {
        "work_id": work_id,
        "label": row.label,
        "task": row.task,
        "expires_at": _aware(updated).isoformat(),
    }


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
        row = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
        if row is None:
            raise ClaimError(404, "No live claim on this work.")
        by_token = (
            bool(token) and row.account == account and secrets.compare_digest(row.token_hash, digest(token))
        )
        by_label = bool(label) and row.account == account and row.label == label and row.label != "unlabelled"
        if not (is_privileged or by_token or by_label):
            raise ClaimError(403, "Only the claim holder or an approver/admin may release this claim.")
        db.execute(delete(work_claim).where(work_claim.c.work_id == work_id))
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
            raise ClaimError(403, MSG_APPROVED)


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
    moment = now()
    minutes = claim_minutes()
    token_hash = digest(token) if token else None

    row = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()

    def refuse_or_warn(status_code: int, message: str, reason: str, label: str | None = None) -> dict:
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
        raise ClaimError(status_code, message)

    # 1. Caller holds the matching token (live or lapsed-and-untaken -> revived).
    if (
        row is not None
        and token_hash is not None
        and row.account == account
        and secrets.compare_digest(row.token_hash, token_hash)
    ):
        if is_label_blocked(db, account, row.label):
            return refuse_or_warn(403, MSG_BLOCKED_LABEL.format(label=row.label), "blocked_label", row.label)
        expires_at = _aware(row.expires_at)
        renewed_at = _aware(row.renewed_at)
        if expires_at <= moment or moment - renewed_at >= timedelta(seconds=60):
            if db.dialect.name == "postgresql":
                db.execute(
                    text(
                        "UPDATE work_claim "
                        "   SET renewed_at = CURRENT_TIMESTAMP, "
                        "       expires_at = CURRENT_TIMESTAMP + make_interval(mins => :minutes) "
                        " WHERE work_id = :work_id AND token_hash = :token_hash"
                    ),
                    {"minutes": minutes, "work_id": work_id, "token_hash": token_hash},
                )
                expires_at = _aware(
                    db.execute(
                        select(work_claim.c.expires_at).where(work_claim.c.work_id == work_id)
                    ).scalar()
                )
            else:
                expires_at = moment + timedelta(minutes=minutes)
                db.execute(
                    update(work_claim)
                    .where(work_claim.c.work_id == work_id, work_claim.c.token_hash == token_hash)
                    .values(renewed_at=moment, expires_at=expires_at)
                )
        return {
            "work_id": work_id,
            "account": account,
            "label": row.label,
            "task": row.task,
            "expires_at": expires_at.isoformat(),
            "implicit": False,
        }

    # 2. Live claim held by another token/holder.
    if row is not None and _aware(row.expires_at) > moment:
        if is_privileged:
            _audit_op(
                db,
                account,
                "saved_over_claim",
                {
                    "work_id": work_id,
                    "act_id": effective_act_id,
                    "holder_account": row.account,
                    "holder_label": row.label,
                },
            )
            return {
                "work_id": work_id,
                "account": account,
                "label": None,
                "task": None,
                "saved_over_claim": True,
            }
        return refuse_or_warn(
            409,
            _held_message(row.label, row.account, row.expires_at),
            "held_by_other",
            row.label,
        )

    # 3. Free or lapsed work, caller has no matching token.
    if is_agent:
        return refuse_or_warn(409, MSG_AGENT_NEEDS_CLAIM, "agent_no_claim")
    if not write:
        return None

    # Implicit claim for a human reviewer, inside the save transaction.
    new_token = secrets.token_urlsafe(32)
    new_hash = digest(new_token)
    if db.dialect.name == "postgresql":
        res = db.execute(
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
                " WHERE work_claim.expires_at <= CURRENT_TIMESTAMP"
            ),
            {"work_id": work_id, "account": account, "token_hash": new_hash, "minutes": minutes},
        )
        if res.rowcount == 0:
            holder = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
            if holder and not is_privileged:
                return refuse_or_warn(
                    409,
                    _held_message(holder.label, holder.account, holder.expires_at),
                    "held_by_other",
                    holder.label,
                )
        new_expires = _aware(
            db.execute(select(work_claim.c.expires_at).where(work_claim.c.work_id == work_id)).scalar()
        )
    else:
        new_expires = moment + timedelta(minutes=minutes)
        res = db.execute(
            text(
                "INSERT INTO work_claim "
                "  (work_id, account, label, token_hash, task, claimed_at, renewed_at, expires_at) "
                "VALUES (:work_id, :account, 'unlabelled', :token_hash, 'review', :now, :now, :expires) "
                "ON CONFLICT (work_id) DO UPDATE "
                "   SET account = :account, label = 'unlabelled', token_hash = :token_hash, "
                "       task = 'review', claimed_at = :now, renewed_at = :now, expires_at = :expires "
                " WHERE work_claim.expires_at <= :now"
            ),
            {
                "work_id": work_id,
                "account": account,
                "token_hash": new_hash,
                "now": moment,
                "expires": new_expires,
            },
        )
        if res.rowcount == 0:
            holder = db.execute(select(work_claim).where(work_claim.c.work_id == work_id)).first()
            if holder and not is_privileged:
                return refuse_or_warn(
                    409,
                    _held_message(holder.label, holder.account, holder.expires_at),
                    "held_by_other",
                    holder.label,
                )
    return {
        "work_id": work_id,
        "account": account,
        "label": "unlabelled",
        "task": "review",
        "claim_token": new_token,
        "claim_expires_at": new_expires.isoformat(),
        "implicit": True,
    }
