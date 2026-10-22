"""The credential gate (MAC-05–07) without a database: challenges, the second check, tokens."""

import base64
import json
import math
import time
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from accounts import keys
from api.credentials import GRANT_CLAIM, CredentialGate, GateConfig, TokenVerifier, problem
from rights.bytes import LedgerUnavailable, Reservation

ISSUER = "https://login.example.test/"
RESOURCE = "https://api.example.test/api/v1"
METADATA = "https://api.example.test/.well-known/oauth-protected-resource/api/v1"
PEPPER = b"q" * 32
OK = {
    "ok": True,
    "organisation": "org_00000000000000000000000000",
    "credential": "key_" + "a" * 32,
    "tier": {"id": "pilot", "includes": ["rest"], "allowance": {"requests_per_minute": 60}},
    "scope": {"all": True},
}


def app():
    inner = FastAPI()

    @inner.get("/sources")
    def sources(request: Request):
        return {"organisation": request.state.access["organisation"]}

    @inner.get("/tiers")
    def tiers():
        return {"tiers": []}

    @inner.get("/unchanged")
    def unchanged():
        return Response(status_code=304)

    @inner.get("/missing")
    def missing(request: Request):
        return problem("unavailable", request.state.request_id)

    return inner


def gate(check, verifier=None, usage=None, ledger=None, admission=None):
    parent = FastAPI()
    parent.mount(
        "/api/v1",
        CredentialGate(
            app(),
            config=GateConfig(RESOURCE, "rest", "api", METADATA, public_paths=frozenset({"/tiers"})),
            check=check,
            record_usage=(usage.append if usage is not None else lambda e: None),
            admission=admission,
            verifier=verifier,
            key_pepper=PEPPER,
            ledger=ledger,
        ),
    )
    return TestClient(parent)


class Ledger:
    """rights.bytes.ByteLedger's steps in memory: reserve (charged at once), begin, settle or release."""

    def __init__(self, down=False):
        self.down, self.spent, self.open, self.steps = down, {}, {}, []

    def reserve(self, admission_id, nbytes, scopes, lease_seconds):
        if self.down:
            raise LedgerUnavailable("down")
        [scope] = scopes
        self.steps.append(("reserve", scope, nbytes))
        used = self.spent.get(scope.key, 0) + sum(n for k, n in self.open.values() if k == scope.key)
        if used + nbytes > scope.capacity:
            wait = math.ceil((used + nbytes - scope.capacity) * 60 / scope.refill_per_minute)
            return Reservation(False, blocked=(scope.key,), retry_after=wait)
        reservation = uuid4()
        self.open[reservation] = (scope.key, nbytes)
        return Reservation(True, reservation)

    def begin_emission(self, reservation):
        self.steps.append(("begin", reservation in self.open))
        return True

    def settle(self, reservation, sent, receipt):
        key, _ = self.open.pop(reservation)
        self.spent[key] = self.spent.get(key, 0) + sent
        self.steps.append(("settle", sent, receipt))

    def release(self, reservation):
        self.open.pop(reservation)
        self.steps.append(("release",))


def test_missing_malformed_and_address_credentials_are_refused_with_the_right_challenge():
    client = gate(lambda r: OK)
    missing = client.get("/api/v1/sources")
    assert missing.status_code == 401 and missing.json()["code"] == "credential_required"
    assert missing.headers["content-type"] == "application/problem+json"
    assert (
        missing.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA}", scope="corpus:read"'
    )
    basic = client.get("/api/v1/sources", headers={"Authorization": "Basic abc"})
    assert (
        basic.json()["code"] == "invalid_credentials"
        and 'error="invalid_token"' in basic.headers["www-authenticate"]
    )
    in_address = client.get("/api/v1/sources?api_key=oa_live_x", headers={"Authorization": "Bearer x"})
    assert in_address.status_code == 400 and in_address.json()["code"] == "credential_not_accepted"
    for named in ("token=x", "Access_Token=x", "limit=1&KEY=x", "apikey=x", "id_token=x"):
        refused = client.get(f"/api/v1/changes?{named}", headers={"Authorization": "Bearer x"})
        assert refused.json()["code"] == "credential_not_accepted", named
    # The change feed's resume token is no credential: the request goes on to the credential check.
    resumed = client.get("/api/v1/changes?resume_token=r1.abc", headers={"Authorization": "Bearer x"})
    assert resumed.json()["code"] == "invalid_credentials"
    assert client.get("/api/v1/tiers").status_code == 200  # public


