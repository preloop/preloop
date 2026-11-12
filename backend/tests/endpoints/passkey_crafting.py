"""Craft real, signed WebAuthn ceremony responses for tests.

No authenticator hardware runs in CI, so these helpers play the authenticator:
a P-256 key signs genuine authenticator data whose flags byte the test
chooses. The server verifies them with the real ``py_webauthn`` code, so a
test can show exactly what happens when the user-verification (UV) flag is
present or absent.
"""

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

RP_ID = "localhost"
ORIGIN = "http://localhost"

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40


def b64url(data: bytes) -> str:
    """Unpadded base64url, as WebAuthn JSON uses."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@dataclass
class Authenticator:
    """A software authenticator holding one P-256 credential."""

    credential_id: bytes = field(default_factory=lambda: os.urandom(16))
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    sign_count: int = 0

    def cose_public_key(self) -> bytes:
        """The credential public key in COSE_Key form (EC2, ES256)."""
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        rp_hash = hashlib.sha256(RP_ID.encode()).digest()
        return rp_hash + bytes([flags]) + struct.pack(">I", self.sign_count) + attested

    def register(self, challenge: bytes, *, user_verified: bool) -> dict:
        """Return a ``navigator.credentials.create`` result ("none" attestation)."""
        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": b64url(challenge),
                "origin": ORIGIN,
                "crossOrigin": False,
            }
        ).encode()
        attested = (
            bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.cose_public_key()
        )
        flags = FLAG_UP | FLAG_AT | (FLAG_UV if user_verified else 0)
        attestation = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, attested)}
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
        }

    def assert_(self, challenge: bytes, *, user_verified: bool) -> dict:
        """Return a ``navigator.credentials.get`` result signed by this key."""
        self.sign_count += 1
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": b64url(challenge),
                "origin": ORIGIN,
                "crossOrigin": False,
            }
        ).encode()
        auth_data = self._auth_data(FLAG_UP | (FLAG_UV if user_verified else 0))
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
            },
        }
