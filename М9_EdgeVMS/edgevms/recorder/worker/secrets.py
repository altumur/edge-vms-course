"""Lesson 5 — the credential hiding in rtsp_url.

The column is encrypted with a key on the data partition. This module ships
the weaker version and says so: the key travels with the database in every
backup, so it is the same factor in a different file. The improvement — a
TPM-sealed key, or a key delivered at runtime by whatever starts the recorder —
is named as a debt in Lesson 5 and paid in М11/М12.

What this module MUST do is keep the credential out of every URL string
that reaches a log, a pipeline description or an error message.
"""
from __future__ import annotations

import os
import secrets as _secrets
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12


class ColumnKey:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("column key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def load(cls, path: str) -> "ColumnKey":
        with open(path, "rb") as f:
            return cls(f.read())

    @classmethod
    def generate(cls, path: str) -> "ColumnKey":
        key = _secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return cls(key)

    def encrypt(self, secret: str, camera_id: int) -> bytes:
        nonce = _secrets.token_bytes(_NONCE)
        # camera_id as associated data: a ciphertext copied between rows fails.
        return nonce + self._aead.encrypt(nonce, secret.encode(), str(camera_id).encode())

    def decrypt(self, blob: bytes, camera_id: int) -> str:
        return self._aead.decrypt(blob[:_NONCE], blob[_NONCE:], str(camera_id).encode()).decode()


def compose_rtsp_url(cam: dict, key: ColumnKey | None) -> str:
    """Build the URL GStreamer needs, credential inline, IN MEMORY ONLY.

    Log cam["rtsp_url"], never the return value of this function: the
    composed URL is about to be interpolated into a pipeline description that
    GStreamer will happily print in an error message.
    """
    url = cam["rtsp_url"]
    if not cam.get("cred_username") or not cam.get("cred_secret") or key is None:
        return url
    parts = urlsplit(url)
    secret = key.decrypt(cam["cred_secret"], cam["id"])
    netloc = f"{quote(cam['cred_username'], safe='')}:{quote(secret, safe='')}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact(text: str) -> str:
    """Last line of defence for anything that might carry a composed URL —
    a bus error, an exception message. Strips user:pass@ from every URL."""
    import re
    return re.sub(r"(rtsps?://)[^/@\s]+@", r"\1***@", text)
