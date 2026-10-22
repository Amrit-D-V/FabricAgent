from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

from config import PROJECT_ROOT

_KEY_FILE: Path = PROJECT_ROOT / ".app_secret_key"
_fernet: Fernet | None = None


def _derive_fernet_key(raw: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


def _load_key() -> bytes:
    env = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if env:
        return _derive_fernet_key(env)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, ttl: int | None = None) -> str:
    return _get_fernet().decrypt(token.encode("ascii"), ttl=ttl).decode("utf-8")
