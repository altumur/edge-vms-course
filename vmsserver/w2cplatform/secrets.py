"""A secret is written by the operator and read by the process that needs it.
Everything between those two points renders it as `***`."""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # secrets.py — the `*_secret` rule: what a console may never hand back
#
# **Role in the module.** A device needs a login and a password, so a unit's row has to carry them. Nothing
# else in the system does: placement is decided from labels and headroom, the console renders rows, the
# resource works on files. So a secret has exactly two honest points of contact — the operator writes it,
# and the one worker that opens the device reads it — and every path between them is a leak waiting to be
# found.
#
# The rule is a SUFFIX, not a registry: a field whose name ends `_secret` is a secret. One line in a YAML
# and every subsystem has it, which is the same move `type: int` is. A registry would be a second place to
# keep in step with the specs, and the specs would win.
#
# What the rule buys, in three places that are easy to miss:
# - `GET /<rows>` — the obvious one.
# - the reply to a create, and the reply to an update. Less obvious, and worse: the create's reply is what
#   `IdempotencyKeys` stores to answer a retry, so an unmasked reply puts a SECOND copy of the secret in
#   the config store, under a key nobody would think to look at. That is not a hypothetical — it is what
#   the first version of this did, and `test_credentials.py` scans the whole store because of it.
# - `snapshot:` — the fields that leave the cluster for М12. `SubsystemSpec.from_dict` refuses a spec that
#   names a secret there, and leaves secrets out when the snapshot defaults to "every field". A refusal at
#   load time rather than care at review time.
#
# What it does NOT buy, said plainly: the row in the store holds the secret in the clear, and the store's
# ACL is about writers — anyone who may READ `<sub>/<rows>/*` reads every password. On one box that is the
# file under `PLATFORM_DIR`; on a cluster it is raft, encrypted at rest, with a policy per job. Narrowing
# the readers is a change to the Variables contract, not to this file, and it has not been made.
#
# ## Module-level names
# - `SECRET_MASK` — `"***"`. What a masked value reads as.
# - `is_secret_field(name)` — the whole rule: `name.endswith("_secret")`.
# - `mask_secrets(rows)` — copies of the rows with every secret masked. An EMPTY secret stays empty, so a
#   page can tell "not set" from "set" — a mask over an empty string would make every camera look
#   configured. Copies, never in place: the caller usually holds the row it is about to hand to a worker.
# ================================================================================================
from __future__ import annotations

SECRET_MASK = "***"


# The rule, in one line. `cred_secret` is a secret; `cred_username` is not, and neither is `secret_note` —
# the suffix is the rule and there is no second one.
def is_secret_field(name: str) -> bool:
    return name.endswith("_secret")


# Copies of `rows` with every secret field masked. Empty stays empty.
def mask_secrets(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({k: (SECRET_MASK if is_secret_field(k) and v else v) for k, v in r.items()})
    return out
