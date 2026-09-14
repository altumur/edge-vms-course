# make-ca.sh — a two-level update CA on your own machine, plus the attacker's CA

**Role.** Lesson 2, Step 2. Creates, in `pki/out/`, the root CA whose certificate is the *only* thing that ships on a device (`keyring.pem`), an issuing "Development-1" certificate that CI signs bundles with (`rauc/build-bundle.sh` uses `dev.cert.pem` + `dev.key.pem`; the certificate travels inside the bundle), and a second, perfectly valid CA the device has never heard of, for the wrong-signer proof in `pki/verify-chain.sh` and `build-bundle.sh --rogue`. Runs on the developer host with OpenSSL ≥ 1.1.1 (`-addext`), no VM. Exits 1 if a CA already exists in `OUT`. Keys never exist on a device. `-nodes` leaves the private keys unencrypted — fine for a lesson, wrong for production; a real root lives offline (М12 Lesson 7 has the ceremony).

## Environment variables
- `OUT` — output directory, default `pki/out` beside the script.
- `ORG` — organisation name for subjects, default `Example VMS`.

## Step by step
### Guard
`mkdir -p "$OUT" && cd "$OUT"`; if `ca.key.pem` exists, refuse ("remove it to start over") so a rerun cannot silently replace the root the bench already trusts.

### Root CA
`openssl req -x509 -newkey rsa:3072 … -days 3650` with `basicConstraints=critical,CA:TRUE` and `keyUsage=critical,keyCertSign,cRLSign`. Ten years because a root that expires bricks the update path fleet-wide. Subject `/O=$ORG/CN=$ORG Update CA`.

### Issuing certificate
A 3072-bit key and CSR for `CN=$ORG Development-1`, signed by the root for 750 days with `CA:FALSE` and `keyUsage=digitalSignature` (the extensions are supplied through a process-substitution `-extfile`). `-CAcreateserial` produces `ca.cert.srl`. `openssl verify -CAfile ca.cert.pem dev.cert.pem` proves the chain before anything is signed with it. Rotatable without touching a device: only the root is on the box.

### Keyring and permissions
`cp ca.cert.pem keyring.pem` — the keyring is just the root; intermediates travel inside the bundle (`rauc/system.conf` `[keyring] path=/etc/rauc/keyring.pem`, `bench/build-disk.sh` copies it into the image if present). `chmod 600 ./*.key.pem` on every private key.

### The attacker
A second self-signed CA (`/O=Attacker/CN=Attacker CA`, `CA:TRUE`) and a signer issued by it (`rogue.cert.pem`, `rogue.key.pem`). Everything about it is valid — that is the point: the device refuses it not because it is malformed but because it does not chain to the keyring.

### Summary
Prints where the keyring (ships in the image), the signer (stays in CI) and the attacker (Step 3, Lesson 3) ended up.

## Notes
- `rogue.cert.pem` is issued without an `-extfile`, so it carries no key-usage extension; OpenSSL's CMS verification still accepts it as a signer, which is what `verify-chain.sh` needs.
- The serial files `*.srl` and the CSRs are left in `out/`; nothing reads them again.
