package w2cplatform

// A secret is written by the operator and read by the process that needs it. Everything between those two
// points renders it as `***`.
//
// A device needs a login and a password, so a unit's row has to carry them. Nothing else in the system
// does: placement is decided from labels and headroom, the console renders rows, the resource works on
// files. So a secret has exactly two honest points of contact, and every path between them is a leak
// waiting to be found.
//
// The rule is a SUFFIX, not a registry: a field whose name ends `_secret` is a secret. One line in a YAML
// and every subsystem has it, which is the same move `type: int` is. A registry would be a second place to
// keep in step with the specs, and the specs would win.
//
// Three places it is applied, and the third is the one that is easy to miss: GET /<rows>; the reply to an
// update; and the reply to a CREATE — which is what IdempotencyKeys stores to answer a retry, so an
// unmasked one puts a second copy of the secret in the config store under a key nobody would think to
// look at. `vms/credentials_test.go` walks the whole store because of that.
//
// What this does NOT buy, said plainly: the row holds the secret in the clear, and the store's ACL is
// about writers — anything that may READ <sub>/<rows>/* reads every password. Narrowing the readers is a
// change to the Variables contract, not to this file, and it has not been made.

import "strings"

// SecretMask is what a masked value reads as.
const SecretMask = "***"

// IsSecretField is the whole rule. `cred_secret` is a secret; `cred_username` is not, and neither is
// `secret_note` — the suffix is the rule and there is no second one.
func IsSecretField(name string) bool { return strings.HasSuffix(name, "_secret") }

// MaskSecrets returns COPIES of the rows with every secret masked. An empty secret stays empty, so a page
// can tell "not set" from "set"; a mask over an empty string would make every unit look configured.
// Copies, never in place: the caller usually still holds the row it is about to hand to a worker.
func MaskSecrets(rows []Row) []Row {
	out := make([]Row, 0, len(rows))
	for _, r := range rows {
		cp := make(Row, len(r))
		for k, v := range r {
			if IsSecretField(k) {
				if s, _ := v.(string); s != "" {
					v = SecretMask
				}
			}
			cp[k] = v
		}
		out = append(out, cp)
	}
	return out
}

// MaskRow is MaskSecrets for one row.
func MaskRow(r Row) Row { return MaskSecrets([]Row{r})[0] }
