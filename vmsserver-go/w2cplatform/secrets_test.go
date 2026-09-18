package w2cplatform_test

import (
	"strings"
	"testing"

	p "vmsserver/w2cplatform"
)

// A secret is written by the operator and read by the worker that needs it. The console is neither: it
// renders the rows, so it renders them masked.
func TestSecretsAreMaskedOnTheWayOutOfTheConsole(t *testing.T) {
	rows := []p.Row{
		{"id": "7", "name": "cam7", "cred_username": "admin", "cred_secret": "Hunter2"},
		{"id": "8", "cred_username": "", "cred_secret": ""},
	}
	out := p.MaskSecrets(rows)
	if out[0]["cred_secret"] != p.SecretMask {
		t.Fatal("the secret left the console:", out[0])
	}
	if out[1]["cred_secret"] != "" {
		t.Fatal("an unset secret reads as set — a page cannot tell the two apart:", out[1])
	}
	if out[0]["cred_username"] != "admin" || out[0]["name"] != "cam7" {
		t.Fatal("the login is not a secret:", out[0])
	}
	if rows[0]["cred_secret"] != "Hunter2" {
		t.Fatal("MaskSecrets wrote through to the row it was given; the caller still needs it")
	}
}

func TestTheRuleIsTheSuffixAndNothingElse(t *testing.T) {
	if p.IsSecretField("cred_username") || !p.IsSecretField("cred_secret") || p.IsSecretField("secret_note") {
		t.Fatal("the rule is the *_secret suffix, and there is no second one")
	}
}

// The snapshot is what leaves the cluster for М12's directory, so a secret in it is refused when the spec
// LOADS — a different thing from being watched for at review time.
func TestASpecMayNotPutASecretInTheSnapshot(t *testing.T) {
	base := map[string]any{"name": "x", "unit": map[string]any{"fields": map[string]any{
		"host": map[string]any{"type": "string"}, "api_secret": map[string]any{"type": "string"}}}}

	with := func(snap ...any) map[string]any {
		d := map[string]any{}
		for k, v := range base {
			d[k] = v
		}
		if snap != nil {
			d["snapshot"] = snap
		}
		return d
	}
	if _, err := p.SpecFromMap(with("host", "api_secret")); err == nil || !strings.Contains(err.Error(), "may not be in the snapshot") {
		t.Fatal(err)
	}
	// …and the DEFAULT — every field — leaves secrets out rather than refusing: a spec that said nothing
	// made no mistake, and the safe reading of silence is the one that keeps the secret in.
	spec, err := p.SpecFromMap(with())
	if err != nil {
		t.Fatal(err)
	}
	if len(spec.Snapshot) != 1 || spec.Snapshot[0] != "host" {
		t.Fatal(spec.Snapshot)
	}
	// a snapshot naming a field that does not exist is a typo, and typos in this list are expensive
	if _, err := p.SpecFromMap(with("hsot")); err == nil || !strings.Contains(err.Error(), "names no field") {
		t.Fatal(err)
	}
}

// The quiet path a password takes into a system that thinks it has none.
func TestAUrlFieldRefusesALogin(t *testing.T) {
	spec, err := p.SpecFromMap(map[string]any{"name": "x", "unit": map[string]any{"fields": map[string]any{
		"source": map[string]any{"type": "url"}, "note": map[string]any{"type": "string"}}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"driverpack://root:hunter2@10.0.0.7/cam", "driverpack://admin@10.0.0.7/cam"} {
		err := spec.Refuse(map[string]any{"source": bad})
		var refused *p.Refused
		if err == nil || !strings.Contains(err.Error(), "may not carry a login") {
			t.Fatalf("a credential rode in on the URL %q: %v", bad, err)
		}
		_ = refused
	}
	if err := spec.Refuse(map[string]any{"source": "driverpack://acme/10.0.0.7"}); err != nil {
		t.Fatal("an ordinary URL was refused:", err)
	}
	// a `string` field is not a url field: the rule follows the declared type, not the name
	if err := spec.Refuse(map[string]any{"note": "root:hunter2@10.0.0.7"}); err != nil {
		t.Fatal(err)
	}
}
