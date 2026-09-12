package vmsplatform_test

import (
	"os"
	"testing"

	p "vmsserver/vmsplatform"
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
		fields["enabled"].(map[string]any)["default"] != true || fields["retention_days"].(map[string]any)["default"] != 30 {
		t.Fatal(m)
	}
	d := unit["derived"].([]any)[0].(map[string]any)
	if d["row"] != "retention/{id}" || d["items"].(map[string]any)["days"] != "events_retention_days" || d["on_delete"].(map[string]any)["days"] != 0 {
		t.Fatal(d)
	}
	pl := m["placement"].(map[string]any)
	if pl["constraint"] != "labels-subset" || pl["rebalance"].(map[string]any)["dead_band"] != 0.10 || len(m["snapshot"].([]any)) != 8 {
		t.Fatal(pl)
	}
}
