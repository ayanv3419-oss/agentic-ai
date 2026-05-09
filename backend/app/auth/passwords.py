"""bcrypt password hashing + verification.

Uses bcrypt's adaptive cost factor (default 12 rounds → ~250ms/hash on
commodity CPU). The cost is tuned for password verification at login time;
registrations are rare and can take longer.

Why bcrypt over argon2:
  - Battle-tested, widely deployed
  - In CPython without compiling cffi extensions
  - Built-in salt
  - 60-byte hashes fit comfortably in a TEXT column

Argon2 is the modern preference for greenfield systems; bcrypt is fine for
current load. To swap: install `argon2-cffi` and replace the two calls.
"""
from __future__ import annotations

import bcrypt


_BCRYPT_ROUNDS = 12


def hash_password(plaintext: str) -> str:
    """Generate a salted bcrypt hash. Returns a UTF-8 string for storage
    in a TEXT column."""
    if not plaintext or not isinstance(plaintext, str):
        raise ValueError("password must be a non-empty string")
    if len(plaintext) > 1024:
        raise ValueError("password too long (>1024 chars)")
    # bcrypt's API takes bytes — we encode + decode at the boundary.
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(plaintext.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time verify. Returns False (never raises) on any input
    that bcrypt rejects — malformed hash, empty password, wrong type —
    so timing-attack and DoS-by-malformed-input vectors are both closed."""
    if not plaintext or not hashed:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
