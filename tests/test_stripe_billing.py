"""Stripe billing's pure parts, without a database (docs: stripe-billing §1.2, §3, §6): the signature check, the
pinned API version, reading 2025-03-31.basil payloads with the older fields as a fallback, and strict price
mapping. What billing does to accounts is tested against PostgreSQL in tests/test_postgres_billing.py."""

import hashlib
import hmac

import httpx
import pytest

from accounts.billing import (
    STRIPE_VERSION,
    StripeClient,
    StripeConfig,
    StripeError,
    _form,
    invoice_subscription,
    period_end,
    verify_signature,
)
from accounts.store import AccountError
from api import credentials


def sign(body: bytes, secret: str, at: int) -> str:
    return f"t={at},v1=" + hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()


def test_the_signature_is_checked_over_the_raw_body_within_five_minutes():
    body, secret, at = b'{"id":"evt_1"}', "whsec_x", 1_700_000_000
    verify_signature(body, sign(body, secret, at), secret, now=at + 299)
    verify_signature(body, "v1=00," + sign(body, secret, at), secret, now=at)  # any of several v1 values
    for header, now in ((sign(body + b" ", secret, at), at), (sign(body, secret, at), at + 301), ("", at)):
        with pytest.raises(AccountError) as refused:
            verify_signature(body, header, secret, now=now)
        assert (refused.value.code, refused.value.status) == ("invalid_signature", 400)
    with pytest.raises(AccountError) as unset:
        verify_signature(body, sign(body, secret, at), "", now=at)
    assert unset.value.code == "billing_unavailable"


def test_every_request_names_the_pinned_api_version():
    seen = []

    def answer(request):
        seen.append(request.headers)
        return httpx.Response(200, json={"data": []})

    client = StripeClient(StripeConfig("sk_test_x"), http=httpx.Client(transport=httpx.MockTransport(answer)))
    client.post("/customers", {"metadata": {"organisation_id": "org_1"}}, key="k")
    assert STRIPE_VERSION == "2025-03-31.basil"
    assert seen[0]["Stripe-Version"] == STRIPE_VERSION and seen[0]["Idempotency-Key"] == "k"


def test_an_unreachable_stripe_and_a_refusal_are_told_apart():
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    client = StripeClient(StripeConfig("sk_test_x"), http=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(StripeError) as unreachable:
        client.get("/subscriptions/sub_1")
    assert unreachable.value.unreachable
    refusing = httpx.MockTransport(
        lambda request: httpx.Response(
            404, json={"error": {"code": "resource_missing", "message": "No such"}}
        )
    )
    with pytest.raises(StripeError) as missing:
        StripeClient(StripeConfig("sk_test_x"), http=httpx.Client(transport=refusing)).get(
            "/subscriptions/sub_1"
        )
    assert not missing.value.unreachable and missing.value.code == "resource_missing"


def test_basil_payloads_are_read_with_the_older_fields_as_a_fallback():
    basil = {"parent": {"subscription_details": {"subscription": "sub_new"}}, "subscription": None}
    assert invoice_subscription(basil) == "sub_new"
    assert invoice_subscription({"subscription": "sub_old"}) == "sub_old"
    assert invoice_subscription({"subscription": {"id": "sub_expanded"}}) == "sub_expanded"
    assert invoice_subscription({"id": "in_1"}) is None
    item_end = {"items": {"data": [{"current_period_end": 1_800_000_000}]}, "current_period_end": 1}
    assert period_end(item_end).timestamp() == 1_800_000_000
    assert period_end({"current_period_end": 1_700_000_000}).timestamp() == 1_700_000_000


def test_a_price_maps_to_a_plan_only_by_its_lookup_key_or_a_configured_id():
    client = StripeClient(StripeConfig("sk_test_x", price_ids={("team", "year"): "price_team_annual"}))
    assert client.plan_of({"id": "price_any", "lookup_key": "oa_pro_monthly"}) == ("developer", "month")
    assert client.plan_of({"id": "price_team_annual", "lookup_key": None}) == ("team", "year")
    assert client.plan_of({"id": "price_unknown", "lookup_key": "team"}) is None
    assert client.plan_of(None) is None


def test_stripes_form_encoding_nests_objects_and_lists():
    assert _form({"a": {"b": "c"}, "items": [{"price": "p", "quantity": 1}], "on": True, "skip": None}) == [
        ("a[b]", "c"),
        ("items[0][price]", "p"),
        ("items[0][quantity]", "1"),
        ("on", "true"),
    ]


def test_a_seat_refusal_is_a_403_with_its_own_wording():
    assert credentials.STATUS["seat_limit_exceeded"] == 403
    assert credentials.MCP_NUMBERS[credentials.STATUS["seat_limit_exceeded"]] == 10002
    assert "owner" in credentials.DETAIL["seat_limit_exceeded"]
