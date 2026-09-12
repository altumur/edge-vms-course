package cluster

// Lesson 3 — the six steps, as code.
//
//	1. empty Postgres; migrations run                 (the AppHost did this)
//	2. read its own Nomad Variable                    (FromEnvironment)
//	3. fetch that object from the CLUSTER's object store
//	4. restore it; check the revision against the Variable
//	5. request a new epoch                            (NextEpoch)
//	6. begin recording into epoch-N+1                 (the AppHost, with the epoch in the path)
//
// A recorder the directory has never seen comes up `unconfigured` and invents
// nothing. A pointer to a missing object is refused, loudly: that is the
// publication order broken, and a human must look.

import (
	"fmt"
	"log"
)

// RestoreRefused is the loud refusal: the operator must look.
type RestoreRefused struct{ Why string }

func (e *RestoreRefused) Error() string { return "restore refused: " + e.Why }

type RestoreResult struct {
	State    string // "restored" | "unconfigured" | "already-configured"
	Revision int64
	Cameras  int
}

func Rehydrate(id Identity, store Store, objects ObjectStore) (RestoreResult, error) {
	empty, err := store.IsUnconfigured()
	if err != nil {
		return RestoreResult{}, err
	}
	if !empty { // a restart on the same server: the disk is still here
		rev, err := store.ConfigRevision()
		return RestoreResult{"already-configured", rev, 0}, err
	}
	if !id.SeenBefore() { // step 2
		log.Printf("%s: the directory has never seen this recorder; coming up unconfigured", id.recorder)
		return RestoreResult{State: "unconfigured"}, nil
	}
	blob, err := objects.Get(id.ConfigObject) // step 3
	if err != nil {
		return RestoreResult{}, err
	}
	if blob == nil {
		return RestoreResult{}, &RestoreRefused{fmt.Sprintf("Variable names %s but the object store has no such object"+
			" — publication order was broken or the store lost data; refusing to guess", id.ConfigObject)}
	}
	rev, err := RevisionOf(blob) // step 4
	if err != nil {
		return RestoreResult{}, &RestoreRefused{fmt.Sprintf("object %s is not a configuration: %v", id.ConfigObject, err)}
	}
	if rev != id.ConfigRevision {
		return RestoreResult{}, &RestoreRefused{fmt.Sprintf("object %s is revision %d but the Variable says %d; refusing to guess",
			id.ConfigObject, rev, id.ConfigRevision)}
	}
	if _, err := store.RestoreConfig(blob); err != nil {
		return RestoreResult{}, err
	}
	cams, err := store.Cameras()
	if err != nil {
		return RestoreResult{}, err
	}
	log.Printf("%s: restored revision %d, %d cameras, from %s", id.recorder, rev, len(cams), id.ConfigObject)
	return RestoreResult{"restored", rev, len(cams)}, nil
}
