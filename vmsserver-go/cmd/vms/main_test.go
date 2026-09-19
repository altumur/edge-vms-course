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
