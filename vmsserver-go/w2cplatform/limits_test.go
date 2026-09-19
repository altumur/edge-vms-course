// The ceiling a store declares, and what the platform does when a write is over it.
//
// Before this, the 64 KiB of a Nomad Variable lived in prose. FsObjectStore accepted anything, so a write
// that production would reject was green here, and the platform found its ceiling the way you find a
// ceiling in the dark. These tests build stores that DO have a ceiling, which is the only way the refusal
// is observable on one box.
package w2cplatform_test

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"

	"vmsserver/testbox"
	p "vmsserver/w2cplatform"
)

// The console's Reply carries an `any` body; every reply in these tests is a map.
func body(r p.Reply) map[string]any { m, _ := r.Body.(map[string]any); return m }

// MaxBytes is an ANSWER, not a method that may be missing. A directory has no ceiling and says 0.
func TestAStoreWithNoCeilingStillAnswersTheQuestion(t *testing.T) {
	box := testbox.NewBox()
	if box.Objects.MaxBytes() != 0 || box.Vars.MaxBytes() != 0 {
		t.Fatal(box.Objects.MaxBytes(), box.Vars.MaxBytes())
	}
}

func TestTheObjectStoreRefusesOverItsCeilingAndKeepsWhatWasThere(t *testing.T) {
	small, err := p.NewFsObjectStoreCapped(t.TempDir(), 1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := small.Put("k", make([]byte, 1000)); err != nil {
		t.Fatal(err)
	}
	err = small.Put("k", make([]byte, 2000))
	var tooLarge *p.TooLarge
	if !errors.As(err, &tooLarge) {
		t.Fatal("a write over the store's own ceiling was accepted:", err)
	}
	if tooLarge.Size != 2000 || tooLarge.Limit != 1024 {
		t.Fatal(tooLarge)
	}
	got, _ := small.Get("k")
	if len(got) != 1000 {
		t.Fatal("refused, but not cleanly:", len(got)) // refused, not truncated
	}
}

// The store refuses with bytes. The caller knows what those bytes were, and a shard is ONE worker's
// assignment — so an oversized shard is no longer a shape problem to be solved by cutting differently. It
// is a store too small to hold what a single worker carries, and the message says which knob that is.
func TestAnOversizedShardSaysHowManyUnitsThatWas(t *testing.T) {
	box := testbox.NewBox()
	tiny, err := p.NewFsObjectStoreCapped(t.TempDir(), 512)
	if err != nil {
		t.Fatal(err)
	}
	ctl := p.NewSpecController(detSpec(t), box.Vars, tiny, 50, box.Wall.Now, "cluster-a")
	for i := 0; i < 20; i++ {
		if _, err := ctl.Create(map[string]any{"name": string(rune('a'+i)) + "-linecross", "cam": "7", "kind": "linecross"}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := ctl.EnsurePlaced([]string{"w-1"}); err != nil {
		t.Fatal(err)
	}
	err = ctl.PublishSnapshot()
	var tooLarge *p.TooLarge
	if !errors.As(err, &tooLarge) {
		t.Fatal("a shard over the store's ceiling was published:", err)
	}
	if !strings.Contains(err.Error(), "20 units on w-1") || !strings.Contains(err.Error(), "OBJECTS=") {
		t.Fatal(err)
	}
}

// The worst version of this defect is the silent one. A row over the store's ceiling is refused at the
// console, with the size and the limit in the sentence, before anything is written.
func TestARowThatDoesNotFitIsA413ToThePersonWhoTypedIt(t *testing.T) {
	box := testbox.NewBox()
	capped, err := p.NewFileVariablesCapped(t.TempDir(), 400)
	if err != nil {
		t.Fatal(err)
	}
	ctl := p.NewSpecController(detSpec(t), capped, box.Objects, 8, box.Wall.Now, "cluster-a")
	con := p.NewSpecConsole(ctl, p.ConsoleOptions{})
	if rep := con.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); rep.Status != 201 {
		t.Fatal(rep)
	}
	rep := con.Update("7-linecross", map[string]any{"params": strings.Repeat("x", 500)})
	if rep.Status != 413 {
		t.Fatal(rep.Status, rep.Body)
	}
	if !strings.Contains(p.Str(body(rep)["detail"]), "over this store's limit of 400") {
		t.Fatal(rep.Body)
	}
	if p.Str(ctl.Unit("7-linecross")["params"]) != "" {
		t.Fatal("the row was written anyway")
	}
}

// How Nomad measures a Variable, and therefore how the platform must: every key and every value in the
// path, together — not the JSON around them.
func TestTheSizeAStoreChargesIsKeysAndValues(t *testing.T) {
	if n := p.ItemsBytes(p.Items{"a": "xx", "bb": "y"}); n != 1+2+2+1 {
		t.Fatal(n)
	}
}

// Two jobs in one loop, and what it costs to report them as one.
//
// The controller's pass PLACES units and PUBLISHES a copy of where they went. They fail independently and
// mean different things to whoever is woken up. In the Python port the four calls shared one try and one
// sentence — "placement pass failed" — and since publishing is LAST, that sentence named the one thing
// that had not failed. Here the publish returned an error nobody looked at, which is the same defect with
// the volume turned down.
func TestTheAgeOfThePublishedCopyIsOnMetrics(t *testing.T) {
	box := testbox.NewBox()
	ctl := p.NewSpecController(detSpec(t), box.Vars, box.Objects, 8, box.Wall.Now, "cluster-a")
	con := p.NewSpecConsole(ctl, p.ConsoleOptions{})
	if _, err := ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); err != nil {
		t.Fatal(err)
	}

	// never published is not the same as published a moment ago, and the gauge says so
	if _, ok := ctl.SnapshotAge(0); ok {
		t.Fatal("nothing was published, and the age pretended otherwise")
	}
	if !strings.Contains(con.MetricsText(), "det_snapshot_age_seconds -1") {
		t.Fatal(con.MetricsText())
	}

	if _, err := ctl.EnsurePlaced([]string{"w-1"}); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(con.MetricsText(), "det_snapshot_age_seconds 0") {
		t.Fatal(con.MetricsText())
	}

	box.Wall.Advance(90) // the controller stopped publishing
	if age, ok := ctl.SnapshotAge(0); !ok || math.Round(age) != 90 {
		t.Fatal(age, ok)
	}
	if !strings.Contains(con.MetricsText(), "det_snapshot_age_seconds 90") {
		t.Fatal(con.MetricsText())
	}
}

// A number like this must only ever be wrong in the pessimistic direction. One shard that stopped being
// rewritten IS the cluster being behind, and taking the newest would report an RPO better than the real one.
func TestTheAgeIsTheStalestShardAndNeverTheFreshest(t *testing.T) {
	box := testbox.NewBox()
	ctl := p.NewSpecController(detSpec(t), box.Vars, box.Objects, 2, box.Wall.Now, "cluster-a")
	for _, i := range []string{"7", "8", "9", "10"} {
		if _, err := ctl.Create(map[string]any{"name": i + "-linecross", "cam": i, "kind": "linecross"}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := ctl.EnsurePlaced([]string{"w-1", "w-2"}); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	raw, _ := box.Objects.Get("det/snapshot/w-1")
	var shard map[string]any
	json.Unmarshal(raw, &shard)
	shard["ts"] = box.Wall.Now() - 300 // w-1's shard stopped moving
	out, _ := json.Marshal(shard)
	box.Objects.Put("det/snapshot/w-1", out)

	age, ok := ctl.SnapshotAge(0)
	if !ok || math.Round(age) != 300 {
		t.Fatal("the freshest shard was taken, and the RPO looked better than it is:", age)
	}
}
