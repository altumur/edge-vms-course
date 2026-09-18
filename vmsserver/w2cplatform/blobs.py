"""A field too big for a row: the bytes go in the object store, the row keeps a digest.

Some units carry one opaque lump the platform does not interpret: a detector's
mask, a panel's firmware, a model. Three things about it are all true at once —
it belongs to exactly one unit, the platform will never read inside it, and it
does not fit in a row.

The tempting fix is to put such configuration in the object store and be done.
It costs more than it looks. A row in Variables has CAS, one writer, and a
`revision` that the reconciler watches: change a field and the unit restarts
with the new value. An object has none of that. Move the configuration there
whole and the revision never moves, so nothing restarts, and you are back to
building a mechanism the platform already had. Lesson 19 says the same thing
about `cred_secret` in one sentence: a separate store buys isolation on the
read and sells rotation back.

So: the bytes go in the object store, and the ROW keeps a reference. The
reference is a DIGEST rather than a name, and every useful property follows
from that one choice.

- Different bytes are a different digest, so they are a different row, so
  `revision` moves and the reconciler restarts the unit. Rotation is the
  mechanism that was already there.
- A digest key is written once and never overwritten, so the object store's
  last-writer-wins stops being something to think about.
- A worker can cache by digest forever and be right.
- The same lump on a hundred units is stored once.

The order of writes is the object FIRST, then the row that names it (М12's
identity store publishes the same way, for the same reason). A crash between
the two leaves an object nobody points at — harmless, and collectable. The
other order leaves a row pointing at nothing, which is a unit that cannot
start.

The honest residue: nothing in the platform deletes an object, so unreferenced
blobs accumulate and a sweep does not exist yet. It is written down rather than
implied.

The spelling is `sha256-<hex>` — a dash, not a colon, because this string is
also a key, and a key becomes a path on a filesystem, and a colon is not a path
character everywhere (Lesson 24). One spelling, everywhere.
"""
from __future__ import annotations

import hashlib
import re

BLOBS = "blobs"
ALGO = "sha256"
_DIGEST = re.compile(r"^sha256-[0-9a-f]{64}$")


def digest(data: bytes) -> str:
    """The name these bytes have, and the only name they have."""
    return f"{ALGO}-{hashlib.sha256(data).hexdigest()}"


def is_digest(value: str) -> bool:
    return bool(_DIGEST.match(str(value or "")))


class BlobMismatch(Exception):
    """The bytes stored under a digest do not hash to it.

    Content addressing is a PROMISE, and a promise nobody checks is a comment.
    The digest in the row makes the key immutable *by convention*; this makes it
    immutable in a way anyone can verify — and verification is what an ACL cannot
    give, because it is not a claim about who wrote the bytes. It holds against a
    writer with the wrong token, a store shared more widely than intended, an
    object copied between stores, and a disk that rotted.

    So the check belongs on the READ, where the bytes are about to be used, and
    not only on the write, where it would only be trusting the writer again."""


def verify(d: str, data: bytes) -> bytes:
    """`data` if it hashes to `d`; otherwise `BlobMismatch`. Never a silent pass."""
    actual = digest(data)
    if actual != d:
        raise BlobMismatch(f"{d}: the bytes stored there hash to {actual} — "
                           f"the object was replaced by someone who could write that key")
    return data
