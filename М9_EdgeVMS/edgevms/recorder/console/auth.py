"""Lesson 9, Step 3 — a login, marked temporary.

One account, provisioned by hand at commissioning, all capabilities. The
`grants` table exists and nothing consults it. This is the course's fourth
temporary secret; М12 Lesson 4 replaces it with a token from the domain
signer and removes the password hash from the recorder entirely.
"""
from __future__ import annotations

import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(pwhash: str, password: str) -> bool:
    try:
        return _ph.verify(pwhash, password)
    except (VerifyMismatchError, VerificationError):
        return False


class Sessions:
    """In-memory bearer tokens. Lost on restart, which is fine: an operator
    logs in again, and there is exactly one surface to protect."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._tokens: dict[str, tuple[int, float]] = {}

    def issue(self, operator_id: int) -> str:
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = (operator_id, time.monotonic() + self.ttl)
        return tok

    def operator_for(self, token: str | None) -> int | None:
        if not token or token not in self._tokens:
            return None
        oid, exp = self._tokens[token]
        if time.monotonic() > exp:
            del self._tokens[token]
            return None
        return oid
