"""Workbench passkeys (MAC-11): registration and assertions verify only from the right site, with the
person verified, a fresh challenge, the registered key and a counter that moves forward."""

import hashlib
import json
import os
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from workbench.passkeys import EDDSA, ES256, RS256, PasskeyError, RelyingParty, b64url

ORIGIN = "https://review.example.org"


class SoftKey:
    """A software authenticator: what a browser and a passkey hand the server."""

    def __init__(self, origin=ORIGIN, algorithm=ES256, flags=0x05):
        self.origin, self.algorithm, self.flags = origin, algorithm, flags
        self.rp_id = urlsplit(origin).hostname
        self.private = {
            ES256: lambda: ec.generate_private_key(ec.SECP256R1()),
            EDDSA: ed25519.Ed25519PrivateKey.generate,
            RS256: lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        }[algorithm]()
        self.raw_id = os.urandom(16)
        self.counter = 0

    def _authenticator_data(self, attested: bool) -> bytes:
        head = hashlib.sha256(self.rp_id.encode()).digest() + bytes([self.flags | (0x40 if attested else 0)])
        head += self.counter.to_bytes(4, "big")
        if attested:
            head += bytes(16) + len(self.raw_id).to_bytes(2, "big") + self.raw_id + b"\xa0"
        return head

    def _client(self, kind, challenge, **more) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, **more}).encode()

    def create(self, options: dict) -> dict:
        spki = self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return {
            "id": b64url(self.raw_id),
            "rawId": b64url(self.raw_id),
            "clientDataJSON": b64url(self._client("webauthn.create", options["challenge"])),
            "authenticatorData": b64url(self._authenticator_data(True)),
            "publicKey": b64url(spki),
            "publicKeyAlgorithm": self.algorithm,
        }

    def get(self, options: dict, **client) -> dict:
        self.counter += 1
        data = self._authenticator_data(False)
        client_data = self._client("webauthn.get", options["challenge"], **client)
        signed = data + hashlib.sha256(client_data).digest()
        if self.algorithm == ES256:
            signature = self.private.sign(signed, ec.ECDSA(hashes.SHA256()))
        elif self.algorithm == EDDSA:
            signature = self.private.sign(signed)
        else:
            signature = self.private.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return {
            "id": b64url(self.raw_id),
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(data),
            "signature": b64url(signature),
        }


RP = RelyingParty(ORIGIN)


def enrolled(key, challenge="c1"):
    return RP.register(key.create({"challenge": challenge}), challenge)


@pytest.mark.parametrize("algorithm", [ES256, EDDSA, RS256])
def test_each_supported_kind_of_passkey_registers_and_verifies(algorithm):
    key = SoftKey(algorithm=algorithm)
    stored = enrolled(key)
    assert stored["credential_id"] == b64url(key.raw_id) and stored["algorithm"] == algorithm
    assert RP.verify(key.get({"challenge": "c2"}), "c2", stored) == 1


def test_another_site_challenge_or_step_is_refused():
    key = SoftKey()
    stored = enrolled(key)
    with pytest.raises(PasskeyError, match="different or expired"):
        RP.verify(key.get({"challenge": "old"}), "new", stored)
    with pytest.raises(PasskeyError, match="another site"):
        RP.verify(SoftKey(origin="https://evil.example.org").get({"challenge": "c"}), "c", stored)
    with pytest.raises(PasskeyError, match="another site"):
        RP.verify(key.get({"challenge": "c"}, crossOrigin=True), "c", stored)
    with pytest.raises(PasskeyError, match="not the passkey step"):
        RP.register(key.get({"challenge": "c"}) | {"rawId": b64url(key.raw_id)}, "c")
    # The same origin string but another relying party ID in the signed data.
    wrong = SoftKey()
    wrong.rp_id = "evil.example.org"
    with pytest.raises(PasskeyError, match="belongs to another site"):
        RP.register(wrong.create({"challenge": "c"}), "c")


def test_the_person_must_be_verified_and_the_signature_must_be_the_registered_keys():
    with pytest.raises(PasskeyError, match="check it's you"):
        enrolled(SoftKey(flags=0x01))  # present but not verified
    key, other = SoftKey(), SoftKey()
    stored = enrolled(key)
    forged = other.get({"challenge": "c"}) | {"id": b64url(key.raw_id)}
    with pytest.raises(PasskeyError, match="signature does not verify"):
        RP.verify(forged, "c", stored)


def test_a_counter_that_does_not_move_forward_is_refused():
    key = SoftKey()
    stored = enrolled(key)
    stored["sign_count"] = RP.verify(key.get({"challenge": "a"}), "a", stored)
    key.counter = 0  # a copy of the key, behind the original
    with pytest.raises(PasskeyError, match="went backwards"):
        RP.verify(key.get({"challenge": "b"}), "b", stored)


def test_a_key_that_does_not_match_its_algorithm_or_its_id_is_refused():
    key = SoftKey()
    response = key.create({"challenge": "c"})
    with pytest.raises(PasskeyError, match="does not match its algorithm"):
        RP.register(response | {"publicKeyAlgorithm": EDDSA}, "c")
    with pytest.raises(PasskeyError, match="not supported"):
        RP.register(response | {"publicKeyAlgorithm": -35}, "c")
    with pytest.raises(PasskeyError, match="ID does not match"):
        RP.register(response | {"rawId": b64url(b"another")}, "c")
    small = SoftKey(algorithm=RS256)
    small.private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(PasskeyError, match="does not match its algorithm"):
        RP.register(small.create({"challenge": "c"}), "c")
