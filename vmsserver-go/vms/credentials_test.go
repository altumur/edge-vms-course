package vms_test

import (
	"encoding/json"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// The three ways a row leaves the console, and the one that is easy to miss.
//
// A create's reply is what the Idempotency-Key store keeps in order to answer a retry the same way. An
// unmasked reply therefore writes a SECOND copy of the secret into the config store, under a key nobody
// would think to look at — so the last thing this test does is walk the whole store.
func TestTheConsoleNeverHandsOutTheDeviceSecret(t *testing.T) {
	const secret = "Hunter2-not-in-any-reply"
	box := testbox.NewBox()
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	srv, ln, err := vms.Serve(con, vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now), "127.0.0.1:0", box.Wall.Now, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()

	st, r, raw := call(t, "POST", base+"/cameras", map[string]any{"name": "gate", "source": "driverpack://acme/10.0.0.5",
		"cred_username": "admin", "cred_secret": secret}, map[string]string{"Idempotency-Key": "c1"})
	if st != 201 || strings.Contains(string(raw), secret) || r["cred_secret"] != p.SecretMask || r["cred_username"] != "admin" {
		t.Fatal("create:", st, string(raw))
	}
	if _, _, again := call(t, "POST", base+"/cameras", map[string]any{"name": "gate"}, map[string]string{"Idempotency-Key": "c1"}); strings.Contains(string(again), secret) {
		t.Fatal("the retry replayed the secret:", string(again))
	}
	if _, _, upd := call(t, "PUT", base+"/cameras/1", map[string]any{"cred_secret": secret + "-2"}, map[string]string{"Idempotency-Key": "c2"}); strings.Contains(string(upd), secret) {
		t.Fatal("update:", string(upd))
	}
	if _, _, list := call(t, "GET", base+"/cameras", nil, nil); strings.Contains(string(list), secret) {
		t.Fatal("list:", string(list))
	}

	// The row itself still holds it: the worker has to read it to open the camera.
	if c := con.Camera("1"); c == nil || c.CredSecret != secret+"-2" {
		t.Fatal("the secret did not reach the row:", c)
	}
	// …and it is the only place in the store that does.
	paths, _ := box.Vars.List("")
	for _, path := range paths {
		items, _, _ := box.Vars.Get(path)
		for k, v := range items {
			if strings.Contains(v, secret) && !(path == "vms/cameras/1" && k == "cred_secret") {
				t.Fatalf("the secret is also stored at %s[%s]", path, k)
			}
		}
	}
}

// The asymmetry, checked where it has consequences: the snapshot is the object М12's directory reads.
func TestTheLoginLeavesTheClusterAndTheSecretDoesNot(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	mustCreate(t, con, map[string]any{"name": "gate", "source": "driverpack://acme/10.0.0.5",
		"cred_username": "admin", "cred_secret": "Hunter2"})
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	raw, _ := box.Objects.Get("vms/snapshot")
	if raw == nil || strings.Contains(string(raw), "Hunter2") {
		t.Fatal("the secret left the cluster:", string(raw))
	}
	if !strings.Contains(string(raw), "admin") {
		t.Fatal("the login is a field like any other and should be here:", string(raw))
	}
	var snap map[string]any
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
}

// The door: a credential may not ride in on the URL of a field that is in the snapshot.
func TestTheCameraSourceRefusesALogin(t *testing.T) {
	box := testbox.NewBox()
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	for _, bad := range []string{"driverpack://root:hunter2@10.0.0.7/cam", "driverpack://admin@10.0.0.7/cam"} {
		if _, err := con.CreateCamera(map[string]any{"name": "gate", "source": bad}); err == nil || !strings.Contains(err.Error(), "may not carry a login") {
			t.Fatalf("a credential rode in on %q: %v", bad, err)
		}
	}
	if cams := con.Cameras(); len(cams) != 0 {
		t.Fatal("something was created on the way to the refusal:", cams)
	}
	cam := mustCreate(t, con, map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"})
	if _, err := con.UpdateCamera(cam.ID, map[string]any{"source": "driverpack://root:hunter2@10.0.0.7/cam"}); err == nil {
		t.Fatal("an update let a credential in")
	}
}