def test_keys_are_hashed_with_the_pepper_and_refusals_from_the_check_carry_no_challenge():
    seen = []
    secret = keys.generate().secret

    def check(asked):
        seen.append(asked)
        return {"ok": False, "code": "entitlement_lapsed"}

    refused = gate(check).get("/api/v1/sources", headers={"Authorization": f"Bearer {secret}"})
    assert refused.status_code == 403 and refused.json()["code"] == "entitlement_lapsed"
    assert "www-authenticate" not in refused.headers
    assert seen == [
        {"kind": "key", "key_id": keys.parse(secret), "secret_hash": keys.secret_hash(secret, PEPPER).hex()}
    ]
    malformed = gate(check).get("/api/v1/sources", headers={"Authorization": "Bearer oa_live_short"})
    assert malformed.json()["code"] == "invalid_credentials" and len(seen) == 1


def test_the_second_check_stops_a_response_when_access_changed_during_the_request():
    answers = iter([OK, {"ok": False, "code": "invalid_credentials"}])
    usage = []
    response = gate(lambda r: next(answers), usage=usage).get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {keys.generate().secret}"}
    )
    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert "org_" not in response.text
    assert usage == [
        {
            "organisation": OK["organisation"],
            "credential": OK["credential"],
            "class": "rest",
            "refusal": "invalid_credentials",
        }
    ]
    passed = gate(lambda r: OK, usage=usage).get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {keys.generate().secret}"}
    )
    assert passed.status_code == 200 and passed.headers["x-request-id"]
    assert usage[-1]["bytes"] == len(passed.content)


class Admission:
    """api.admission.AdmissionStore's two calls: admitted with what is left of each bucket, or refused."""

    def __init__(self, remaining=None):
        self.remaining, self.requests, self.released = remaining, [], []

    def admit(self, request):
        self.requests.append(request)
        if self.remaining is None:
            return {"admitted": False, "blocked": [request["buckets"][0]["key"]], "retry_after": 7}
        return {
            "admitted": True,
            "lease_id": "lease-1",
            "buckets": {b["key"]: {"remaining": self.remaining, "reset": 1} for b in request["buckets"]},
        }

    def release(self, lease):
        self.released.append(lease)


ALLOWANCE = {**OK, "tier": {**OK["tier"], "allowance": {"requests_per_minute": 600, "burst": 100}}}


def test_admitted_answers_state_the_organisations_request_allowance():
    """G13: as the website's data path does (api.admission), each answer counted against the organisation's
    allowance says what is left of it: the burst, its refill time, the requests left and the seconds until
    the burst is full again."""
    admission = Admission(remaining=87)
    client = gate(lambda r: ALLOWANCE, admission=admission)
    headers = {"Authorization": f"Bearer {keys.generate().secret}"}
    for path, status in (("/api/v1/sources", 200), ("/api/v1/unchanged", 304), ("/api/v1/missing", 404)):
        answer = client.get(path, headers=headers)
        assert answer.status_code == status, path
        assert answer.headers["ratelimit-policy"] == '"organisation";q=100;w=10', path
        assert answer.headers["ratelimit"] == '"organisation";r=87;t=1', path
        # A refusal from the route carries its own request ID: the gate never sends a second one.
        assert len(answer.headers.get_list("x-request-id")) == 1, path
    assert admission.requests[0]["buckets"] == [
        {"key": "org:00000000000000000000000000", "capacity": 100, "refill_per_minute": 600}
    ]
    assert admission.released == ["lease-1"] * 3
    refused = gate(lambda r: ALLOWANCE, admission=Admission()).get("/api/v1/sources", headers=headers)
    assert refused.status_code == 429 and refused.headers["retry-after"] == "7"
    assert "ratelimit" not in refused.headers and refused.headers["x-request-id"]
    unknown = gate(lambda r: ALLOWANCE, admission=Admission(remaining=87)).get("/api/v1/sources")
    assert unknown.status_code == 401 and "ratelimit" not in unknown.headers


def test_public_answers_need_no_credential_and_still_carry_a_request_id():
    public = gate(lambda r: OK, admission=Admission(remaining=1)).get("/api/v1/tiers")
    assert public.status_code == 200 and len(public.headers["x-request-id"]) == 16
    assert "ratelimit" not in public.headers  # nothing is counted


def test_a_tier_without_rest_is_refused():
    no_rest = {**OK, "tier": {**OK["tier"], "includes": ["mcp"]}}
    response = gate(lambda r: no_rest).get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {keys.generate().secret}"}
    )
    assert response.status_code == 403 and response.json()["code"] == "entitlement_required"


def with_bytes(per_day):
    return {**OK, "tier": {**OK["tier"], "allowance": {"requests_per_minute": 60, "bytes_per_day": per_day}}}


def test_each_answer_is_charged_to_the_organisations_data_a_day_and_refused_when_it_is_used_up():
    ledger, usage = Ledger(), []
    client = gate(lambda r: with_bytes(200), usage=usage, ledger=ledger)
    headers = {"Authorization": f"Bearer {keys.generate().secret}"}
    first = client.get("/api/v1/sources", headers=headers)
    size = len(first.content)
    assert first.status_code == 200 and 0 < size < 100
    reserve, begin, settle = ledger.steps
    assert reserve[0] == "reserve" and reserve[2] == size  # its Content-Length, before the first byte
    assert reserve[1].key == "org:00000000000000000000000000" and reserve[1].capacity == 200
    assert reserve[1].refill_per_minute == pytest.approx(200 / 1440)  # a day's bytes over 24 hours
    assert begin == ("begin", True) and settle == ("settle", size, f"sent:{size}")
    while (answer := client.get("/api/v1/sources", headers=headers)).status_code == 200:
        pass
    assert answer.status_code == 429 and answer.json()["code"] == "rate_limited"
    assert "data a day" in answer.json()["detail"] and int(answer.headers["retry-after"]) >= 1
    assert "org_" not in answer.text  # the answer that would have gone over was never sent
    assert ledger.spent["org:00000000000000000000000000"] <= 200 and not ledger.open
    assert usage[-1]["refusal"] == "rate_limited" and all("bytes" in u for u in usage[:-1])


def test_an_answer_without_a_body_costs_nothing_and_a_tier_without_a_byte_allowance_is_not_charged():
    ledger = Ledger()
    headers = {"Authorization": f"Bearer {keys.generate().secret}"}
    assert (
        gate(lambda r: with_bytes(200), ledger=ledger).get("/api/v1/unchanged", headers=headers).status_code
        == 304
    )
    assert gate(lambda r: OK, ledger=ledger).get("/api/v1/sources", headers=headers).status_code == 200
    assert ledger.steps == []


def test_a_byte_ledger_that_cannot_be_reached_refuses_rather_than_lets_the_answer_through():
    headers = {"Authorization": f"Bearer {keys.generate().secret}"}
    refused = gate(lambda r: with_bytes(200), ledger=Ledger(down=True)).get(
        "/api/v1/sources", headers=headers
    )
    assert refused.status_code == 503 and refused.json()["code"] == "temporarily_unavailable"
    assert "org_" not in refused.text


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _number(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _part(data: dict) -> str:
    return _b64(json.dumps(data).encode())


class Signer:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": _number(numbers.n), "e": _number(numbers.e)}]}

    def token(self, **claims):
        body = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "exp": time.time() + 60,
            "sub": "google-oauth2|1",
            "scope": "openid corpus:read",
            GRANT_CLAIM: "grt_00000000000000000000000000",
            **claims,
        }
        signing = f"{_part({'alg': 'RS256', 'kid': 'k1'})}.{_part(body)}"
        return signing + "." + _b64(self.key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))


@pytest.fixture
def signer():
    return Signer()


def verifier_for(signer):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=signer.jwks))
    return TokenVerifier(ISSUER, client=httpx.Client(transport=transport))


def test_access_tokens_are_verified_bound_to_the_resource_and_need_the_scope(signer):
    seen = []
    client = gate(lambda asked: (seen.append(asked), OK)[1], verifier=verifier_for(signer))
    good = client.get("/api/v1/sources", headers={"Authorization": f"Bearer {signer.token()}"})
    assert good.status_code == 200
    assert seen[0] == {
        "kind": "grant",
        "grant_id": "grt_00000000000000000000000000",
        "resource": "api",
        "issuer": ISSUER,
        "subject": "google-oauth2|1",
    }
    wrong_audience = client.get(
        "/api/v1/sources",
        headers={"Authorization": f"Bearer {signer.token(aud='https://api.example.test/mcp')}"},
    )
    assert wrong_audience.json()["code"] == "invalid_credentials"
    no_scope = client.get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {signer.token(scope='openid')}"}
    )
    assert no_scope.status_code == 403 and no_scope.json()["code"] == "insufficient_scope"
    assert no_scope.headers["www-authenticate"].startswith('Bearer error="insufficient_scope"')
    expired = client.get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {signer.token(exp=time.time() - 120)}"}
    )
    assert expired.json()["code"] == "invalid_credentials"
    other = Signer()
    forged = client.get("/api/v1/sources", headers={"Authorization": f"Bearer {other.token()}"})
    assert forged.json()["code"] == "invalid_credentials"
