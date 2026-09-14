# verify-chain.sh — prove the CMS signature check with OpenSSL before trusting RAUC with it

**Role.** Lesson 2, Step 3. Uses the same CMS primitive RAUC uses (`openssl cms`) on a stand-in payload to show the three outcomes that the update path depends on: a signature that chains to the keyring is accepted; a valid key the keyring never heard of is refused ("who signed this?"); a good signature over changed content is refused ("is this what they signed?"). No RAUC, no VM; two minutes on the host after `pki/make-ca.sh`. Exit 0 when all three come out as designed, 1 otherwise (or if `pki/out` does not exist). Verified against OpenSSL 3.0.13 per the README.

## Environment variables
- `OUT` — the PKI directory, default `pki/out` beside the script.

## Step by step
### Setup
`set -u` only — deliberately not `-e`, because `openssl cms -verify` exits 4 on a refusal and refusals are the expected result. `cd "$OUT"` or fail with "run pki/make-ca.sh first". A temporary directory `W` is created and removed on exit.

### Sign
Two payloads: `payload.bin` and `tampered.bin` (one word different). `openssl cms -sign … -binary -outform DER` produces a detached DER signature over `payload.bin` twice — once with the real `dev.cert.pem`/`dev.key.pem` (`payload.sig`), once with `rogue.cert.pem`/`rogue.key.pem` (`rogue.sig`). The signer certificate is embedded in the CMS structure, exactly as it is in a RAUC bundle, so the verifier needs only the root.

### `verify()`
`openssl cms -verify -in <sig> -inform DER -CAfile keyring.pem -content <payload> -binary -out /dev/null 2>&1 || true` — verify a detached signature against the keyring (root only) and return the combined output rather than the status. "Judge by its message, not its status."

### The three proofs
1. `payload.sig` over `payload.bin` — output contains `successful` → accepted.
2. `rogue.sig` over `payload.bin` — output contains `issuer` (`unable to get local issuer certificate`) → refused: the chain does not reach the keyring.
3. `payload.sig` over `tampered.bin` — output contains `verif` (`content verify error`) → refused: the digest does not match.
Each prints a line marked `(OK)` or `UNEXPECTED`, and `pass` counts the expected outcomes.

### Verdict
`pass -eq 3` prints "all 3 as designed"; otherwise prints the score and exits 1.

## Notes
- The messages matched (`successful`, `issuer`, `verif`) are OpenSSL's; a different OpenSSL major version could word them differently and turn a correct refusal into an `UNEXPECTED` line. The README pins the tested version.
