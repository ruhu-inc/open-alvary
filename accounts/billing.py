"""Stripe billing: Checkout, the Customer Portal, Team's annual invoice, webhooks and the daily reconciliation
(docs: stripe-billing).

Paid access lives in accounts.entitlement (source 'payment', reference the Stripe subscription) and only a
verified webhook, or the reconciliation ending what Stripe has ended, writes it; a browser coming back from
Checkout changes nothing. No database transaction is ever open across a call to Stripe: a route or a webhook
reads in one short transaction, calls Stripe outside any, then writes in one short transaction. What a webhook
decides to do in Stripe (cancel a duplicate, cancel a deleted organisation's subscription) is done after its
commit; if that call fails, the reconciliation repeats it.

Billing stays off until STRIPE_SECRET_KEY is set, which waits on the owner's pre-charge gates (the plans'
prices, updated terms and privacy notice). Every request and the webhook endpoint use one API version,
STRIPE_VERSION, whose payloads the parsers read (with the older top-level fields as a fallback).

  python -m accounts.billing reconcile   compare every subscription with Stripe, and tell the owners of an
                                         organisation whose seat grace has started (daily job, jobs login)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NoReturn
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from accounts import mail, policies, roles
from accounts.store import AccountError, Accounts, canonical_email

log = logging.getLogger("open_alvary.billing")

STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = "2025-03-31.basil"
WEBHOOK_TOLERANCE_SECONDS = 300

# The plans sold by subscription, and the price each is looked up by in Stripe (stripe-billing §3).
SELF_SERVE = ("developer", "team")
INTERVALS = {"monthly": "month", "annual": "year"}
LOOKUP_KEYS = {
    ("developer", "month"): "oa_pro_monthly",
    ("developer", "year"): "oa_pro_annual",
    ("team", "month"): "oa_team_monthly",
    ("team", "year"): "oa_team_annual",
}
TIER_BY_LOOKUP = {lookup: plan for plan, lookup in LOOKUP_KEYS.items()}
PRICE_ENV = {
    ("developer", "month"): "STRIPE_PRICE_PRO_MONTHLY",
    ("developer", "year"): "STRIPE_PRICE_PRO_ANNUAL",
    ("team", "month"): "STRIPE_PRICE_TEAM_MONTHLY",
    ("team", "year"): "STRIPE_PRICE_TEAM_ANNUAL",
}
# A subscription that counts against a new checkout (stripe-billing §5, Preconditions).
SUBSCRIBED = ("active", "past_due", "unpaid", "incomplete", "trialing")
# One that may still charge: cancelled before its organisation is deleted.
LIVE = (*SUBSCRIBED, "paused")
ENDED = ("canceled", "incomplete_expired")
# One whose paid access is over: what a subscription that can no longer be read is still ended on.
OVER = (*ENDED, "unpaid")
STATUSES = (*LIVE, *ENDED)
# What the person is told when Stripe refuses what they sent, by Stripe's code; never Stripe's own message.
REFUSED = "Our payment provider didn't accept this request. Check the details and try again, or contact us."
REFUSALS = {
    "customer_tax_location_invalid": "The billing address couldn't be used to work out tax. Check the country,"
    " postal code and state or province, then try again.",
    "email_invalid": "The billing email address wasn't accepted. Check it and try again.",
    "tax_id_invalid": "The tax ID wasn't accepted. Check it and try again.",
}
EVENTS = (
    "checkout.session.completed",
    "checkout.session.expired",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.voided",
    "invoice.marked_uncollectible",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "charge.refunded",
    "charge.dispute.created",
)
NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
POSTAL = re.compile(r"^[A-Za-z0-9 -]{2,20}$")
WEBHOOK_EVENTS_KEPT_DAYS = 90


# ---------- Stripe over HTTP ----------
@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str = ""
    portal_config_id: str | None = None
    # Price IDs set by environment, overriding the lookup keys: (tier, interval) -> price ID.
    price_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> StripeConfig | None:
        secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        if not secret:
            return None
        prices = {plan: os.environ[name].strip() for plan, name in PRICE_ENV.items() if os.getenv(name)}
        return cls(
            secret_key=secret,
            webhook_secret=(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip(),
            portal_config_id=(os.getenv("STRIPE_PORTAL_CONFIG_ID") or "").strip() or None,
            price_ids=prices,
        )


class StripeError(Exception):
    """Stripe answered with a refusal (``status`` 4xx, with Stripe's ``code``), or could not be reached
    (``status`` None, or 5xx)."""

    def __init__(self, status: int | None, code: str | None, message: str):
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def unreachable(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


def _form(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested objects and lists in brackets."""
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        if value is None:
            continue
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            pairs.extend(_form(value, name))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(_form(item, f"{name}[{index}]"))
                else:
                    pairs.append((f"{name}[{index}]", str(item)))
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        else:
            pairs.append((name, str(value)))
    return pairs


class StripeClient:
    """The few Stripe REST calls billing makes, each with the pinned API version."""

    def __init__(self, config: StripeConfig, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=20)

    def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: list[tuple[str, str]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.secret_key}",
            "Stripe-Version": STRIPE_VERSION,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        content = None
        if data is not None:
            content = urlencode(_form(data)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self.http.request(
                method, STRIPE_API + path, content=content, params=params, headers=headers
            )
        except httpx.HTTPError as error:
            raise StripeError(None, None, "Stripe could not be reached") from error
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            problem = payload.get("error") or {}
            raise StripeError(response.status_code, problem.get("code"), problem.get("message") or "refused")
        return payload

    def get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, data: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        return self.request("POST", path, data=data or {}, idempotency_key=key)

    def subscriptions_of(self, customer: str) -> list[dict[str, Any]]:
        found, after = [], None
        while True:
            params = [("customer", customer), ("status", "all"), ("limit", "100")]
            page = self.get("/subscriptions", params + ([("starting_after", after)] if after else []))
            found += page.get("data") or []
            if not page.get("has_more") or not found:
                return found
            after = found[-1]["id"]

    def price(self, tier: str, interval: str) -> dict[str, Any]:
        """The price a plan is sold at: by the environment's price ID, else by its lookup key."""
        configured = self.config.price_ids.get((tier, interval))
        if configured:
            return self.get(f"/prices/{configured}")
        found = self.get("/prices", [("lookup_keys[]", LOOKUP_KEYS[(tier, interval)]), ("active", "true")])
        prices = found.get("data") or []
        if not prices:
            raise StripeError(404, "price_missing", "No active price for " + LOOKUP_KEYS[(tier, interval)])
        return prices[0]

    def plan_of(self, price: dict[str, Any] | str | None) -> tuple[str, str] | None:
        """The (tier, interval) a price sells, strictly: its lookup key, or a price ID set by environment.
        Never a guess from metadata (stripe-billing §3)."""
        if isinstance(price, dict):
            plan = TIER_BY_LOOKUP.get(price.get("lookup_key") or "")
            if plan:
                return plan
            price = price.get("id")
        for plan, configured in self.config.price_ids.items():
            if configured and configured == price:
                return plan
        return None


def verify_signature(raw: bytes, header: str, secret: str, *, now: int | None = None) -> None:
    """Stripe-Signature (t=<unix>,v1=<hex>[,v1=...]) over the raw body, within five minutes."""
    if not secret:
        raise AccountError("billing_unavailable", 503, "Billing isn't set up yet.")
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        name, _, value = part.strip().partition("=")
        if name == "t" and value.isdigit():
            timestamp = int(value)
        elif name == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise AccountError("invalid_signature", 400, "Missing or malformed Stripe-Signature.")
    if abs((int(time.time()) if now is None else now) - timestamp) > WEBHOOK_TOLERANCE_SECONDS:
        raise AccountError("invalid_signature", 400, "Stripe-Signature is too old.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, given) for given in signatures):
        raise AccountError("invalid_signature", 400, "Stripe-Signature does not match.")


# ---------- reading Stripe's objects ----------
def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("id")
    return value if isinstance(value, str) and value else None


def _moment(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if isinstance(value, int | float) and value else None


def invoice_subscription(invoice: dict[str, Any]) -> str | None:
    """2025-03-31.basil moved it under parent.subscription_details; older payloads had it at the top."""
    details = ((invoice.get("parent") or {}).get("subscription_details")) or {}
    return _id(details.get("subscription")) or _id(invoice.get("subscription"))


def _first_item(subscription: dict[str, Any]) -> dict[str, Any]:
    items = ((subscription.get("items") or {}).get("data")) or []
    return items[0] if items else {}


def period_end(subscription: dict[str, Any]) -> datetime | None:
    """basil keeps it on each item; older payloads on the subscription."""
    item = _first_item(subscription)
    return _moment(item.get("current_period_end") or subscription.get("current_period_end"))


def _price(subscription: dict[str, Any]) -> dict[str, Any] | None:
    item = _first_item(subscription)
    return item.get("price") or (subscription.get("plan") and {"id": _id(subscription["plan"])})


def _countries(metadata: dict[str, Any]) -> list[str]:
    raw = (metadata or {}).get("jurisdictions") or ""
    return [c for c in (part.strip().lower() for part in raw.split(",")) if c]


@dataclass
class Plan:
    """A subscription as Open Alvary records it."""

    id: str
    organisation_id: str | None
    customer: str | None
    tier: str
    interval: str
    collection_method: str
    status: str
    scope: dict
    period_end: datetime | None
    cancel_at_period_end: bool
    metadata: dict


@dataclass
class Outcome:
    """What a webhook did, and what is left to do after its commit."""

    name: str
    cancel: list[str] = field(default_factory=list)
    alert: str | None = None


# ---------- the service ----------
class Billing:
    def __init__(
        self,
        accounts: Accounts,
        client: StripeClient | None,
        *,
        alert: Callable[[str, dict], None] | None = None,
    ):
        self.accounts = accounts
        self.client = client
        self._alert = alert

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _t(self, conn, name: str) -> str:
        return self.accounts._t(conn, name)

    def _stripe(self) -> StripeClient:
        if self.client is None:
            raise AccountError(
                "billing_unavailable", 503, "Paying by card or invoice isn't available yet. Contact us."
            )
        return self.client

    def alert(self, what: str, details: dict) -> None:
        """Tell the operator (mail, when the console has a mailer); always logged. Never fails the caller."""
        log.warning("billing alert: %s %s", what, json.dumps(details, default=str, sort_keys=True))
        if self._alert is not None:
            try:
                self._alert(what, details)
            except Exception:  # noqa: BLE001 - an alert must never undo what was committed
                log.exception("billing alert could not be sent")

    # ---------- what the console shows ----------
    def status(self, person_id: str, organisation_id: str) -> dict:
        with self.accounts.engine.connect() as conn:
            membership = self.accounts._membership(conn, person_id, organisation_id)
            managers = membership["role"] in roles.MANAGERS
            customer = self._customer(conn, organisation_id)
            current = self.accounts.current_entitlement(conn, organisation_id)
            subscription = self._shown_subscription(conn, organisation_id, current)
        shown = None
        if subscription:
            shown = {
                "tier_id": subscription["tier_id"],
                "interval": subscription["billing_interval"],
                "collection_method": subscription["collection_method"],
                "status": subscription["status"],
                "invoice_paid": subscription["invoice_paid"],
                "invoice_url": subscription["latest_invoice_url"] if managers else None,
                "current_period_end": subscription["current_period_end"],
                "cancel_at_period_end": subscription["cancel_at_period_end"],
                "past_due_since": subscription["past_due_since"],
            }
        return {
            "enabled": self.enabled,
            "has_customer": customer is not None,
            "billing_email": customer["billing_email"] if customer and managers else None,
            "source": current["source"] if current else None,
            "subscription": shown,
        }

    def prices(self, person_id: str, organisation_id: str) -> dict:
        """The prices the dialogs show and the owner agrees to: Stripe's, for each plan and interval."""
        client = self._stripe()
        with self.accounts.engine.connect() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
        shown: dict[str, dict] = {}
        for tier, interval in LOOKUP_KEYS:
            price = self._call(lambda t=tier, i=interval: client.price(t, i))
            shown.setdefault(tier, {})[interval] = {
                "amount": price.get("unit_amount"),
                "currency": price.get("currency"),
            }
        return {"prices": shown}

    def _customer(self, conn, organisation_id: str, *, lock: bool = False) -> dict | None:
        row = conn.execute(
            text(
                "SELECT stripe_customer_id, billing_email, open_checkout_session_id, open_checkout_key"
                f" FROM {self._t(conn, 'billing_customer')} WHERE organisation_id = :o"
                + (" FOR UPDATE" if lock else "")
            ),
            {"o": organisation_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    def _shown_subscription(self, conn, organisation_id: str, current: dict | None) -> dict | None:
        """The one behind paid access, else the newest still running (a pending invoice), else none."""
        rows = (
            conn.execute(
                text(
                    f"SELECT * FROM {self._t(conn, 'billing_subscription')} WHERE organisation_id = :o"
                    " ORDER BY updated_at DESC"
                ),
                {"o": organisation_id},
            )
            .mappings()
            .all()
        )
        if current and current["source"] == "payment":
            for row in rows:
                if row["stripe_subscription_id"] == current["reference"]:
                    return dict(row)
        for row in rows:
            if row["status"] in LIVE:
                return dict(row)
        return None

    # ---------- starting to pay ----------
    def _owner(self, conn, person_id: str, organisation_id: str) -> dict:
        membership = self.accounts._membership(conn, person_id, organisation_id, lock=False)
        if membership["role"] != "owner":
            raise AccountError("role_forbidden", 403, "Only an owner can change the plan or billing.")
        if membership["organisation_state"] != "active":
            raise AccountError("organisation_suspended", 403, "This organisation is suspended.")
        return membership

    def _refuse_if_granted_or_subscribed(self, conn, organisation_id: str, *, recorded: bool = True) -> None:
        """The preconditions read from the database (stripe-billing §5): an operator's grant in place, or a
        subscription already recorded (unless ``recorded`` is False: the invoice asks Stripe instead)."""
        current = self.accounts.current_entitlement(conn, organisation_id)
        if current and current["source"] in ("manual", "waiver"):
            raise AccountError(
                "has_grant",
                409,
                "Your current plan was arranged with Open Alvary. To pay for a plan instead, first choose"
                " Go back to Starter, or contact us to change it.",
            )
        if current and current["source"] == "payment":
            raise AccountError(
                "already_subscribed", 409, "You already have a subscription. Change it under Manage billing."
            )
        live = recorded and conn.scalar(
            text(
                f"SELECT count(*) FROM {self._t(conn, 'billing_subscription')} WHERE organisation_id = :o"
                " AND status = ANY(:live)"
            ),
            {"o": organisation_id, "live": list(SUBSCRIBED)},
        )
        if live:
            raise AccountError(
                "already_subscribed",
                409,
                "This workspace already has a subscription or an unpaid invoice. Change it under Manage"
                " billing, or cancel the unpaid invoice first.",
            )

    def _refuse_if_subscribed_in_stripe(
        self, client: StripeClient, customer: str | None, key: str
    ) -> dict | None:
        """Asked of Stripe too, outside any transaction: a subscription may exist before its webhook. One
        this same request made (its metadata carries the key) is given back, for the invoice's repeat."""
        if customer is None:
            return None
        mine = None
        for subscription in self._call(lambda: client.subscriptions_of(customer)):
            if subscription.get("status") not in SUBSCRIBED:
                continue
            if (subscription.get("metadata") or {}).get("request_key") == key:
                mine = subscription
                continue
            raise AccountError(
                "already_subscribed",
                409,
                "This workspace already has a subscription or an unpaid invoice. Change it under Manage"
                " billing, or cancel the unpaid invoice first.",
            )
        return mine

    def _call(self, call):
        """A call to Stripe for a person waiting on it. One Stripe could not answer, or refused for a reason
        that is ours to fix (the key, a price that is not set up), is an outage to them: try again later.
        One Stripe refused for what they sent (400, 402: an address tax cannot be worked out from, an email
        address) says so, so that they correct it rather than wait."""
        try:
            return call()
        except StripeError as error:
            log.warning("stripe call failed: %s %s", error.status, error.code)
            if error.status not in (400, 402):
                self._down()
            raise AccountError("billing_refused", 400, REFUSALS.get(error.code or "", REFUSED)) from None

    def _down(self) -> NoReturn:
        raise AccountError(
            "billing_unavailable", 503, "We couldn't reach our payment provider. Try again in a few minutes."
        )

    def _request_key(self, organisation_id: str, route: str, sent: dict, nonce: str) -> str:
        body = json.dumps(sent, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256("\0".join((organisation_id, route, body, nonce)).encode()).hexdigest()

    def _team_name(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        name = " ".join(str(value).split())
        if not 1 <= len(name) <= 120:
            raise AccountError("invalid_input", 400, "Enter a team name of 1 to 120 characters.")
        return name

    def _common(self, data: dict) -> tuple[str, str]:
        if data.get("terms_agreed") is not True:
            raise AccountError("terms_required", 400, "Agree to the price and the terms to continue.")
        nonce = data.get("dialog_nonce")
        if not isinstance(nonce, str) or not NONCE.match(nonce):
            raise AccountError("invalid_input", 400, "Reload the page and try again.")
        return nonce, policies.CURRENT["terms"]

    def _ensure_customer(
        self, client: StripeClient, organisation_id: str, existing: dict | None, details: dict
    ) -> str:
        """The organisation's Stripe customer, made once (an idempotency key per organisation) and recorded."""
        if existing:
            if details:
                self._call(lambda: client.post(f"/customers/{existing['stripe_customer_id']}", details))
            return existing["stripe_customer_id"]
        key = hashlib.sha256(f"customer\0{organisation_id}".encode()).hexdigest()
        made = self._call(
            lambda: client.post(
                "/customers", {**details, "metadata": {"organisation_id": organisation_id}}, key
            )
        )
        with self.accounts.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'billing_customer')} (organisation_id, stripe_customer_id)"
                    " VALUES (:o, :c) ON CONFLICT (organisation_id) DO NOTHING"
                ),
                {"o": organisation_id, "c": made["id"]},
            )
            return self._customer(conn, organisation_id)["stripe_customer_id"]

    def _agree(self, conn, person_id, organisation_id, tier, interval, price, terms, context) -> None:
        """The price the owner agreed to (stripe-billing §5, Price agreement): the terms' version in
        policy_acceptance, and the plan and price in the organisation's audit log."""
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'policy_acceptance')} (person_id, document, version)"
                " VALUES (:p, 'terms', :v) ON CONFLICT DO NOTHING"
            ),
            {"p": person_id, "v": terms},
        )
        self.accounts._audit(
            conn,
            self.accounts._actor(conn, person_id),
            "billing.price_agreed",
            organisation=organisation_id,
            summary={
                "tier_id": tier,
                "interval": interval,
                "amount": price.get("unit_amount"),
                "currency": price.get("currency"),
                "terms_version": terms,
            },
            context=context,
        )

    def start_checkout(self, person_id: str, organisation_id: str, data: dict, *, site: str, context=None):
        """A Checkout Session for Pro or Team, by card. Access starts when Stripe's webhook says it is paid."""
        client = self._stripe()
        tier, interval = data.get("tier"), INTERVALS.get(data.get("interval"))
        if tier not in SELF_SERVE or interval is None:
            raise AccountError("invalid_input", 400, "Choose Pro or Team, monthly or annual.")
        nonce, terms = self._common(data)
        team_name = self._team_name(data.get("team_name")) if tier == "team" else None
        with self.accounts.engine.connect() as conn:
            self._owner(conn, person_id, organisation_id)
            scope = self.accounts._plan_scope(conn, tier, {"jurisdictions": data.get("jurisdictions")})
            self._refuse_if_granted_or_subscribed(conn, organisation_id)
            customer = self._customer(conn, organisation_id)
            email = self.accounts.person(person_id)["email"] if customer is None else None
        sent = {"tier": tier, "interval": interval, "scope": scope, "team_name": team_name, "terms": terms}
        key = self._request_key(organisation_id, "checkout", sent, nonce)
        if customer and customer["open_checkout_key"] == key:
            # The same request again (a double click, a retry): the same session while it is open.
            session = self._call(
                lambda: client.get(f"/checkout/sessions/{customer['open_checkout_session_id']}")
            )
            if session.get("status") == "open" and session.get("url"):
                return {"url": session["url"]}
            raise AccountError(
                "checkout_closed", 409, "That checkout has closed. Start again to see the price and pay."
            )
        self._refuse_if_subscribed_in_stripe(client, customer and customer["stripe_customer_id"], key)
        price = self._call(lambda: client.price(tier, interval))
        customer_id = self._ensure_customer(
            client, organisation_id, customer, {} if customer else {"email": email}
        )
        if customer and customer["open_checkout_session_id"]:
            try:
                client.post(f"/checkout/sessions/{customer['open_checkout_session_id']}/expire")
            except StripeError as error:
                if error.unreachable:
                    self._down()
                # Already completed or expired: nothing to expire.
        metadata = {
            "organisation_id": organisation_id,
            "tier_id": tier,
            "jurisdictions": ",".join(scope.get("jurisdictions") or []) or None,
            "team_name": team_name,
        }
        back = f"{site}/console/access?organisation={organisation_id}&checkout="
        session = self._call(
            lambda: client.post(
                "/checkout/sessions",
                {
                    "mode": "subscription",
                    "customer": customer_id,
                    "client_reference_id": organisation_id,
                    "line_items": [{"price": price["id"], "quantity": 1}],
                    "success_url": back + "success",
                    "cancel_url": back + "cancelled",
                    "automatic_tax": {"enabled": True},
                    "tax_id_collection": {"enabled": True},
                    "billing_address_collection": "required",
                    "customer_update": {"address": "auto", "name": "auto"},
                    "metadata": metadata,
                    "subscription_data": {"metadata": metadata},
                },
                key,
            )
        )
        with self.accounts.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id, lock=True)
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_customer')} SET open_checkout_session_id = :s,"
                    " open_checkout_key = :k, updated_at = statement_timestamp() WHERE organisation_id = :o"
                ),
                {"s": session["id"], "k": key, "o": organisation_id},
            )
            self._agree(conn, person_id, organisation_id, tier, interval, price, terms, context)
        return {"url": session["url"]}

    def open_portal(self, person_id: str, organisation_id: str, *, site: str) -> dict:
        client = self._stripe()
        with self.accounts.engine.connect() as conn:
            self._owner(conn, person_id, organisation_id)
            customer = self._customer(conn, organisation_id)
        if customer is None:
            raise AccountError("no_subscription", 409, "This workspace has no billing account yet.")
        request = {
            "customer": customer["stripe_customer_id"],
            "return_url": f"{site}/console/access?organisation={organisation_id}",
        }
        if client.config.portal_config_id:
            request["configuration"] = client.config.portal_config_id
        session = self._call(lambda: client.post("/billing_portal/sessions", request))
        return {"url": session["url"]}

    def request_invoice(self, person_id: str, organisation_id: str, data: dict, *, context=None) -> dict:
        """Team, annually, by bank transfer: a Net-30 invoice. The workspace stays on Starter until it is paid."""
        client = self._stripe()
        nonce, terms = self._common(data)
        legal_name = " ".join(str(data.get("legal_name") or "").split())
        if not 1 <= len(legal_name) <= 200:
            raise AccountError("invalid_input", 400, "Enter the organisation's legal name.")
        email = canonical_email(str(data.get("billing_email") or ""))
        address = self._address(data.get("billing_address"))
        team_name = self._team_name(data.get("team_name"))
        with self.accounts.engine.connect() as conn:
            self._owner(conn, person_id, organisation_id)
            scope = self.accounts._plan_scope(conn, "team", {"jurisdictions": data.get("jurisdictions")})
            self._refuse_if_granted_or_subscribed(conn, organisation_id, recorded=False)
            customer = self._customer(conn, organisation_id)
            recorded = conn.scalar(
                text(
                    f"SELECT count(*) FROM {self._t(conn, 'billing_subscription')} WHERE organisation_id = :o"
                    " AND status = ANY(:live)"
                ),
                {"o": organisation_id, "live": list(SUBSCRIBED)},
            )
        sent = {
            "scope": scope,
            "legal_name": legal_name,
            "email": email,
            "address": address,
            "team_name": team_name,
            "terms": terms,
        }
        key = self._request_key(organisation_id, "invoice", sent, nonce)
        mine = self._refuse_if_subscribed_in_stripe(client, customer and customer["stripe_customer_id"], key)
        if mine is not None:
            with self.accounts.engine.connect() as conn:
                finished = self._local(conn, mine["id"]) is not None
            if finished:
                # The same request again: the invoice it sent, sent nothing new and agreed to nothing new.
                invoice = self._call(lambda: client.get(f"/invoices/{_id(mine.get('latest_invoice'))}"))
                return {"invoice_url": invoice.get("hosted_invoice_url"), "status": invoice.get("status")}
        elif recorded:
            raise AccountError(
                "already_subscribed",
                409,
                "This workspace already has a subscription or an unpaid invoice. Change it under Manage"
                " billing, or cancel the unpaid invoice first.",
            )
        price = self._call(lambda: client.price("team", "year"))
        if mine is not None:
            # The same request again after an attempt that stopped part-way (Stripe made the subscription,
            # then a later step failed): carried on from where it stopped. Each step keeps the first
            # attempt's idempotency key, and the record, the agreement and the audit entry are written once.
            subscription, customer_id = mine, _id(mine.get("customer"))
            metadata = mine.get("metadata") or {}
        else:
            customer_id = self._ensure_customer(
                client, organisation_id, customer, {"name": legal_name, "email": email, "address": address}
            )
            metadata = {
                "organisation_id": organisation_id,
                "tier_id": "team",
                "jurisdictions": ",".join(scope.get("jurisdictions") or []) or None,
                "team_name": team_name,
                "request_key": key,
            }
            subscription = self._call(
                lambda: client.post(
                    "/subscriptions",
                    {
                        "customer": customer_id,
                        "items": [{"price": price["id"], "quantity": 1}],
                        "collection_method": "send_invoice",
                        "days_until_due": 30,
                        "automatic_tax": {"enabled": True},
                        "metadata": metadata,
                        "expand": ["latest_invoice"],
                    },
                    key + ":subscription",
                )
            )
        invoice = subscription.get("latest_invoice") or {}
        if isinstance(invoice, str):
            invoice = self._call(lambda: client.get(f"/invoices/{invoice}"))
        if invoice.get("status") == "draft":
            invoice = self._call(
                lambda: client.post(f"/invoices/{invoice['id']}/finalize", {}, key + ":finalize")
            )
        if invoice.get("status") == "open":
            invoice = self._call(lambda: client.post(f"/invoices/{invoice['id']}/send", {}, key + ":send"))
        plan = Plan(
            subscription["id"],
            organisation_id,
            customer_id,
            "team",
            "year",
            "send_invoice",
            subscription.get("status") or "incomplete",
            scope,
            period_end(subscription),
            bool(subscription.get("cancel_at_period_end")),
            metadata,
        )
        with self.accounts.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id, lock=True)
            if self._local(conn, plan.id, lock=True) is not None:
                # Another run of this same request finished first: nothing is written twice.
                return {"invoice_url": invoice.get("hosted_invoice_url"), "status": invoice.get("status")}
            self._record(conn, plan)
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_subscription')} SET latest_invoice_id = :i,"
                    " latest_invoice_url = :u WHERE stripe_subscription_id = :s AND NOT invoice_paid"
                ),
                {"i": invoice.get("id"), "u": invoice.get("hosted_invoice_url"), "s": plan.id},
            )
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_customer')} SET billing_email = :e,"
                    " updated_at = statement_timestamp() WHERE organisation_id = :o"
                ),
                {"e": email, "o": organisation_id},
            )
            self._agree(conn, person_id, organisation_id, "team", "year", price, terms, context)
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "billing.invoice_sent",
                organisation=organisation_id,
                target=("invoice", invoice.get("id") or plan.id),
                context=context,
            )
        return {"invoice_url": invoice.get("hosted_invoice_url"), "status": invoice.get("status")}

    def _address(self, value: Any) -> dict:
        if not isinstance(value, dict):
            raise AccountError("invalid_input", 400, "Enter the billing address.")
        address = {
            k: " ".join(str(value.get(k) or "").split())
            for k in ("line1", "line2", "city", "state", "postal_code", "country")
        }
        address["country"] = address["country"].upper()
        if (
            not 1 <= len(address["line1"]) <= 200
            or len(address["line2"]) > 200
            or not 1 <= len(address["city"]) <= 100
            or len(address["state"]) > 100
            or not re.fullmatch(r"[A-Z]{2}", address["country"])
            or (address["postal_code"] and not POSTAL.match(address["postal_code"]))
        ):
            raise AccountError(
                "invalid_input", 400, "Enter the street, city and country of the billing address."
            )
        return {k: v for k, v in address.items() if v}

    def cancel_invoice(self, person_id: str, organisation_id: str, *, context=None) -> None:
        """Withdraw an annual invoice not yet paid: void it first, then cancel its subscription."""
        client = self._stripe()
        with self.accounts.engine.connect() as conn:
            self._owner(conn, person_id, organisation_id)
            row = (
                conn.execute(
                    text(
                        f"SELECT stripe_subscription_id, latest_invoice_id FROM {self._t(conn, 'billing_subscription')}"
                        " WHERE organisation_id = :o AND collection_method = 'send_invoice' AND NOT invoice_paid"
                        " AND status <> ALL(:ended) ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"o": organisation_id, "ended": list(ENDED)},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AccountError("no_pending_invoice", 409, "There is no unpaid invoice to cancel.")
        if row["latest_invoice_id"]:
            try:
                client.post(f"/invoices/{row['latest_invoice_id']}/void")
            except StripeError as error:
                if error.unreachable:
                    self._down()
                invoice = self._call(lambda: client.get(f"/invoices/{row['latest_invoice_id']}"))
                if invoice.get("status") == "paid":
                    raise AccountError(
                        "invoice_paid", 409, "This invoice has been paid, so it can't be cancelled."
                    ) from None
        try:
            client.request("DELETE", f"/subscriptions/{row['stripe_subscription_id']}")
        except StripeError as error:
            if error.unreachable or error.code != "resource_missing":
                self._down()
        with self.accounts.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id, lock=True)
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_subscription')} SET status = 'canceled',"
                    " latest_invoice_url = NULL, updated_at = statement_timestamp() WHERE stripe_subscription_id = :s"
                ),
                {"s": row["stripe_subscription_id"]},
            )
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "billing.invoice_cancelled",
                organisation=organisation_id,
                target=("invoice", row["latest_invoice_id"] or row["stripe_subscription_id"]),
                context=context,
            )

    # ---------- deleting an organisation ----------
    def cancel_for_deletion(self, organisation_ids: list[str]) -> None:
        """Before an organisation's deletion transaction: cancel in Stripe every subscription that may still
        charge, voiding its open invoices first. Refused (billing_unavailable) if Stripe cannot be reached,
        so that a deleted organisation is never left paying."""
        for organisation_id in organisation_ids:
            with self.accounts.engine.connect() as conn:
                customer = self._customer(conn, organisation_id)
                local = conn.scalars(
                    text(
                        f"SELECT stripe_subscription_id FROM {self._t(conn, 'billing_subscription')}"
                        " WHERE organisation_id = :o AND status = ANY(:live)"
                    ),
                    {"o": organisation_id, "live": list(LIVE)},
                ).all()
            if customer is None and not local:
                continue
            if self.client is None and not local:
                continue  # nothing recorded may charge, and Stripe is not set up to ask
            client = self._stripe()
            found = set(local)
            if customer:
                found |= {
                    s["id"]
                    for s in self._call(
                        lambda c=client, k=customer: c.subscriptions_of(k["stripe_customer_id"])
                    )
                    if s.get("status") in LIVE
                }
                if customer["open_checkout_session_id"]:
                    try:
                        client.post(f"/checkout/sessions/{customer['open_checkout_session_id']}/expire")
                    except StripeError as error:
                        if error.unreachable:
                            self._down()
            for subscription in sorted(found):
                self._call(lambda c=client, s=subscription: self._cancel_in_stripe(c, s))
            with self.accounts.engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'billing_subscription')} SET status = 'canceled',"
                        " updated_at = statement_timestamp() WHERE stripe_subscription_id = ANY(:s)"
                    ),
                    {"s": sorted(found)},
                )
                if found:
                    self.accounts._audit(
                        conn,
                        ("system", "stripe:deletion"),
                        "billing.cancelled",
                        organisation=organisation_id,
                        summary={"subscriptions": len(found), "reason": "organisation_deleted"},
                    )

    def _cancel_in_stripe(self, client: StripeClient, subscription: str) -> None:
        """Void the subscription's open invoices, then cancel it. Already gone counts as done."""
        invoices = client.get(
            "/invoices", [("subscription", subscription), ("status", "open"), ("limit", "100")]
        )
        for invoice in invoices.get("data") or []:
            client.post(f"/invoices/{invoice['id']}/void")
        try:
            client.request("DELETE", f"/subscriptions/{subscription}")
        except StripeError as error:
            if error.unreachable or error.code != "resource_missing":
                raise

    # ---------- webhooks ----------
    def handle_webhook(self, raw: bytes, signature: str) -> tuple[int, dict]:
        """Stripe's webhook, in the strict order of stripe-billing §6.1: verify, read, fetch outside any
        transaction, then one transaction. Answers (status, body): 500 asks Stripe to send it again."""
        client = self._stripe()
        verify_signature(raw, signature, client.config.webhook_secret)
        try:
            event = json.loads(raw)
            event_id, kind = event["id"], event["type"]
            obj = event["data"]["object"]
            created = _moment(event.get("created")) or datetime.now(UTC)
        except (ValueError, KeyError, TypeError):
            raise AccountError("invalid_input", 400, "Not a Stripe event.") from None
        if not re.fullmatch(r"evt_[A-Za-z0-9_]{1,250}", str(event_id)) or not re.fullmatch(
            r"[a-z_.]{1,100}", str(kind)
        ):
            raise AccountError("invalid_input", 400, "Not a Stripe event.")
        # 2. The ledger and the local subscription, without a lock.
        subscription_id = None
        if kind.startswith("checkout.session."):
            subscription_id = _id(obj.get("subscription"))
        elif kind.startswith("invoice."):
            subscription_id = invoice_subscription(obj)
        elif kind.startswith("customer.subscription."):
            subscription_id = _id(obj)
        with self.accounts.engine.connect() as conn:
            if conn.scalar(
                text(f"SELECT processed_at FROM {self._t(conn, 'stripe_webhook_event')} WHERE event_id = :e"),
                {"e": event_id},
            ):
                return 200, {"duplicate": True}
            local = self._local(conn, subscription_id) if subscription_id else None
        # 3. A subscription not known here is fetched from Stripe, outside any transaction; if Stripe fails,
        # nothing is written and Stripe sends the event again.
        fetched = None
        if kind.startswith("customer.subscription."):
            fetched = obj
        elif subscription_id and local is None and kind in EVENTS:
            try:
                fetched = client.get(f"/subscriptions/{subscription_id}")
            except StripeError as error:
                log.warning("stripe fetch failed for %s: %s", event_id, error.status)
                return 500, {"retry": True}
        # 4. One transaction.
        with self.accounts.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'stripe_webhook_event')} (event_id, event_type, outcome)"
                    " VALUES (:e, :k, 'received') ON CONFLICT (event_id) DO NOTHING"
                ),
                {"e": event_id, "k": kind},
            )
            if conn.scalar(
                text(
                    f"SELECT processed_at FROM {self._t(conn, 'stripe_webhook_event')} WHERE event_id = :e"
                    " FOR UPDATE"
                ),
                {"e": event_id},
            ):
                return 200, {"duplicate": True}
            outcome = self._dispatch(conn, event_id, kind, obj, created, subscription_id, local, fetched)
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'stripe_webhook_event')} SET processed_at = statement_timestamp(),"
                    " outcome = :o WHERE event_id = :e"
                ),
                {"o": outcome.name, "e": event_id},
            )
        # After the commit: what the handler decided to do in Stripe, and the operator's alert.
        for subscription in outcome.cancel:
            try:
                self._cancel_in_stripe(client, subscription)
            except StripeError as error:
                log.warning("cancel after commit failed (reconciliation repeats it): %s", error.status)
        if outcome.alert:
            self.alert(
                outcome.alert,
                {"event": event_id, "type": kind, "subscription": subscription_id, "outcome": outcome.name},
            )
        return 200, {"received": True, "outcome": outcome.name}

    def _local(self, conn, subscription_id: str, *, lock: bool = False) -> dict | None:
        row = conn.execute(
            text(
                f"SELECT * FROM {self._t(conn, 'billing_subscription')} WHERE stripe_subscription_id = :s"
                + (" FOR UPDATE" if lock else "")
            ),
            {"s": subscription_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    def _organisation_of_customer(self, conn, customer: str | None) -> str | None:
        if not customer:
            return None
        return conn.scalar(
            text(
                f"SELECT organisation_id FROM {self._t(conn, 'billing_customer')} WHERE stripe_customer_id = :c"
            ),
            {"c": customer},
        )

    def _plan(self, conn, subscription: dict, recorded: dict | None = None) -> Plan | str:
        """A subscription as Stripe sent it, read strictly; or why it cannot be (an outcome's name). Its
        countries are those recorded here, else those its checkout chose (metadata), cut to the plan's."""
        mapped = self.client.plan_of(_price(subscription))
        if mapped is None:
            return "unmapped_price"
        tier, interval = mapped
        status = subscription.get("status")
        if status not in STATUSES:
            return "unexpected_status"
        metadata = subscription.get("metadata") or {}
        customer = _id(subscription.get("customer"))
        organisation = self._organisation_of_customer(conn, customer)
        if organisation is None:
            return "unknown_customer"
        if metadata.get("organisation_id") and metadata["organisation_id"] != organisation:
            return "customer_mismatch"
        chosen = recorded["scope"] if recorded else {"jurisdictions": _countries(metadata)}
        try:
            scope = self.accounts._plan_scope(conn, tier, self._trimmed(conn, tier, chosen))
        except AccountError:
            return "invalid_metadata"
        return Plan(
            subscription["id"],
            organisation,
            customer,
            tier,
            interval,
            subscription.get("collection_method") or "charge_automatically",
            status,
            scope,
            period_end(subscription),
            bool(subscription.get("cancel_at_period_end")),
            metadata,
        )

    def _record(self, conn, plan: Plan, *, event_at: datetime | None = None) -> None:
        """The subscription as Stripe last told it, inserted or brought up to date."""
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'billing_subscription')} AS s (stripe_subscription_id,"
                " organisation_id, tier_id, billing_interval, collection_method, status, scope,"
                " current_period_end, cancel_at_period_end, last_event_at) VALUES (:id, :o, :tier, :interval,"
                " :method, :status, CAST(:scope AS jsonb), :end, :cancel, :at)"
                " ON CONFLICT (stripe_subscription_id) DO UPDATE SET tier_id = EXCLUDED.tier_id,"
                " billing_interval = EXCLUDED.billing_interval, collection_method = EXCLUDED.collection_method,"
                " status = EXCLUDED.status, scope = EXCLUDED.scope, current_period_end = EXCLUDED.current_period_end,"
                " cancel_at_period_end = EXCLUDED.cancel_at_period_end,"
                " last_event_at = coalesce(EXCLUDED.last_event_at, s.last_event_at),"
                " past_due_since = CASE WHEN EXCLUDED.status = 'active' THEN NULL ELSE s.past_due_since END,"
                " updated_at = statement_timestamp()"
            ),
            {
                "id": plan.id,
                "o": plan.organisation_id,
                "tier": plan.tier,
                "interval": plan.interval,
                "method": plan.collection_method,
                "status": plan.status,
                "scope": json.dumps(plan.scope),
                "end": plan.period_end,
                "cancel": plan.cancel_at_period_end,
                "at": event_at,
            },
        )

    def _from_row(self, row: dict) -> Plan:
        return Plan(
            row["stripe_subscription_id"],
            row["organisation_id"],
            None,
            row["tier_id"],
            row["billing_interval"],
            row["collection_method"],
            row["status"],
            row["scope"],
            row["current_period_end"],
            row["cancel_at_period_end"],
            {},
        )

    def _dispatch(self, conn, event_id, kind, obj, created, subscription_id, local, fetched) -> Outcome:
        actor = ("system", "stripe:" + event_id)
        if kind not in EVENTS:
            return Outcome("ignored")
        if kind in ("charge.refunded", "charge.dispute.created"):
            organisation = self._organisation_of_customer(conn, _id(obj.get("customer")))
            if organisation:
                self.accounts._audit(
                    conn,
                    actor,
                    "billing.refunded" if kind == "charge.refunded" else "billing.disputed",
                    organisation=organisation,
                    target=("charge", _id(obj.get("charge")) or _id(obj)),
                )
            return Outcome("alerted", alert=kind)
        if kind == "checkout.session.expired":
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_customer')} SET open_checkout_session_id = NULL,"
                    " open_checkout_key = NULL, updated_at = statement_timestamp()"
                    " WHERE open_checkout_session_id = :s"
                ),
                {"s": _id(obj)},
            ).rowcount
            return Outcome("checkout_expired" if done else "ignored")
        if subscription_id is None:
            return Outcome("ignored")
        # The subscription: as fetched (or as the subscription event carries it), else as recorded here.
        unread = None
        if fetched is not None:
            plan = self._plan(conn, fetched, local)
            if isinstance(plan, str):
                over = "canceled" if kind == "customer.subscription.deleted" else fetched.get("status")
                if local is None or over not in OVER:
                    return Outcome(plan, alert=plan)
                # It cannot be read (its price is no longer one of ours: the lookup key moved to a new
                # price), but it is recorded here and Stripe says it is over. Ended as recorded, with no
                # plan guessed, so that nothing keeps access a cancelled subscription paid for; the operator
                # still hears why it could not be read.
                unread, plan = plan, self._from_row(local)
                plan.status = over
                plan.period_end = period_end(fetched) or plan.period_end
        else:
            plan = self._from_row(local)
            # The subscription's metadata as the event carries it: a session's own, an invoice's copy.
            details = (obj.get("parent") or {}).get("subscription_details") or obj.get("subscription_details")
            plan.metadata = (details or {}).get("metadata") or obj.get("metadata") or {}
        organisation = plan.organisation_id
        state = conn.scalar(
            text(f"SELECT state FROM {self._t(conn, 'organisation')} WHERE id = :o FOR UPDATE"),
            {"o": organisation},
        )
        recorded = self._local(conn, plan.id, lock=True)
        stale = (
            kind.startswith("customer.subscription.")
            and recorded is not None
            and recorded["last_event_at"] is not None
            and created < recorded["last_event_at"]
        )
        if stale:
            return Outcome("stale")
        if state == "deleted":
            if fetched is not None:
                self._record(conn, plan, event_at=created if kind.startswith("customer.") else None)
            if plan.status in LIVE:
                return Outcome("org_deleted_cancelled", cancel=[plan.id], alert="org_deleted_cancelled")
            return Outcome("org_deleted")
        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "invoice.paid": self._on_invoice_paid,
            "invoice.payment_failed": self._on_payment_failed,
            "invoice.voided": self._on_invoice_voided,
            "invoice.marked_uncollectible": self._on_uncollectible,
            "customer.subscription.updated": self._on_subscription_updated,
            "customer.subscription.deleted": self._on_subscription_deleted,
        }[kind]
        outcome = handler(conn, actor, obj, created, plan, recorded, fetched)
        if unread:
            outcome.alert = outcome.alert or unread
        return outcome

    # Each handler runs in the webhook's transaction with the organisation's row locked.
    def _on_checkout_completed(self, conn, actor, session, created, plan, recorded, fetched) -> Outcome:
        if session.get("mode") != "subscription":
            return Outcome("ignored")
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'billing_customer')} SET open_checkout_session_id = NULL,"
                " open_checkout_key = NULL, updated_at = statement_timestamp()"
                " WHERE organisation_id = :o AND open_checkout_session_id = :s"
            ),
            {"o": plan.organisation_id, "s": session.get("id")},
        )
        other = conn.scalar(
            text(
                f"SELECT stripe_subscription_id FROM {self._t(conn, 'billing_subscription')}"
                " WHERE organisation_id = :o AND stripe_subscription_id <> :s AND status = ANY(:live) LIMIT 1"
            ),
            {"o": plan.organisation_id, "s": plan.id, "live": list(SUBSCRIBED)},
        )
        if fetched is not None:
            self._record(conn, plan)
        if other is not None:
            # A second checkout went through while another subscription runs: keep the first.
            return Outcome("duplicate_cancelled", cancel=[plan.id], alert="duplicate_subscription")
        if session.get("payment_status") == "paid" and plan.status in ("active", "trialing", "past_due"):
            return self._grant(conn, actor, plan)
        return Outcome("linked")

    def _on_invoice_paid(self, conn, actor, invoice, created, plan, recorded, fetched) -> Outcome:
        if fetched is not None:
            self._record(conn, plan)
        if plan.status in ENDED:
            return Outcome("subscription_cancelled", alert="paid_after_cancel")
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'billing_subscription')} SET invoice_paid = true, past_due_since = NULL,"
                " latest_invoice_id = :i, latest_invoice_url = :u, updated_at = statement_timestamp()"
                " WHERE stripe_subscription_id = :s"
            ),
            {"i": invoice.get("id"), "u": invoice.get("hosted_invoice_url"), "s": plan.id},
        )
        # Granted whatever the status, but for those two: a first annual invoice paid after its due date
        # arrives with the subscription past_due, as does a card payment Smart Retries recovered.
        return self._grant(conn, actor, plan)

    def _on_payment_failed(self, conn, actor, invoice, created, plan, recorded, fetched) -> Outcome:
        if fetched is not None:
            self._record(conn, plan)
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'billing_subscription')} SET past_due_since ="
                " coalesce(past_due_since, statement_timestamp()), updated_at = statement_timestamp()"
                " WHERE stripe_subscription_id = :s"
            ),
            {"s": plan.id},
        )
        self.accounts._audit(
            conn,
            actor,
            "billing.payment_failed",
            organisation=plan.organisation_id,
            target=("invoice", invoice.get("id")),
        )
        return Outcome("payment_failed")

    def _on_invoice_voided(self, conn, actor, invoice, created, plan, recorded, fetched) -> Outcome:
        if fetched is not None:
            self._record(conn, plan)
        cleared = conn.execute(
            text(
                f"UPDATE {self._t(conn, 'billing_subscription')} SET latest_invoice_id = NULL,"
                " latest_invoice_url = NULL, updated_at = statement_timestamp()"
                " WHERE stripe_subscription_id = :s AND NOT invoice_paid AND latest_invoice_id = :i"
            ),
            {"s": plan.id, "i": invoice.get("id")},
        ).rowcount
        self.accounts._audit(
            conn,
            actor,
            "billing.invoice_voided",
            organisation=plan.organisation_id,
            target=("invoice", invoice.get("id")),
        )
        # A void is never an ending: an operator may void and issue again.
        return Outcome(
            "pending_invoice_cleared" if cleared else "alerted", alert=None if cleared else "invoice_voided"
        )

    def _on_uncollectible(self, conn, actor, invoice, created, plan, recorded, fetched) -> Outcome:
        if fetched is not None:
            self._record(conn, plan)
        ended = self._end_if_ours(conn, actor, plan, "Invoice marked uncollectible")
        return Outcome("ended" if ended else "uncollectible_noted", alert="invoice_uncollectible")

    def _on_subscription_updated(
        self, conn, actor, subscription, created, plan, recorded, fetched
    ) -> Outcome:
        before = recorded["tier_id"] if recorded else None
        if recorded and before != plan.tier:
            # The plan switched (the portal): the countries in use now, cut to the new plan's, and the first
            # country change after it is free (§6.6).
            current = self.accounts.current_entitlement(conn, plan.organisation_id)
            ours = current and current["source"] == "payment" and current["reference"] == plan.id
            plan.scope = self.accounts._plan_scope(
                conn,
                plan.tier,
                self._trimmed(conn, plan.tier, current["scope"] if ours else recorded["scope"]),
            )
        self._record(conn, plan, event_at=created)
        outcome = "synced"
        if recorded and before != plan.tier:
            self.accounts._audit(
                conn,
                actor,
                "billing.plan_changed",
                organisation=plan.organisation_id,
                target=("subscription", plan.id),
                summary={"from": before, "to": plan.tier},
            )
            current = self.accounts.current_entitlement(conn, plan.organisation_id)
            if current and current["source"] == "payment" and current["reference"] == plan.id:
                self._grant(conn, actor, plan)
                outcome = "plan_changed"
        if plan.status in ("unpaid", "canceled"):
            if self._end_if_ours(conn, actor, plan, f"Subscription {plan.status}"):
                outcome = "ended"
        elif plan.status == "active":
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'billing_subscription')} SET past_due_since = NULL"
                    " WHERE stripe_subscription_id = :s"
                ),
                {"s": plan.id},
            )
        self.accounts._sync_seat_grace(conn, plan.organisation_id, actor=actor)
        return Outcome(
            outcome,
            alert="unexpected_status"
            if plan.status in ("paused", "incomplete")
            and recorded is not None
            and recorded["status"] != plan.status
            else None,
        )

    def _on_subscription_deleted(
        self, conn, actor, subscription, created, plan, recorded, fetched
    ) -> Outcome:
        plan.status = "canceled"
        if recorded is not None:
            plan.tier, plan.scope = recorded["tier_id"], recorded["scope"]
        self._record(conn, plan, event_at=created)
        ended = self._end_if_ours(conn, actor, plan, "Subscription cancelled")
        return Outcome("ended" if ended else "cancel_noted")

    def _trimmed(self, conn, tier: str, scope: dict) -> dict:
        """Team (5) to Pro (1): the first country; Pro to Team keeps it (the owner adds the rest). A plan
        without a number of countries covers them all."""
        countries = conn.scalar(
            text(f"SELECT (allowance->>'jurisdictions')::int FROM {self._t(conn, 'tier')} WHERE id = :t"),
            {"t": tier},
        )
        chosen = list((scope or {}).get("jurisdictions") or [])
        if countries is None or not chosen:
            return scope
        return {"jurisdictions": sorted(chosen)[:countries]}

    def _end_if_ours(self, conn, actor, plan: Plan, reason: str) -> bool:
        """End paid access only if this subscription is what grants it: never an operator's grant, never
        another subscription's (stripe-billing §6.7)."""
        current = self.accounts.current_entitlement(conn, plan.organisation_id)
        if not current or current["source"] != "payment" or current["reference"] != plan.id:
            return False
        self.accounts._change_entitlement(conn, current["id"], "ended", actor, reason, None)
        self.accounts._sync_seat_grace(conn, plan.organisation_id, actor=actor)
        return True

    def _grant(self, conn, actor, plan: Plan) -> Outcome:
        """Paid access from this subscription, once, with no end date (Stripe cancels; §2): kept as it is
        when it is already this subscription's plan; replaced, with ``plan.scope``, when the plan changed."""
        organisation = plan.organisation_id
        access = self.accounts.access(organisation, conn)
        current = self.accounts.current_entitlement(conn, organisation)
        if current is None and access.get("state") == "revoked":
            return Outcome("revoked_hold", alert="revoked_hold")
        if current and current["source"] in ("manual", "waiver"):
            return Outcome("higher_grant_kept", alert="higher_grant_kept")
        if current and current["reference"] != plan.id:
            return Outcome("duplicate_cancelled", cancel=[plan.id], alert="duplicate_subscription")
        if current and current["tier_id"] == plan.tier:
            # Already this subscription's plan (a renewal, the other of checkout and invoice.paid): kept as it
            # is, with any countries the owner has changed since.
            self.accounts._sync_seat_grace(conn, organisation, actor=actor)
            return Outcome("already_granted")
        if current:
            self.accounts._change_entitlement(
                conn, current["id"], "ended", actor, "Plan changed in Stripe", None
            )
        entitlement = self.accounts._start_entitlement(
            conn, organisation, plan.tier, "payment", plan.scope, None, plan.id, "Stripe subscription", actor
        )
        self.accounts._audit(
            conn,
            actor,
            "entitlement.granted",
            organisation=organisation,
            target=("entitlement", entitlement),
            summary={"tier": plan.tier, "scope": plan.scope, "source": "payment"},
        )
        team_name = (plan.metadata or {}).get("team_name")
        if plan.tier == "team" and team_name:
            renamed = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'organisation')} SET name = :n, personal_of = NULL,"
                    " revision = revision + 1, updated_at = statement_timestamp()"
                    " WHERE id = :o AND personal_of IS NOT NULL"
                ),
                {"n": " ".join(str(team_name).split())[:120], "o": organisation},
            ).rowcount
            if renamed:
                self.accounts._audit(
                    conn,
                    actor,
                    "organisation.updated",
                    organisation=organisation,
                    target=("organisation", organisation),
                    summary={"fields": ["name"], "team": True},
                )
        self.accounts._sync_seat_grace(conn, organisation, actor=actor)
        return Outcome("granted")

    # ---------- the daily reconciliation (stripe-billing §2) ----------
    def reconcile(self) -> dict:
        """Run as open_alvary_jobs. Each subscription's change is its own short transaction; Stripe is
        called outside them. Ends access Stripe has ended, records subscriptions found only in Stripe,
        cancels a deleted organisation's live subscription and duplicates, alerts on what it does not
        expect, and settles every organisation's seat grace."""
        done = {"checked": 0, "ended": 0, "recorded": 0, "cancelled": 0, "alerts": 0, "graces": 0}
        actor = ("system", "stripe:reconcile")
        if self.client is not None:
            client = self.client
            with self.accounts.engine.connect() as conn:
                local = [
                    dict(r)
                    for r in conn.execute(
                        text(
                            f"SELECT stripe_subscription_id, organisation_id, status, invoice_paid"
                            f" FROM {self._t(conn, 'billing_subscription')} WHERE status <> ALL(:ended)"
                        ),
                        {"ended": list(ENDED)},
                    ).mappings()
                ]
                customers = conn.scalars(
                    text(f"SELECT stripe_customer_id FROM {self._t(conn, 'billing_customer')}")
                ).all()
            seen: dict[str, dict] = {}
            for row in local:
                try:
                    seen[row["stripe_subscription_id"]] = client.get(
                        f"/subscriptions/{row['stripe_subscription_id']}"
                    )
                except StripeError as error:
                    if error.code == "resource_missing":
                        seen[row["stripe_subscription_id"]] = {
                            **row,
                            "id": row["stripe_subscription_id"],
                            "status": "canceled",
                            "_gone": True,
                        }
                    else:
                        log.warning("reconcile: Stripe failed for a subscription: %s", error.status)
            for customer in customers:
                try:
                    for subscription in client.subscriptions_of(customer):
                        seen.setdefault(subscription["id"], subscription)
                except StripeError as error:
                    log.warning("reconcile: Stripe failed listing a customer: %s", error.status)
            paid_here = {row["stripe_subscription_id"] for row in local if row["invoice_paid"]}
            for _, subscription in sorted(seen.items()):
                done["checked"] += 1
                # An invoice subscription is active before its invoice is paid: whether one not known paid
                # here has been paid is asked of Stripe, outside any transaction.
                if (
                    subscription.get("collection_method") == "send_invoice"
                    and subscription.get("status") == "active"
                    and subscription["id"] not in paid_here
                    and _id(subscription.get("latest_invoice"))
                ):
                    try:
                        invoice = client.get(f"/invoices/{_id(subscription['latest_invoice'])}")
                        subscription = {**subscription, "_paid": invoice.get("status") == "paid"}
                    except StripeError as error:
                        log.warning("reconcile: Stripe failed for an invoice: %s", error.status)
                result = self._reconcile_one(subscription, actor)
                for name in ("ended", "recorded", "alerts"):
                    done[name] += result.get(name, 0)
                for cancel in result.get("cancel", []):
                    try:
                        self._cancel_in_stripe(client, cancel)
                        done["cancelled"] += 1
                    except StripeError as error:
                        log.warning("reconcile: cancel failed, tried again tomorrow: %s", error.status)
        with self.accounts.engine.connect() as conn:
            organisations = conn.scalars(
                text(
                    f"SELECT o.id FROM {self._t(conn, 'organisation')} o WHERE o.state <> 'deleted'"
                    f" AND (o.seat_grace_until IS NOT NULL OR (SELECT count(*) FROM {self._t(conn, 'membership')} m"
                    " WHERE m.organisation_id = o.id) > 1)"
                )
            ).all()
        for organisation in organisations:
            with self.accounts.engine.begin() as conn:
                before = conn.scalar(
                    text(f"SELECT seat_grace_until FROM {self._t(conn, 'organisation')} WHERE id = :o"),
                    {"o": organisation},
                )
                if self.accounts._sync_seat_grace(conn, organisation, actor=("system", "seats")) != before:
                    done["graces"] += 1
        return done

    def _reconcile_one(self, subscription: dict, actor) -> dict:
        """One subscription, in its own transaction. This job ends access and records; it never grants and
        never changes a plan (its login cannot): what only a webhook can put right, it tells the operator,
        each day until it is. Alerts go out after the commit."""
        result: dict[str, Any] = {"cancel": []}
        alerts: list[tuple[str, dict]] = []
        gone = subscription.get("_gone")
        with self.accounts.engine.begin() as conn:
            recorded = self._local(conn, subscription["id"], lock=True)
            plan: Plan | str | None
            if gone:
                plan = self._from_row(recorded)
                plan.status = "canceled"
            else:
                plan = self._plan(conn, subscription)
                if isinstance(plan, str):
                    alerts.append(("reconcile_" + plan, {"subscription": subscription.get("id")}))
                    if recorded is not None and subscription.get("status") in OVER:
                        # As the webhook does: one that can no longer be read, but is recorded here and
                        # over in Stripe, is ended as recorded.
                        plan = self._from_row(recorded)
                        plan.status = subscription["status"]
                    else:
                        plan = None
            if plan is not None:
                self._reconcile_plan(conn, actor, plan, recorded, subscription, result, alerts)
        result["alerts"] = len(alerts)
        for what, details in alerts:
            self.alert(what, details)
        return result

    def _reconcile_plan(self, conn, actor, plan: Plan, recorded, subscription, result, alerts) -> None:
        state = conn.scalar(
            text(f"SELECT state FROM {self._t(conn, 'organisation')} WHERE id = :o FOR UPDATE"),
            {"o": plan.organisation_id},
        )
        if recorded is None:
            # A subscription only Stripe knew (a local write that failed): recorded, and the operator told,
            # since recording it grants nothing.
            result["recorded"] = 1
            alerts.append(("reconcile_recorded", {"subscription": plan.id, "status": plan.status}))
        elif recorded["tier_id"] != plan.tier:
            # The webhook changes plans; this only notices, and says so.
            alerts.append(
                (
                    "reconcile_plan_differs",
                    {"subscription": plan.id, "recorded": recorded["tier_id"], "stripe": plan.tier},
                )
            )
            plan.scope = recorded["scope"]
            plan.tier = recorded["tier_id"]
        self._record(conn, plan)
        if plan.status in ("canceled", "unpaid", "incomplete_expired") and self._end_if_ours(
            conn, actor, plan, f"Subscription {plan.status} (reconciliation)"
        ):
            result["ended"] = 1
        if plan.status in LIVE and state == "deleted":
            result["cancel"].append(plan.id)
            alerts.append(("reconcile_org_deleted", {"subscription": plan.id}))
        elif plan.status in SUBSCRIBED:
            current = self.accounts.current_entitlement(conn, plan.organisation_id)
            paid = (
                plan.collection_method == "charge_automatically"
                or bool(recorded and recorded["invoice_paid"])
                or bool(subscription.get("_paid"))
            )
            if current and current["source"] == "payment" and current["reference"] != plan.id:
                result["cancel"].append(plan.id)
                alerts.append(("reconcile_duplicate", {"subscription": plan.id}))
            elif current is None and plan.status == "active" and paid:
                # Paid in Stripe, and the organisation is on Starter: the webhook that grants never came,
                # or its grant was held. Sending the event again from Stripe grants it.
                alerts.append(("reconcile_paid_without_access", {"subscription": plan.id}))
        if plan.status in ("paused", "incomplete"):
            alerts.append(("reconcile_unexpected_status", {"subscription": plan.id, "status": plan.status}))

    # ---------- telling owners a seat grace has started (teams-and-seats §5.3) ----------
    def seat_notices(self, mailer, base: str) -> int:
        """Run daily as open_alvary_jobs. Each organisation whose seat grace has started and whose owners
        have not been told since is mailed once, and the mail is recorded (seats.notice_sent) so that the
        next run sends nothing. Whatever started the grace is covered: a cancelled subscription, a grant
        that lapsed, an operator's change. Without a mail key nothing is recorded, and it is tried again."""
        with self.accounts.engine.connect() as conn:
            audit = self._t(conn, "audit_event")
            due = conn.scalars(
                text(
                    f"SELECT o.id FROM {self._t(conn, 'organisation')} o WHERE o.state <> 'deleted'"
                    " AND o.seat_grace_until > statement_timestamp() AND NOT EXISTS ("
                    f"SELECT 1 FROM {audit} n WHERE n.organisation_id = o.id AND n.action = 'seats.notice_sent'"
                    f" AND n.seq > coalesce((SELECT max(g.seq) FROM {audit} g WHERE g.organisation_id = o.id"
                    " AND g.action = 'seats.grace_started'), 0)) ORDER BY o.id"
                )
            ).all()
        sent = 0
        for organisation in due:
            with self.accounts.engine.connect() as conn:
                row = (
                    conn.execute(
                        text(
                            f"SELECT o.name, o.seat_grace_until, (SELECT count(*) FROM {self._t(conn, 'membership')} m"
                            " WHERE m.organisation_id = o.id) AS members,"
                            f" {self.accounts._fn(conn, 'organisation_seat_limit')}(o.id) AS seats"
                            f" FROM {self._t(conn, 'organisation')} o WHERE o.id = :o"
                        ),
                        {"o": organisation},
                    )
                    .mappings()
                    .one()
                )
            if row["seat_grace_until"] is None or row["members"] <= row["seats"]:
                continue
            owners = self.accounts.notice_recipients(organisation)["to"]
            delivered = sum(
                bool(
                    mailer.send(
                        mail.seats_over_limit(
                            to, row["name"], row["members"], row["seats"], row["seat_grace_until"], base
                        )
                    )
                )
                for to in owners
            )
            if not delivered:
                continue
            with self.accounts.engine.begin() as conn:
                self.accounts._audit(
                    conn,
                    ("system", "seats"),
                    "seats.notice_sent",
                    organisation=organisation,
                    target=("organisation", organisation),
                    summary={
                        "members": row["members"],
                        "seats": row["seats"],
                        "until": row["seat_grace_until"].isoformat(),
                    },
                )
            sent += 1
        return sent


