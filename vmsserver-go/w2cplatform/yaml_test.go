package w2cplatform_test

import (
	"os"
	"testing"

	p "vmsserver/w2cplatform"
)

func TestYAMLSubsetReadsTheSpec(t *testing.T) {
	src, _ := os.ReadFile("../vms/vms.subsystem.yaml")
	v, err := p.ParseYAML(string(src))
	if err != nil {
		t.Fatal(err)
	}
	m := v.(map[string]any)
	unit := m["unit"].(map[string]any)
	fields := unit["fields"].(map[string]any)
	if m["name"] != "vms" || unit["rows"] != "cameras" || fields["name"].(map[string]any)["default"] != "cam{id}" ||
		fields["enabled"].(map[string]any)["default"] != true || fields["events_retention_days"].(map[string]any)["default"] != 365 {
		t.Fatal(m)
	}
	d := unit["derived"].([]any)[0].(map[string]any)
	if d["row"] != "retention/{id}" || d["items"].(map[string]any)["days"] != "events_retention_days" || d["on_delete"].(map[string]any)["days"] != 0 {
		t.Fatal(d)
	}
	pl := m["placement"].(map[string]any)
	if pl["constraint"] != "labels-subset" || pl["rebalance"].(map[string]any)["dead_band"] != 0.10 || len(m["snapshot"].([]any)) != 8 {
		t.Fatal(pl, m["snapshot"])
	}
	// The credential the device needs: two fields, and the snapshot names NEITHER of them. The secret is
	// out because the spec refuses it there; the login is out because nothing above the cluster reads it.
	// Both facts are declared HERE — in the YAML — and not in the code that publishes the snapshot.
	if _, ok := fields["cred_username"]; !ok {
		t.Fatal(fields)
	}
	if _, ok := fields["cred_secret"]; !ok {
		t.Fatal(fields)
	}
	for _, f := range m["snapshot"].([]any) {
		if f == "cred_secret" || f == "cred_username" {
			t.Fatal("a credential field is in the snapshot:", m["snapshot"])
		}
	}
	// the field Lesson 15 added: a channel held for its archive and nothing else
	if fields["live"].(map[string]any)["default"] != "always" {
		t.Fatal(fields["live"])
	}
}
