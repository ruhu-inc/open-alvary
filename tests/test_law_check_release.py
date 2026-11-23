"""What Law check needs to be released: the plan's record list agrees with the list the page shows, and the
release script writes a limits file the service can load."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.admission import AdmissionConfig
from api.law_check import MAX_ANCHOR_TEXT_BYTES
from api.receipts import MAX_FUTURE_SKEW, ReceiptSigner, verify_receipt
from scripts.prepare_law_check_release import compute_limits, generate_artefacts

ROOT = Path(__file__).resolve().parent.parent


def _law_check_plan() -> dict:
    [plan] = [t for t in json.loads((ROOT / "accounts/tiers.json").read_text()) if t["id"] == "law-check"]
    return plan


def test_the_plans_record_list_is_the_list_the_page_shows():
    """The plan row comes from accounts/tiers.json (scripts.seed) and nothing else fills its list: with
    `[]` every check answered `unavailable` while the page listed three records as covered."""
    listed = _law_check_plan()["allowance"]["source_ids"]
    shown = [
        r["source_id"] for r in json.loads((ROOT / "web/public/agents/records.json").read_text())["records"]
    ]
    assert listed and len(set(listed)) == len(listed)
    assert sorted(listed) == sorted(shown)
    # Each is a record of the catalogue, under that id.
    held = {s["id"] for s in json.loads((ROOT / "ingestion/catalogue/ng.json").read_text())["sources"]}
    assert set(listed) <= held


def test_the_plan_sets_what_the_server_part_relies_on():
    plan = _law_check_plan()
    assert plan["includes"] == ["mcp"] and plan["published"] is False
    assert plan["allowance"]["withdrawals"] == "not_told"
    # The keyed MCP refuses a larger part before writing it, so only the refusal is charged.
    assert plan["allowance"]["anchor_text_bytes"] == MAX_ANCHOR_TEXT_BYTES


def test_the_release_script_writes_a_limits_file_the_service_loads(tmp_path):
    """The script wrote `database_budget` and no hmac_secret, trusted_proxies or lease: AdmissionConfig.load
    raised, and api.service loads the file at start."""
    result = generate_artefacts(
        tmp_path,
        public_pool_size=18,
        api_replicas=1,
        kid="oa-receipt-test",
        trusted_proxies=2,
        operator_reserve=5,
        other_workers=5,
    )
    config = AdmissionConfig.load(Path(result["admission_path"]))
    assert config.trusted_proxies == 2 and config.public_pool_size == 18 and config.api_replicas == 1
    assert config.global_concurrency == result["G"] == 6 and config.identity_concurrency == 4
    assert config.capacity == 40 and config.refill_per_minute == 20
    assert len(config.hmac_secret) == 32
    # A second run draws its own secret.
    again = generate_artefacts(
        tmp_path / "again",
        public_pool_size=18,
        api_replicas=1,
        kid="oa-receipt-test",
        trusted_proxies=2,
        operator_reserve=5,
        other_workers=5,
    )
    assert AdmissionConfig.load(Path(again["admission_path"])).hmac_secret != config.hmac_secret
    assert (Path(result["admission_path"]).stat().st_mode & 0o777) == 0o600

    # A pool the database budget cannot hold is refused when the file is made, not when the service starts.
    with pytest.raises(ValueError, match="database budget"):
        generate_artefacts(
            tmp_path / "over",
            public_pool_size=45,
            api_replicas=1,
            kid="oa-receipt-test",
            trusted_proxies=2,
            operator_reserve=5,
            other_workers=5,
        )
    with pytest.raises(ValueError):
        compute_limits(12, 1, trusted_proxies=2, operator_reserve=5, other_workers=5)  # p < 15


def _signed(signer: ReceiptSigner, issued_at: datetime) -> dict:
    body = {
        "schema_version": "oa-receipt-1",
        "receipt_id": "rcp_01TEST00000000000000000000",
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "key": {"alg": "ES256", "kid": signer.kid},
        "status": "not_in_catalogue",
    }
    return {**body, "signature": signer.sign(body)}


def test_python_reads_a_key_list_as_strictly_as_the_browser(tmp_path):
    """L8: both verifiers refuse a key with no valid_from, treat any revoked_at but null as withdrawn, and
    refuse a receipt dated more than a few minutes ahead (clients/receipt/receipt.test.mjs, same cases)."""
    result = generate_artefacts(
        tmp_path,
        public_pool_size=18,
        api_replicas=1,
        kid="k1",
        trusted_proxies=2,
        operator_reserve=5,
        other_workers=5,
    )
    signer = ReceiptSigner(Path(result["key_path"]).read_bytes(), kid="k1", valid_from="2026-01-01T00:00:00Z")
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    receipt = _signed(signer, now - timedelta(hours=1))
    jwk = signer.public_jwk()

    def reason(key: dict, at: datetime = now, signed: dict = receipt):
        return verify_receipt(signed, {"keys": [key]}, now=at)["reason"]

    assert reason(jwk) is None
    assert reason({k: v for k, v in jwk.items() if k != "valid_from"}) == "issued_before_key_valid"
    assert reason({**jwk, "valid_from": ""}) == "issued_before_key_valid"
    assert reason({**jwk, "revoked_at": ""}) == "key_revoked"
    assert reason({**jwk, "valid_to": ""}) == "issued_after_key_valid"
    assert reason({k: v for k, v in jwk.items() if k != "revoked_at"}) is None

    ahead = _signed(signer, now + MAX_FUTURE_SKEW + timedelta(seconds=1))
    assert reason(jwk, signed=ahead) == "issued_in_future"
    assert reason(jwk, signed=_signed(signer, now + MAX_FUTURE_SKEW - timedelta(seconds=1))) is None


def test_python_agrees_with_the_browser_verifiers_vectors():
    """clients/receipt/vectors.json is what the browser verifier is tested against: the same receipts,
    keys and claims give the same answer here."""
    vectors = json.loads((ROOT / "clients/receipt/vectors.json").read_text())
    now = datetime.fromisoformat(vectors["now"].replace("Z", "+00:00"))
    assert len(vectors["cases"]) >= 6
    for case in vectors["cases"]:
        answer = verify_receipt(case["receipt"], vectors["jwks"], claim=case.get("claim"), now=now)
        assert answer["ok"] is case["expected"]["ok"], case["name"]
        if "ageDays" in case["expected"]:
            assert answer["age_days"] == case["expected"]["ageDays"], case["name"]