def purge_webhook_events(accounts: Accounts, conn) -> int:
    """Stripe's events are kept 90 days (stripe-billing §4.3): long past Stripe's three days of retries."""
    return conn.execute(
        text(
            f"DELETE FROM {accounts._t(conn, 'stripe_webhook_event')}"
            f" WHERE received_at < statement_timestamp() - make_interval(days => {WEBHOOK_EVENTS_KEPT_DAYS})"
        )
    ).rowcount


def from_environment(accounts: Accounts, alert: Callable[[str, dict], None] | None = None) -> Billing:
    config = StripeConfig.from_environment()
    return Billing(accounts, StripeClient(config) if config else None, alert=alert)


def main(argv=None) -> int:
    from api.store import engine_for
    from changes.worker import accounts_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["reconcile"])
    parser.parse_args(argv)
    # The job's alerts reach the operator by mail, as the console's do; without a mail key they are logged.
    mailer = mail.Mailer.from_environment()
    operator = os.getenv("ALVARY_OPERATOR_EMAIL", "hello@ruhu.ai")
    billing = from_environment(
        accounts_for(engine_for()),
        lambda what, details: mailer.send(mail.billing_alert(operator, what, details)),
    )
    done = billing.reconcile()
    done["notices"] = billing.seat_notices(
        mailer, os.getenv("ALVARY_CONSOLE_BASE_URL", "https://open.alvary.ai")
    )
    print(json.dumps({"enabled": billing.enabled, **done}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
