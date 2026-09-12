package cluster

// Lesson 2 — who am I? Never the allocation index. The scheduler delivers
// the recorder's own Variable through its template as environment; this reads
// it, and can also read the Variable directly for the values a template did
// not carry (the column key).
//
//	nodes/<node>           node, config, revision, cameras
//	nodes/<node>/pg        password
//	nodes/<node>/key       hex            (М9's column key — the debt paid)
//	nodes/<node>/epoch     epoch
//	nodes/<node>/failover  last, worst    (RTO, worst case)
//
// The heartbeat is NOT here: small, frequent, never queried — it is an
// object (<node>/heartbeat in the object store), see apphost.go.

import (
	"encoding/hex"
	"errors"
	"os"
	"strconv"
	"strings"
)

type Identity struct {
	recorder           string
	ConfigObject   string // "" when the directory has never seen this recorder
	ConfigRevision int64
	CameraIDs      []int64
	ColumnKey      []byte
}

func (i Identity) SeenBefore() bool { return i.ConfigObject != "" }

func parseIDs(s string) []int64 {
	var out []int64
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, atoi(p))
		}
	}
	return out
}

// FromEnvironment reads NODE_ID (or NOMAD_JOB_NAME), CONFIG_OBJECT,
// CONFIG_REVISION and CAMERA_IDS; with a Variables client it then prefers
// the Variable — the template may be stale by one render.
func FromEnvironment(v Variables) (Identity, error) {
	node := os.Getenv("NODE_ID")
	if node == "" {
		node = os.Getenv("NOMAD_JOB_NAME")
	}
	if node == "" {
		return Identity{}, errors.New("NODE_ID is not set: a recorder must know what it is before it does anything")
	}
	id := Identity{recorder: node, ConfigObject: os.Getenv("CONFIG_OBJECT"),
		ConfigRevision: atoi(os.Getenv("CONFIG_REVISION")), CameraIDs: parseIDs(os.Getenv("CAMERA_IDS"))}
	if v == nil {
		return id, nil
	}
	if items, _, err := v.Get("nodes/" + node + "/key"); err == nil && items["hex"] != "" {
		if k, err := hex.DecodeString(items["hex"]); err == nil {
			id.ColumnKey = k
		}
	}
	items, _, err := v.Get("nodes/" + node)
	if err != nil {
		return id, err
	}
	if items != nil {
		if c, ok := items["config"]; ok {
			id.ConfigObject = c
		}
		if r, ok := items["revision"]; ok {
			id.ConfigRevision, _ = strconv.ParseInt(r, 10, 64)
		}
		if cams := parseIDs(items["cameras"]); len(cams) > 0 {
			id.CameraIDs = cams
		}
	}
	return id, nil
}
