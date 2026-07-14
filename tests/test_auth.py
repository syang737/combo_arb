"""Verify our RSA-PSS signing is actually valid (round-trip with the public key)."""

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from combo_arb.kalshi.auth import auth_headers, sign


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies_with_public_key():
    key = _key()
    message = "1700000000000GET/trade-api/v2/markets"
    sig_b64 = sign(key, message)
    signature = base64.b64decode(sig_b64)
    # Does not raise -> signature is valid for this message.
    key.public_key().verify(
        signature,
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_auth_headers_shape_and_message():
    key = _key()
    headers = auth_headers("my-key-id", key, "get", "/trade-api/v2/markets",
                           timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    # The signature must verify against "{ts}{METHOD}{path}" with uppercased method.
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        signature,
        b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
