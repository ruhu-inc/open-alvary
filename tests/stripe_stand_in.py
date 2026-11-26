"""A stand-in for Stripe's REST API, as an httpx transport: no network. It keeps customers, prices, Checkout
Sessions, subscriptions and invoices in memory in the shapes of API version 2025-03-31.basil, honours
Idempotency-Key, records every call, and can be made unreachable. Events are signed as Stripe signs them."""

import copy
import hashlib
import hmac
import json
import time
from itertools import count
from urllib.parse import parse_qsl

import httpx

from accounts.billing import STRIPE_VERSION, Billing, StripeClient, StripeConfig

SECRET = "whsec_stand_in"
PRICES = {
    "oa_pro_monthly": 4900,
    "oa_pro_annual": 49000,
    "oa_team_monthly": 39900,
    "oa_team_annual": 399000,
}
_events = count(1)


def _nested(form: list[tuple[str, str]]) -> dict:
    """Stripe's bracketed form, read back into objects (lists come back as dicts keyed by index)."""
    result: dict = {}
    for key, value in form:
        parts = key.replace("]", "").split("[")
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


class StandInStripe:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.down = False
        # Tests set these: (method, path) -> True for a call that cannot be reached; (method, path) ->
        # (status, body) for one Stripe refuses.
        self.unreachable = None
        self.refuse = None
        self.ids = count(1)
        self.prices = {
            key: {"id": f"price_{key}", "lookup_key": key, "unit_amount": amount, "currency": "usd"}
            for key, amount in PRICES.items()
        }
        self.customers: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.replayed: dict[str, tuple[int, dict]] = {}

    # ---------- making Stripe's objects ----------
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self.ids):06d}"

    def subscription(
        self,
        customer: str,
        lookup: str,
        *,
        status: str = "active",
        metadata: dict | None = None,
        method: str = "charge_automatically",
        sub_id: str | None = None,
    ) -> dict:
        sub = {
            "id": sub_id or self.new_id("sub"),
            "object": "subscription",
            "customer": customer,
            "status": status,
            "collection_method": method,
            "cancel_at_period_end": False,
            "metadata": metadata or {},
            "latest_invoice": None,
            "items": {
                "data": [{"price": self.prices[lookup], "current_period_end": int(time.time()) + 30 * 86400}]
            },
        }
        self.subscriptions[sub["id"]] = sub
        return sub

    def invoice(self, sub: dict, *, status: str = "open") -> dict:
        invoice = {
            "id": self.new_id("in"),
            "object": "invoice",
            "status": status,
            "customer": sub["customer"],
            "hosted_invoice_url": f"https://invoice.stripe.test/{sub['id']}",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": sub["id"], "metadata": sub["metadata"]},
            },
        }
        self.invoices[invoice["id"]] = invoice
        sub["latest_invoice"] = invoice["id"]
        return invoice

    def complete(self, session_id: str, *, status: str = "active") -> tuple[dict, dict]:
        """A customer pays at Checkout: the session completes and its subscription exists in Stripe."""
        session = self.sessions[session_id]
        lookup = session["line_items"]["0"]["price"].removeprefix("price_")
        sub = self.subscription(session["customer"], lookup, status=status, metadata=session["metadata"])
        session.update(status="complete", payment_status="paid", subscription=sub["id"])
        return session, sub

    # ---------- the API ----------
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Stripe is unreachable", request=request)
        assert request.headers["Stripe-Version"] == STRIPE_VERSION
        assert request.headers["Authorization"] == "Bearer sk_test_stand_in"
        body = _nested(parse_qsl(request.content.decode())) if request.content else {}
        path = request.url.path.removeprefix("/v1")
        self.calls.append((request.method, path, body))
        if self.unreachable and self.unreachable(request.method, path):
            raise httpx.ConnectError("Stripe is unreachable", request=request)
        refused = self.refuse(request.method, path) if self.refuse else None
        if refused:
            return httpx.Response(refused[0], json=refused[1])
        key = request.headers.get("Idempotency-Key")
        if key and key in self.replayed:
            status, answer = self.replayed[key]
            return httpx.Response(status, json=copy.deepcopy(answer))
        status, answer = self.route(request.method, path, dict(request.url.params), body)
        if key:
            self.replayed[key] = (status, copy.deepcopy(answer))
        return httpx.Response(status, json=answer)

    def missing(self, what: str):
        return 404, {"error": {"code": "resource_missing", "message": f"No such {what}"}}

    def route(self, method: str, path: str, params: dict, body: dict):
        parts = path.strip("/").split("/")
        if parts[0] == "prices":
            if len(parts) == 2:
                found = next((p for p in self.prices.values() if p["id"] == parts[1]), None)
                return (200, found) if found else self.missing("price")
            lookup = params.get("lookup_keys[]")
            return 200, {"data": [self.prices[lookup]] if lookup in self.prices else []}
        if parts[0] == "customers":
            if len(parts) == 1:
                customer = {"id": self.new_id("cus"), **body}
                self.customers[customer["id"]] = customer
                return 200, customer
            self.customers[parts[1]].update(body)
            return 200, self.customers[parts[1]]
        if parts[0] == "checkout":
            if method == "POST" and len(parts) == 2:
                session = {
                    "id": self.new_id("cs"),
                    "object": "checkout.session",
                    "mode": body["mode"],
                    "status": "open",
                    "payment_status": "unpaid",
                    "customer": body["customer"],
                    "metadata": body.get("metadata", {}),
                    "line_items": body["line_items"],
                    "subscription": None,
                }
                session["url"] = f"https://checkout.stripe.test/{session['id']}"
                self.sessions[session["id"]] = session
                return 200, session
            session = self.sessions.get(parts[2])
            if session is None:
                return self.missing("checkout session")
            if len(parts) == 4 and parts[3] == "expire":
                if session["status"] != "open":
                    return 400, {"error": {"code": "checkout_session_not_open", "message": "Not open"}}
                session["status"] = "expired"
            return 200, session
        if parts[0] == "subscriptions":
            if len(parts) == 1 and method == "GET":
                found = [s for s in self.subscriptions.values() if s["customer"] == params.get("customer")]
                return 200, {"data": found, "has_more": False}
            if len(parts) == 1:
                lookup = body["items"]["0"]["price"].removeprefix("price_")
                sub = self.subscription(
                    body["customer"],
                    lookup,
                    metadata=body.get("metadata", {}),
                    method=body.get("collection_method", "charge_automatically"),
                )
                answer = copy.deepcopy(sub)
                answer["latest_invoice"] = self.invoice(sub, status="draft")
                return 200, answer
            sub = self.subscriptions.get(parts[1])
            if sub is None:
                return self.missing("subscription")
            if method == "DELETE":
                sub["status"] = "canceled"
            return 200, sub
        if parts[0] == "invoices":
            if len(parts) == 1:
                found = [
                    i
                    for i in self.invoices.values()
                    if i["parent"]["subscription_details"]["subscription"] == params.get("subscription")
                    and i["status"] == params.get("status", i["status"])
                ]
                return 200, {"data": found, "has_more": False}
            invoice = self.invoices.get(parts[1])
            if invoice is None:
                return self.missing("invoice")
            action = parts[2] if len(parts) == 3 else None
            if action == "finalize":
                invoice["status"] = "open"
            elif action == "void":
                if invoice["status"] != "open":
                    return 400, {"error": {"code": "invoice_not_open", "message": "Not open"}}
                invoice["status"] = "void"
            return 200, invoice
        if parts[0] == "billing_portal":
            return 200, {"id": self.new_id("bps"), "url": "https://billing.stripe.test/portal"}
        raise AssertionError(f"Unexpected Stripe call: {method} {path}")

    def posted(self, path_start: str) -> list[dict]:
        return [body for method, path, body in self.calls if method == "POST" and path.startswith(path_start)]


def billing_with(accounts, stand_in: StandInStripe, alerts: list | None = None) -> Billing:
    client = StripeClient(
        StripeConfig("sk_test_stand_in", SECRET, portal_config_id="bpc_test"),
        http=httpx.Client(transport=httpx.MockTransport(stand_in.handler)),
    )
    return Billing(
        accounts,
        client,
        alert=(lambda what, details: alerts.append((what, details))) if alerts is not None else None,
    )


def signed(
    kind: str, obj: dict, *, created: int | None = None, event_id: str | None = None
) -> tuple[bytes, str]:
    """A Stripe event and its Stripe-Signature header."""
    event = {
        "id": event_id or f"evt_stand_in_{next(_events):06d}",
        "object": "event",
        "api_version": STRIPE_VERSION,
        "type": kind,
        "created": created or int(time.time()),
        "data": {"object": copy.deepcopy(obj)},
    }
    body = json.dumps(event).encode()
    now = int(time.time())
    mac = hmac.new(SECRET.encode(), f"{now}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={now},v1={mac}"


def send(billing: Billing, kind: str, obj: dict, **kwargs) -> tuple[int, dict]:
    return billing.handle_webhook(*signed(kind, obj, **kwargs))
