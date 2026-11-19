"""Kalshi API key request signing (RSA-PSS, SHA-256)."""
from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: str):
        self.key_id = key_id
        pem = private_key_pem.strip().replace("\\n", "\n").encode("utf-8")
        self._key = serialization.load_pem_private_key(pem, password=None)

    def sign(self, method: str, path: str, ts_ms: int | None = None) -> dict[str, str]:
        """Return the three auth headers for METHOD + path (path without host or query)."""
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        payload = f"{ts}{method.upper()}{path.split('?')[0]}".encode("utf-8")
        sig = self._key.sign(
            payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
