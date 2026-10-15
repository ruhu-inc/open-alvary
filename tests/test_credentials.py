"""The credential gate (MAC-05–07) without a database: challenges, the second check, tokens."""

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from accounts import keys
from api.credentials import GRANT_CLAIM, CredentialGate, GateConfig, TokenVerifier

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

    return inner


def gate(check, verifier=None, usage=None):
    parent = FastAPI()
    parent.mount(
        "/api/v1",
        CredentialGate(
            app(),
            config=GateConfig(RESOURCE, "rest", "api", METADATA, public_paths=frozenset({"/tiers"})),
            check=check,
            record_usage=(usage.append if usage is not None else lambda e: None),
            admission=None,
            verifier=verifier,
            key_pepper=PEPPER,
        ),
    )
    return TestClient(parent)


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


def test_a_tier_without_rest_is_refused():
    no_rest = {**OK, "tier": {**OK["tier"], "includes": ["mcp"]}}
    response = gate(lambda r: no_rest).get(
        "/api/v1/sources", headers={"Authorization": f"Bearer {keys.generate().secret}"}
    )
    assert response.status_code == 403 and response.json()["code"] == "entitlement_required"


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
