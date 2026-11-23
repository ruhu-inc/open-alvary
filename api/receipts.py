"""ES256 receipt signing, RFC 8785 (JCS) canonicalization, claim digest, and offline verification."""

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, date, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

CHECKED_CLAIM_FIELDS = ("citation", "anchor_id", "quote", "as_of", "jurisdiction", "language")
DEFAULT_KID = "oa-receipt-2026-12"
DEFAULT_VALID_FROM = "2026-12-01T00:00:00Z"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid base64url string")
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _encode_jcs(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise ValueError("Receipts cannot contain floating-point numbers")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        items = []
        for key in sorted(value.keys(), key=lambda k: _require_str_key(k).encode("utf-16-be")):
            items.append(json.dumps(key, ensure_ascii=False) + ":" + _encode_jcs(value[key]))
        return "{" + ",".join(items) + "}"
    raise TypeError(f"Unsupported type for canonical JSON: {type(value).__name__}")


def _require_str_key(key) -> str:
    if not isinstance(key, str):
        raise TypeError(f"Canonical JSON object keys must be strings, got {type(key).__name__}")
    return key


def canonical_json(obj) -> bytes:
    """RFC 8785 (JCS) canonical UTF-8 JSON for receipts (rejects floats; UTF-16 key order; no whitespace)."""
    return _encode_jcs(obj).encode("utf-8")


def claim_digest(claim_fields: dict) -> str:
    """64-character lowercase hex SHA-256 of canonical_json of only the caller-sent checked fields.

    Omitted fields are absent (never null); ``reading`` and any non-checked fields are excluded.
    """
    filtered: dict[str, str] = {}
    for field in CHECKED_CLAIM_FIELDS:
        if field in claim_fields and claim_fields[field] is not None:
            val = claim_fields[field]
            if field == "as_of" and isinstance(val, date) and not isinstance(val, datetime):
                val = val.isoformat()
            filtered[field] = val
    return hashlib.sha256(canonical_json(filtered)).hexdigest()


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


class ReceiptSigner:
    """ECDSA P-256 (ES256) signer producing raw 64-byte r||s base64url unpadded signatures."""

    def __init__(
        self,
        private_pem: bytes,
        kid: str,
        valid_from: str,
        valid_to: str | None = None,
        extra_jwks: list[dict] | None = None,
    ):
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("Receipt signing key must be an ECDSA P-256 (SECP256R1) private key")
        self._private_key = key
        self.kid = kid
        self.valid_from = valid_from
        self.valid_to = valid_to
        self._extra_jwks = list(extra_jwks) if extra_jwks else []

    def sign(self, receipt_without_sig: dict) -> str:
        payload = {k: v for k, v in receipt_without_sig.items() if k != "signature"}
        message = canonical_json(payload)
        der_sig = self._private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return _b64url_encode(raw_sig)

    def public_jwk(self) -> dict:
        numbers = self._private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url_encode(numbers.x.to_bytes(32, "big")),
            "y": _b64url_encode(numbers.y.to_bytes(32, "big")),
            "use": "sig",
            "alg": "ES256",
            "kid": self.kid,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "revoked_at": None,
        }

    def jwks(self) -> dict:
        keys = [self.public_jwk()]
        for extra in self._extra_jwks:
            if isinstance(extra, dict) and extra.get("kid") != self.kid:
                keys.append(extra)
        return {"keys": keys}


