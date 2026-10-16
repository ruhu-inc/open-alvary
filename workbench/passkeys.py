"""Passkeys for the Access section (MAC-11): WebAuthn checked by the workbench itself, whatever the
operator's Google account policy.

Registration keeps the public key the browser hands over in SPKI form
(AuthenticatorAttestationResponse.getPublicKey()), with attestation "none": no attestation format is
parsed, and none would prove more. A first passkey also needs a one-time enrolment code issued on the
server (workbench.admin enrol), so a taken-over Google account cannot add one.

Every ceremony checks the session's single-use challenge, the origin, the relying party's ID hash,
and that the person was present and verified (PIN or biometric). An assertion's signature is checked
over authenticatorData || SHA-256(clientDataJSON) with the stored key; a signature counter that does
not move forward refuses the passkey (a possible clone).
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

ES256, EDDSA, RS256 = -7, -8, -257
ALGORITHMS = (ES256, EDDSA, RS256)
PRESENT, VERIFIED, CREDENTIAL = 0x01, 0x04, 0x40


class PasskeyError(Exception):
    """A ceremony that does not verify; the message is safe to show."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as error:
        raise PasskeyError("Malformed passkey response.") from error


@dataclass(frozen=True)
class RelyingParty:
    origin: str  # https://review.example.org, exactly as the browser reports it
    name: str = "Open Alvary review workbench"

    @property
    def id(self) -> str:
        return urlsplit(self.origin).hostname or ""

    def creation_options(self, challenge: str, email: str, user_handle: str, existing: list[str]) -> dict:
        return {
            "challenge": challenge,
            "rp": {"id": self.id, "name": self.name},
            "user": {"id": user_handle, "name": email, "displayName": email},
            "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in ALGORITHMS],
            "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"},
            "attestation": "none",
            "excludeCredentials": [{"type": "public-key", "id": c} for c in existing],
            "timeout": 300000,
        }

    def request_options(self, challenge: str, credentials: list[str]) -> dict:
        return {
            "challenge": challenge,
            "rpId": self.id,
            "allowCredentials": [{"type": "public-key", "id": c} for c in credentials],
            "userVerification": "required",
            "timeout": 300000,
        }

    def _client_data(self, raw: bytes, kind: str, challenge: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise PasskeyError("Malformed passkey response.") from error
        if not isinstance(data, dict) or data.get("type") != kind:
            raise PasskeyError("This is not the passkey step that was asked for.")
        if not hmac.compare_digest(str(data.get("challenge", "")), challenge):
            raise PasskeyError("The passkey answered a different or expired request. Try again.")
        if data.get("origin") != self.origin or data.get("crossOrigin") is True:
            raise PasskeyError("The passkey was used from another site.")

    def _authenticator_data(self, raw: bytes, *, attested: bool) -> tuple[int, bytes]:
        if len(raw) < 37 or not hmac.compare_digest(raw[:32], hashlib.sha256(self.id.encode()).digest()):
            raise PasskeyError("The passkey belongs to another site.")
        flags = raw[32]
        if not flags & PRESENT or not flags & VERIFIED:
            raise PasskeyError("The passkey must check it's you (PIN, fingerprint or face).")
        if attested and not flags & CREDENTIAL:
            raise PasskeyError("The passkey did not return its credential.")
        return int.from_bytes(raw[33:37], "big"), raw[37:]

    def register(self, response: dict, challenge: str) -> dict:
        """A new passkey: {credential_id, public_key, algorithm, sign_count}."""
        client_data = unb64url(str(response.get("clientDataJSON", "")))
        authenticator = unb64url(str(response.get("authenticatorData", "")))
        raw_id = unb64url(str(response.get("rawId", "")))
        self._client_data(client_data, "webauthn.create", challenge)
        counter, rest = self._authenticator_data(authenticator, attested=True)
        if len(rest) < 18:
            raise PasskeyError("Malformed passkey response.")
        length = int.from_bytes(rest[16:18], "big")
        if not raw_id or rest[18 : 18 + length] != raw_id:
            raise PasskeyError("The passkey's ID does not match what it signed.")
        algorithm = response.get("publicKeyAlgorithm")
        public_key = response.get("publicKey")
        if algorithm not in ALGORITHMS or not public_key:
            raise PasskeyError("This kind of passkey is not supported here; use another.")
        der = unb64url(str(public_key))
        _load(der, algorithm)
        return {
            "credential_id": b64url(raw_id),
            "public_key": b64url(der),
            "algorithm": algorithm,
            "sign_count": counter,
        }

    def verify(self, response: dict, challenge: str, stored: dict) -> int:
        """Check an assertion against the stored passkey; the new signature counter."""
        client_data = unb64url(str(response.get("clientDataJSON", "")))
        authenticator = unb64url(str(response.get("authenticatorData", "")))
        signature = unb64url(str(response.get("signature", "")))
        self._client_data(client_data, "webauthn.get", challenge)
        counter, _ = self._authenticator_data(authenticator, attested=False)
        key = _load(unb64url(stored["public_key"]), stored["algorithm"])
        signed = authenticator + hashlib.sha256(client_data).digest()
        try:
            if stored["algorithm"] == ES256:
                key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
            elif stored["algorithm"] == EDDSA:
                key.verify(signature, signed)
            else:
                key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as error:
            raise PasskeyError("The passkey's signature does not verify.") from error
        if (counter or stored["sign_count"]) and counter <= stored["sign_count"]:
            raise PasskeyError("This passkey's counter went backwards; it may have been copied. Use another.")
        return counter


def _load(der: bytes, algorithm: int):
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm) as error:
        raise PasskeyError("The passkey's public key could not be read.") from error
    fits = {
        ES256: isinstance(key, ec.EllipticCurvePublicKey)
        and isinstance(getattr(key, "curve", None), ec.SECP256R1),
        EDDSA: isinstance(key, ed25519.Ed25519PublicKey),
        RS256: isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048,
    }
    if not fits.get(algorithm):
        raise PasskeyError("The passkey's key does not match its algorithm.")
    return key
