# secrets.py — the camera credential: encrypted at rest, composed into the URL in memory only, redacted everywhere else

**Role in the module.** Lesson 5 — the credential that was hiding in `rtsp_url`. `cred_secret` in `cameras` is AES-GCM ciphertext under a 32-byte key kept on the data partition; this module ships that weaker version and says so: the key travels with the database in every backup, so it is the same factor in a different file. A TPM-sealed key, or a key delivered at runtime, is the debt named in Lesson 5 and paid in М11/М12. What it MUST do — and does — is keep the credential out of every URL string that reaches a log, a pipeline description or an error message. Used by `pipeline.py` (`compose_rtsp_url`, `redact`), `console/app.py` and `tools/provision.py` (`encrypt`), `worker.main()` (`ColumnKey.load`). Depends on `cryptography`.

## Module-level names
- `_NONCE` — 12, the AES-GCM nonce length in bytes; the nonce is prepended to the ciphertext.

## `class ColumnKey`
One AES-GCM key. Created by `load` (from the file) or `generate` (new random key written to the file); holds only the `AESGCM` object.

### `__init__(self, key)`
Requires exactly 32 bytes (AES-256) or raises `ValueError`.

### `load(cls, path)` (classmethod)
Reads the raw bytes of `path` and constructs the key.

### `generate(cls, path)` (classmethod)
32 random bytes from `secrets.token_bytes`, written with `O_WRONLY | O_CREAT | O_EXCL` and mode `0600` — refuses to overwrite an existing key (an overwritten key is every stored credential lost) and is never world-readable. Returns the key. `tools/provision.py key` calls it.

### `encrypt(self, secret, camera_id) -> bytes`
Fresh random nonce, then `nonce + AESGCM.encrypt(nonce, secret, aad=str(camera_id))`. The camera id as associated data means a ciphertext copied between rows fails to decrypt: a credential cannot be moved to another camera by editing the database.

### `decrypt(self, blob, camera_id) -> str`
Splits the nonce off, decrypts with the same associated data; raises `cryptography.exceptions.InvalidTag` on a wrong key, wrong camera or tampered blob.

## Functions
### `compose_rtsp_url(cam, key)`
Builds the URL GStreamer needs, credential inline, IN MEMORY ONLY. Returns `cam["rtsp_url"]` unchanged if there is no username, no secret, or no key (so a camera without credentials still starts on a box without a key). Otherwise decrypts, percent-encodes user and secret with `quote(..., safe='')`, and rebuilds the URL with `user:pass@host` as the netloc. The docstring's rule: log `cam["rtsp_url"]`, never the return value — it is about to be interpolated into a pipeline description that GStreamer will happily print in an error message.

### `redact(text)`
Last line of defence: `re.sub` replaces `user:pass@` in every `rtsp://` or `rtsps://` URL in the text with `***@`. `pipeline.py` passes every bus error and parse exception through it before storing `last_error`.

## Notes
- The tests (`test_worker.py`, `test_restart.py`) build pipelines with `key=None` and credential-free cameras; the encryption itself is listed among the "run" items in the README.