def verify_receipt(
    receipt: dict,
    jwks: dict,
    *,
    claim: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Verify an oa-receipt-1 receipt against a JWKS document and optional claim.

    Always returns ``{"ok": bool, "status": str | None, "reason": str | None, "age_days": int | None}``.
    """
    if not isinstance(receipt, dict):
        return {"ok": False, "status": None, "reason": "invalid_receipt", "age_days": None}

    status = receipt.get("status") if isinstance(receipt.get("status"), str) else None
    issued_at = _parse_iso_dt(receipt.get("issued_at"))
    if issued_at is None:
        return {"ok": False, "status": status, "reason": "invalid_issued_at", "age_days": None}

    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    age_days = (now_dt - issued_at).days

    key_meta = receipt.get("key")
    if not isinstance(key_meta, dict):
        return {"ok": False, "status": status, "reason": "missing_key", "age_days": age_days}
    if key_meta.get("alg") != "ES256":
        return {"ok": False, "status": status, "reason": "unsupported_algorithm", "age_days": age_days}
    kid = key_meta.get("kid")
    if not isinstance(kid, str) or not kid:
        return {"ok": False, "status": status, "reason": "missing_kid", "age_days": age_days}

    keys = jwks.get("keys", []) if isinstance(jwks, dict) else []
    jwk = next((k for k in keys if isinstance(k, dict) and k.get("kid") == kid), None)
    if jwk is None:
        return {"ok": False, "status": status, "reason": "unknown_kid", "age_days": age_days}

    if jwk.get("revoked_at") is not None:
        return {"ok": False, "status": status, "reason": "key_revoked", "age_days": age_days}

    valid_from = _parse_iso_dt(jwk.get("valid_from"))
    if valid_from is None or issued_at < valid_from:
        return {"ok": False, "status": status, "reason": "issued_before_key_valid", "age_days": age_days}

    if jwk.get("valid_to") is not None:
        valid_to = _parse_iso_dt(jwk.get("valid_to"))
        if valid_to is None or issued_at > valid_to:
            return {"ok": False, "status": status, "reason": "issued_after_key_valid", "age_days": age_days}

    sig_b64 = receipt.get("signature")
    if not isinstance(sig_b64, str) or not sig_b64:
        return {"ok": False, "status": status, "reason": "missing_signature", "age_days": age_days}

    try:
        raw_sig = _b64url_decode(sig_b64)
        if len(raw_sig) != 64:
            return {"ok": False, "status": status, "reason": "invalid_signature", "age_days": age_days}
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            return {"ok": False, "status": status, "reason": "unsupported_key", "age_days": age_days}
        x_bytes = _b64url_decode(jwk["x"])
        y_bytes = _b64url_decode(jwk["y"])
        pub_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x_bytes, "big"),
            int.from_bytes(y_bytes, "big"),
            ec.SECP256R1(),
        ).public_key()

        payload = {k: v for k, v in receipt.items() if k != "signature"}
        message = canonical_json(payload)
        pub_key.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return {"ok": False, "status": status, "reason": "invalid_signature", "age_days": age_days}

    if claim is not None:
        receipt_claim = receipt.get("claim")
        recorded_digest = receipt_claim.get("digest") if isinstance(receipt_claim, dict) else None
        if not isinstance(recorded_digest, str):
            return {"ok": False, "status": status, "reason": "claim_digest_mismatch", "age_days": age_days}
        expected_digest = claim_digest(claim)
        if not hmac.compare_digest(recorded_digest, expected_digest):
            return {"ok": False, "status": status, "reason": "claim_digest_mismatch", "age_days": age_days}

    return {"ok": True, "status": status, "reason": None, "age_days": age_days}


def _parse_keys_json(raw: str) -> list[dict]:
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("keys"), list):
        return parsed["keys"]
    if isinstance(parsed, list):
        return parsed
    raise ValueError("ALVARY_RECEIPT_KEYS_JSON must be a JSON list or an object with a 'keys' list")


def load_signer_from_env() -> ReceiptSigner | None:
    """Load the ES256 ReceiptSigner from environment variables, or return None if unset."""
    pem_str = os.environ.get("ALVARY_RECEIPT_SIGNING_KEY", "").strip()
    if not pem_str:
        return None
    if "\\n" in pem_str and "\n" not in pem_str:
        pem_str = pem_str.replace("\\n", "\n")
    kid = os.environ.get("ALVARY_RECEIPT_KID", "").strip() or DEFAULT_KID
    valid_from = os.environ.get("ALVARY_RECEIPT_VALID_FROM", "").strip() or DEFAULT_VALID_FROM
    valid_to = os.environ.get("ALVARY_RECEIPT_VALID_TO", "").strip() or None
    keys_json = os.environ.get("ALVARY_RECEIPT_KEYS_JSON", "").strip()
    extra_jwks = _parse_keys_json(keys_json) if keys_json else None
    return ReceiptSigner(
        private_pem=pem_str.encode("utf-8"),
        kid=kid,
        valid_from=valid_from,
        valid_to=valid_to,
        extra_jwks=extra_jwks,
    )


def load_jwks_from_env() -> dict:
    """Return JWKS from ALVARY_RECEIPT_KEYS_JSON if set, else from the configured signer, else empty."""
    keys_json = os.environ.get("ALVARY_RECEIPT_KEYS_JSON", "").strip()
    if keys_json:
        return {"keys": _parse_keys_json(keys_json)}
    signer = load_signer_from_env()
    if signer is not None:
        return signer.jwks()
    return {"keys": []}
