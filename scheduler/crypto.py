"""AES-256-GCM envelope encryption for platform access tokens.

The database stores only version || nonce || authenticated ciphertext. The
32-byte master key is supplied by the deployment secret manager and is never
stored in PostgreSQL. Phase 2 may replace this local envelope with cloud KMS.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC=b"CLP1"
_NONCE_BYTES=12


def _key():
    key_file=os.environ.get("TOKEN_ENCRYPTION_KEY_FILE","").strip()
    raw=os.environ.get("TOKEN_ENCRYPTION_KEY","").strip()
    if key_file:
        from pathlib import Path
        raw=Path(key_file).read_text(encoding="utf-8").strip()
    if not raw: raise RuntimeError("TOKEN_ENCRYPTION_KEY or TOKEN_ENCRYPTION_KEY_FILE is required")
    try:
        key=base64.urlsafe_b64decode(raw + "="*((4-len(raw)%4)%4))
    except Exception as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be base64url for 32 random bytes") from exc
    if len(key)!=32: raise RuntimeError("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def encrypt_token(plaintext: str) -> bytes:
    if not isinstance(plaintext,str) or not plaintext:
        raise ValueError("token plaintext must be a non-empty string")
    nonce=os.urandom(_NONCE_BYTES)
    encrypted=AESGCM(_key()).encrypt(nonce,plaintext.encode("utf-8"),_MAGIC)
    return _MAGIC+nonce+encrypted


def decrypt_token(ciphertext: bytes | memoryview) -> str:
    raw=bytes(ciphertext)
    if len(raw)<len(_MAGIC)+_NONCE_BYTES+16 or not raw.startswith(_MAGIC):
        raise ValueError("unsupported or malformed Clipper token ciphertext")
    nonce=raw[len(_MAGIC):len(_MAGIC)+_NONCE_BYTES]
    body=raw[len(_MAGIC)+_NONCE_BYTES:]
    try:
        return AESGCM(_key()).decrypt(nonce,body,_MAGIC).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Token decryption failed; check key version/rotation") from exc
