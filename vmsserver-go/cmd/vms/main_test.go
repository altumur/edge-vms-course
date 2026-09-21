package main

import (
	"os"
	"strings"
	"testing"
)

// The bug this catches is the one that was actually made: SweepBlobs was written, tested and left
// UNCALLED in this port for a day — every test green, and not one blob collected on a running cluster.
// A unit test of the sweep proves the sweep; only this proves that anything runs it.
//
// It reads the source, which is a weak kind of test and worth being honest about: it would not notice a
// loop that starts and immediately returns. What it does notice is the entire class of "we built it and
// forgot to plug it in", and that class is not hypothetical here.
func TestTheConsoleActuallyRunsTheBlobSweep(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	text := string(src)
	i := strings.Index(text, `case "console"`)
	if i < 0 {
		t.Fatal("no console entry point")
	}
	j := strings.Index(text[i:], `case "resource"`)
	if j < 0 {
		t.Fatal("could not find the end of the console case")
	}
	console := text[i : i+j]
	if !strings.Contains(console, "go sweepLoop(") {
		t.Fatal("the console process does not start the blob sweep — nothing in this port collects blobs")
	}
	// and both subsystems the console fronts are handed to it: the recorder has blobs too
	if !strings.Contains(console, "ctl.SpecController, recCtl") {
		t.Fatal("the sweep was started for some of the subsystems the console fronts, not all of them")
	}
}

// The third subsystem needs a process, and this port has lost work to "written, tested, never called"
// twice. Weak evidence, said out loud — and it catches exactly that.
func TestTheDetectorHasAProcess(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`case "detworker":`, `case "detcontroller":`, "vms.NewDetWorker("} {
		if !strings.Contains(string(src), want) {
			t.Fatalf("the det subsystem has no way to run in this port: %q is missing", want)
		}
	}
}

// And the fifth. A scan needs two processes and a reaper: the worker that scans, the controller that
// places it, and the console pass that moves the row when the worker says the work is over. Without the
// third, `state` never leaves `running` and `retire_when` un-places nothing — every finished job stays
// assigned for ever.
func TestTheScanHasItsProcesses(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`case "detjobworker":`, `case "detjobcontroller":`, "vms.NewDetJobWorker(", "go reapLoop(", "vms.Reap("} {
		if !strings.Contains(string(src), want) {
			t.Fatalf("the detjob subsystem has no way to run in this port: %q is missing", want)
		}
	}
}

// Everything a worker knows and may not write goes through the console's pass. Five functions, each tested
// on its own in vms/ — and each dead unless named here. This project has lost work that way twice.
func TestTheConsolePassCarriesEverythingTheWorkersReport(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	text := string(src)
	i := strings.Index(text, "func reapLoop(")
	j := strings.Index(text[i:], "\n}\n")
	loop := text[i : i+j]
	for _, want := range []string{"vms.Reap(jobCtl", "vms.ClearRequests(recCtl)", "vms.AskForFootage(jobCtl, recCtl",
		"vms.ScanWhatArrived(recCtl, detCtl, jobCtl)", "vms.KeepWhatFired(surveyCtl, recCtl)"} {
		if !strings.Contains(loop, want) {
			t.Fatalf("the console's pass does not run %q", want)
		}
	}
	c := text[strings.Index(text, `case "console"`):strings.Index(text, `case "resource"`)]
	for _, want := range []string{"vms.SurveySpec.ACLConsole()", "vms.DetSpec.ACLConsole()", "go reapLoop(stop, jobCtl, recCtl, detCtl, surveyCtl)",
		"detCtl, jobCtl, surveyCtl}"} {
		if !strings.Contains(c, want) {
			t.Fatalf("the console process is missing %q", want)
		}
	}
}

func TestTheSurveyHasItsProcesses(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`case "surveyworker":`, `case "surveycontroller":`, "vms.NewSurveyWorker("} {
		if !strings.Contains(string(src), want) {
			t.Fatalf("the survey has no way to run in this port: %q is missing", want)
		}
	}
}
