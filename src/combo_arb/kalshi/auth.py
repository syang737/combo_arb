"""Kalshi RSA-PSS request signing.

For every authenticated request Kalshi requires three headers:
    KALSHI-ACCESS-KEY        -> the API key id
    KALSHI-ACCESS-TIMESTAMP  -> unix time in milliseconds
    KALSHI-ACCESS-SIGNATURE  -> base64( RSA-PSS-SHA256( "{ts}{METHOD}{path}" ) )

The signed ``path`` is the URL path only (e.g. ``/trade-api/v2/markets``), with
no host and no query string.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    with open(path, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API key must be an RSA private key (PKCS#8 PEM).")
    return key


def sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    """RSA-PSS (SHA-256, digest-length salt) signature, base64-encoded."""
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(
    key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
    timestamp_ms: Optional[int] = None,
) -> dict[str, str]:
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    message = f"{ts}{method.upper()}{path}"
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, message),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }
