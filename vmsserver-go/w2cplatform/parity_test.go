// The three words the scan subsystem needed, and one key: `near: {sub, by}`, `retire_when`,
// `events.older_epochs`, `<name>/requests/<id>`. Same spelling, same refusals and same behaviour as the
// Python port — a YAML that loads there and is ignored here is the quiet kind of divergence.
package w2cplatform_test

import (
	"strings"
	"testing"

	"vmsserver/testbox"
	p "vmsserver/w2cplatform"
)

const jobYAML = `
name: detjob
unit:
  rows: jobs
  id: name
  fields:
    name:   {type: string, required: true}
    cam:    {type: string, required: true}
    rec:    {type: string, required: true}
    kind:   {type: string, required: true}
    from:   {type: float,  required: true}
    to:     {type: float,  required: true}
    state:  {type: string, default: queued}
    labels: {type: list,   default: ["gpu"]}
placement:
  capacity:    {from: capacity, fallback: 2}
  headroom:    {from: headroom}
  constraint:  labels-subset
  near:        {sub: rec, by: rec}
  retire_when: {field: state, in: [done, failed]}
  tie_break:   most-free-capacity
snapshot: [name, cam, kind, state]
events:
  older_epochs: earlier-run
console:
  running: jobs_running
`

func spec(t *testing.T, src string) *p.SubsystemSpec {
	t.Helper()
	v, err := p.ParseYAML(src)
	if err != nil {
		t.Fatal(err)
	}
	s, err := p.SpecFromMap(v.(map[string]any))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func refused(t *testing.T, src, want string) {
	t.Helper()
	v, err := p.ParseYAML(src)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.SpecFromMap(v.(map[string]any)); err == nil {
		t.Fatalf("accepted a spec that should be refused (%s)", want)
	} else if !strings.Contains(err.Error(), want) {
		t.Fatalf("refused for the wrong reason: %v", err)
	}
}

func jobCtl(t *testing.T, box *testbox.Box) *p.SpecController {
	t.Helper()
	return p.NewSpecController(spec(t, jobYAML), box.Vars, box.Objects, 2, box.Wall.Now, "cluster-a")
}

func worker(box *testbox.Box, sub p.Subsystem, name, server string, capacity int, status ...map[string]any) {
	box.Objects.Put(sub.HeartbeatKey(name), p.Heartbeat{Worker: name, Ts: box.Wall.Now(), Status: status,
		Extra: map[string]any{"server": server, "capacity": capacity, "headroom": capacity, "labels": "gpu"}}.ToBytes())
}

func job(t *testing.T, ctl *p.SpecController, name string) {
	t.Helper()
	if _, err := ctl.Create(p.Row{"name": name, "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0}); err != nil {
		t.Fatal(err)
	}
}

// -- near: {sub, by} -------------------------------------------------------------------------------

func TestTheAffinityFollowsAFieldAndNotTheUnitsOwnName(t *testing.T) {
	box := testbox.NewBox()
	s := spec(t, jobYAML)
	if s.Near != "rec" || s.NearBy != "rec" {
		t.Fatal(s.Near, s.NearBy)
	}
	ctl := jobCtl(t, box)
	job(t, ctl, "7-lpr-1")

	// a recorder holding recording 7 — `id: cam` makes that the camera's number
	worker(box, p.Subsystem{Name: "rec"}, "r-2", "srv-2", 50, map[string]any{"id": "7", "phase": "running"})
	if got := ctl.NearID("7-lpr-1"); got != "7" {
		t.Fatalf("NearID read the wrong thing: %q", got)
	}
	if w, server := ctl.HolderNear("7-lpr-1"); w != "r-2" || server != "srv-2" {
		t.Fatalf("the recorder is right there and was not found: %q %q", w, server)
	}
}

func TestTheShortFormStillMeansTheSameID(t *testing.T) {
	s := spec(t, strings.Replace(jobYAML, "near:        {sub: rec, by: rec}", "near:        rec", 1))
	if s.Near != "rec" || s.NearBy != "id" {
		t.Fatal(s.Near, s.NearBy)
	}
}

func TestANearByNamingNoFieldIsRefusedAtLoad(t *testing.T) {
	refused(t, strings.Replace(jobYAML, "by: rec}", "by: recording}", 1), "near.by names no field")
	refused(t, strings.Replace(jobYAML, "near:        {sub: rec, by: rec}", "near:        {by: rec}", 1), "needs a near to follow")
}

// -- retire_when -----------------------------------------------------------------------------------

func TestFinishedWorkIsNotPlaced(t *testing.T) {
	box := testbox.NewBox()
	ctl := jobCtl(t, box)
	worker(box, ctl.Sub, "j-1", "srv-1", 2)
	job(t, ctl, "a")
	job(t, ctl, "b")
	if _, err := ctl.Update("b", p.Row{"state": "done"}); err != nil {
		t.Fatal(err)
	}
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if ctl.Placement("a") == nil || ctl.Placement("b") != nil {
		t.Fatal("finished work was placed, or unfinished work was not")
	}
}

func TestTheBudgetAFinishedJobHeldComesBack(t *testing.T) {
	box := testbox.NewBox()
	ctl := jobCtl(t, box)
	worker(box, ctl.Sub, "j-1", "srv-1", 1) // one slot
	job(t, ctl, "a")
	job(t, ctl, "b")
	ctl.EnsurePlaced(nil)
	if ctl.Placement("a") == nil || ctl.Placement("b") != nil {
		t.Fatal("the one slot did not go to the first job")
	}
	ctl.Update("a", p.Row{"state": "done"})
	ctl.EnsurePlaced(nil)
	if ctl.Placement("a") != nil || ctl.Placement("b") == nil {
		t.Fatal("the slot a finished job held did not come back")
	}
}

func TestTheReasonSaysWhichEndItCameTo(t *testing.T) {
	box := testbox.NewBox()
	ctl := jobCtl(t, box)
	worker(box, ctl.Sub, "j-1", "srv-1", 2)
	job(t, ctl, "a")
	ctl.EnsurePlaced(nil)
	ctl.Update("a", p.Row{"state": "failed"})
	ctl.EnsurePlaced(nil)
	it, _, _ := box.Vars.Get(ctl.Sub.Config("placement", "a"))
	if it["worker"] != "" || it["reason"] != "failed" {
		t.Fatalf("%q %q — done and failed are different news", it["worker"], it["reason"])
	}
}

func TestFinishedWorkIsNotTheClustersFault(t *testing.T) {
	box := testbox.NewBox()
	ctl := jobCtl(t, box)
	worker(box, ctl.Sub, "j-1", "srv-1", 2) // labels: gpu
	ctl.Create(p.Row{"name": "a", "cam": "7", "rec": "7", "kind": "lpr", "from": 1.0, "to": 2.0, "labels": []string{"fpga"}})
	ctl.Create(p.Row{"name": "b", "cam": "7", "rec": "7", "kind": "lpr", "from": 1.0, "to": 2.0, "labels": []string{"fpga"}})
	ctl.Update("b", p.Row{"state": "done"})
	ctl.EnsurePlaced(nil)
	un := ctl.Unplaceable()
	if len(un) != 1 || p.Str(un[0].ID) != "a" {
		t.Fatalf("/unplaceable should name the one still waiting, and only it: %+v", un)
	}
}

func TestAPredicateThatRetiresNothingIsRefusedAtLoad(t *testing.T) {
	refused(t, strings.Replace(jobYAML, "{field: state, in: [done, failed]}", "{field: phase, in: [done]}", 1), "retire_when names no field")
	refused(t, strings.Replace(jobYAML, "{field: state, in: [done, failed]}", "{field: state}", 1), "non-empty")
	refused(t, strings.Replace(jobYAML, "{field: state, in: [done, failed]}", "{in: [done]}", 1), "non-empty")
}

// -- events.older_epochs ---------------------------------------------------------------------------

// Two runs of one unit, written as the workers write them, read back through the real database.
func twoRuns(t *testing.T, box *testbox.Box, sub, unit string) *p.EventDatabase {
	t.Helper()
	for _, e := range []int{1, 2} {
		if _, err := p.NewEventLog(box.Archive, sub, unit, e, 0).Append(1000+float64(e), "lpr", map[string]any{"cam": 7}); err != nil {
			t.Fatal(err)
		}
		if _, _, err := p.NextEpoch(box.Vars, sub+"/epoch/"+unit); err != nil {
			t.Fatal(err)
		}
	}
	db := p.NewEventDatabase(box.Archive, "srv-1", box.Wall.Now, 0)
	db.Rebuild()
	return db
}

func TestOneComparisonTwoMeanings(t *testing.T) {
	box := testbox.NewBox()
	db := twoRuns(t, box, "detjob", "7-lpr-1")
	cur, _ := p.CurrentEpoch(box.Vars, "detjob/epoch/7-lpr-1")
	got := db.Query(p.Query{T0: 0, T1: 1e12, CurrentEpochs: map[[2]string]int{{"detjob", "7-lpr-1"}: cur},
		EpochPolicy: map[string]string{"detjob": "earlier-run"}})
	if len(got.Events) != 2 {
		t.Fatal(len(got.Events))
	}
	if got.Events[0].EpochIs != "earlier-run" || got.Events[0].Fenced {
		t.Fatalf("the finished earlier run was struck through: %q %v", got.Events[0].EpochIs, got.Events[0].Fenced)
	}
	if got.Events[1].EpochIs != "current" {
		t.Fatal(got.Events[1].EpochIs)
	}
}

func TestALiveDetectorStillStrikesItsZombieThrough(t *testing.T) {
	box := testbox.NewBox()
	db := twoRuns(t, box, "det", "7-linecross")
	cur, _ := p.CurrentEpoch(box.Vars, "det/epoch/7-linecross")
	q := p.Query{T0: 0, T1: 1e12, CurrentEpochs: map[[2]string]int{{"det", "7-linecross"}: cur}}

	got := db.Query(q) // no policy at all: the behaviour this code had before it could be asked
	if !got.Events[0].Fenced || got.Events[0].EpochIs != "fenced" {
		t.Fatalf("%q %v", got.Events[0].EpochIs, got.Events[0].Fenced)
	}
	q.EpochPolicy = map[string]string{"det": "fenced"}
	if got := db.Query(q); !got.Events[0].Fenced {
		t.Fatal("saying the default out loud changed it")
	}
}

func TestAMeaningThePageCannotDrawIsRefusedAtLoad(t *testing.T) {
	refused(t, strings.Replace(jobYAML, "older_epochs: earlier-run", "older_epochs: superseded", 1), "fenced or earlier-run")
}

func TestTheDefaultIsWhatEveryOtherSubsystemSays(t *testing.T) {
	if s := spec(t, jobYAML); s.OlderEpochs != "earlier-run" {
		t.Fatal(s.OlderEpochs)
	}
	plain := spec(t, strings.Replace(jobYAML, "events:\n  older_epochs: earlier-run\n", "", 1))
	if plain.OlderEpochs != "fenced" {
		t.Fatal(plain.OlderEpochs)
	}
}

// -- <name>/requests/<id> and the bare snapshot ----------------------------------------------------

func TestARequestIsAKeyWithAGrant(t *testing.T) {
	s := spec(t, jobYAML)
	if got := s.Sub().RequestKey("7-100-200"); got != "detjob/requests/7-100-200" {
		t.Fatal(got)
	}
	if got := s.Sub().RequestsPrefix(); got != "detjob/requests/" {
		t.Fatal(got)
	}
	found := false
	for _, g := range s.ACLConsole() {
		if g == "detjob/requests/*" {
			found = true
		}
	}
	if !found {
		t.Fatal("the console cannot write what it is the only writer of")
	}
}

func TestDeclaringNoFieldsIsNotDeclaringEveryField(t *testing.T) {
	empty := spec(t, strings.Replace(jobYAML, "snapshot: [name, cam, kind, state]", "snapshot: []", 1))
	if len(empty.Snapshot) != 0 {
		t.Fatalf("`snapshot: []` published %v", empty.Snapshot)
	}
	full := spec(t, strings.Replace(jobYAML, "snapshot: [name, cam, kind, state]\n", "", 1))
	if len(full.Snapshot) == 0 {
		t.Fatal("a spec that says nothing about the snapshot publishes every field that may go")
	}
	refused(t, strings.Replace(jobYAML, "snapshot: [name, cam, kind, state]", "snapshot:", 1), "says neither")
}
