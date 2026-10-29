"""AES-GCM encryption for refresh tokens at rest."""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TokenCipher:
    def __init__(self, key_b64: str) -> None:
        key = base64.b64decode(key_b64) if key_b64 else b""
        if len(key) != 32:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be 32 bytes, base64 encoded")
        self._aes = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        return "v1:" + base64.b64encode(nonce + self._aes.encrypt(nonce, plaintext.encode(), b"refresh_token")).decode()

    def decrypt(self, token: str) -> str:
        if not token.startswith("v1:"):
            raise ValueError("Unknown ciphertext format")
        raw = base64.b64decode(token[3:])
        return self._aes.decrypt(raw[:12], raw[12:], b"refresh_token").decode()


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()
