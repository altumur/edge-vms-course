# shared.py — Lesson 12: settings of the system as one signed object behind a pointer, carried by every member's agent, resolved as defaults when read

**Role in the module.** Defaults, the folder tree, scenarios between cameras belong to no camera, and with cameras as members there is no cluster to keep them in; the domain has no database. The identity set's publish-then-point (Lesson 4) plus the offline key set (Lessons 4, 7): the document is an object (it outgrows a 64 KiB Variable), the pointer is the CAS, the signature makes any copy as good as the original, `(term, rev)` keeps a member from going backwards, and the member keeps the verified copy in its own durable store. Also used by Lesson 15 (`term.py`) to carry the domain's backup, through `carry`'s path arguments.

## Module-level names
- `POINTER = OBJECT = "domain/shared"` (the pointer in Variables, the document in a member's objects), `REFUSED = "domain/shared-refused"`.

## Functions
### `sign(doc, issuer) -> dict`, `verify(doc, keys, now) -> dict`
Ed25519 over canonical JSON without `kid`/`sig`, by the signer's token key. `verify` raises `NotTaken` for no key set, an untrusted or retired `kid`, or a bad signature.
### `carry(domain_vars, domain_objects, member_vars, member_objects, keys, now, src=POINTER, dst=POINTER, obj=OBJECT, refused=REFUSED) -> str`
The agent's side. Nothing published → nothing. Member already at or past `(term, rev)` → nothing ("up to date" / "holding newer"). Otherwise fetch, check checksum, signature (against the MEMBER's keys) and that the object's `(term, rev)` matches the pointer; failure writes `refused` with the reason and keeps the old copy; success writes the object, then the member's pointer.

## `class SharedSettings`
The domain's side. `current()` → the document and the pointer's index. `edit(mutate, base_rev, by=None)` → a `Conflict` if the editor's `base_rev` is stale; else signs rev+1 with the current `term()`, puts `shared/rev-<n>`, then the pointer by CAS. `delivery(fed)` → per member `holding` / `behind` / `refused` / `silent`, from each member's copy of the pointer, and a sentence naming the silent and the refusing.

## `class SharedView`
A member's console side. `document()` re-verifies the stored copy against the member's own keys; `settings()`; `effective(row)` → `{field: (value, "camera" | "domain rev N")}` — defaults resolved at read time, never written into rows.

## Notes
- `test_a_document_not_signed_by_the_domain_is_refused_and_the_old_one_kept` forges a document with a matching checksum; `test_a_default_is_resolved_when_read_and_never_written_into_a_row` checks no row revision moves.
