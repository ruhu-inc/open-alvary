"""API keys (ACS-03, MAC-05): `oa_live_<key_id>_<secret>`, parsed by position, stored as a keyed hash.

The key ID (32 lowercase hex) is public: the console and audit log show it as `key_<key_id>`. The
secret (43 base64url characters, 256 bits) is shown once. The database keeps HMAC-SHA256 of the
whole key under a pepper held outside the database (Secret Manager), so a copy of the database alone
cannot be used to test guesses.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass

PREFIX = "oa_live_"
KEY = re.compile(r"^oa_live_([0-9a-f]{32})_([A-Za-z0-9_-]{43})$")
DEFAULT_DAYS = 90
MAX_DAYS = 365


@dataclass(frozen=True)
class NewKey:
    key_id: str
    secret: str  # the whole key, shown once

    @property
    def public_id(self) -> str:
        return "key_" + self.key_id

    @property
    def last_four(self) -> str:
        return self.secret[-4:]


def generate() -> NewKey:
    key_id = secrets.token_hex(16)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return NewKey(key_id, f"{PREFIX}{key_id}_{secret}")


def parse(value: str) -> str | None:
    """The key ID of a well-formed key, or None."""
    match = KEY.match(value or "")
    return match.group(1) if match else None


def pepper() -> bytes:
    raw = os.environ.get("ALVARY_KEY_PEPPER", "")
    try:
        value = base64.b64decode(raw, validate=True)
    except ValueError:
        value = b""
    if len(value) < 32:
        raise RuntimeError("ALVARY_KEY_PEPPER must hold at least 32 random bytes, base64-encoded")
    return value


def secret_hash(key: str, secret_pepper: bytes | None = None) -> bytes:
    return hmac.new(secret_pepper or pepper(), key.encode(), hashlib.sha256).digest()
