package cluster

// What travels: the recorder's configuration, as one opaque blob — format 1,
// byte-compatible with the Python version's, so a recorder written in either
// language restores what the other published.
//
// Lesson 3's table: footage stays, the index is rebuilt, events are
// expendable, configuration MUST come. A dump is sites + cameras (operator
// columns, the encrypted credential, the revision) + operators + grants.
// Nothing controller-owned travels: the new instance re-derives phase and
// observed_revision by observation, which is М9 Lesson 5's rule.

import (
	"encoding/json"
	"fmt"
)

// CameraRow is the operator-owned half of М9's cameras table plus the
// controller-assigned revision. CredSecret is the encrypted credential as
// stored — it travels encrypted; the column key travels separately, by the
// cluster (nodes/<node>/key), never inside a backup.
type CameraRow struct {
	ID            int64   `json:"id"`
	SiteID        string  `json:"site_id"`
	Name          string  `json:"name"`
	RtspURL       string  `json:"rtsp_url"`
	CredUsername  *string `json:"cred_username"`
	CredSecret    []byte  `json:"cred_secret"` // base64 on the wire, like Python's b64encode
	Enabled       bool    `json:"enabled"`
	RetentionDays int     `json:"retention_days"`
	Priority      int     `json:"priority"`
	Revision      int64   `json:"revision"`
}

type Site struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Config is the blob. Operators and grants pass through untouched.
type Config struct {
	Format    int               `json:"format"`
	Revision  int64             `json:"revision"`
	Sites     []Site            `json:"sites"`
	Cameras   []CameraRow       `json:"cameras"`
	Operators []json.RawMessage `json:"operators"`
	Grants    []json.RawMessage `json:"grants"`
}

func Encode(sites []Site, cameras []CameraRow, operators, grants []json.RawMessage) ([]byte, error) {
	var rev int64
	for _, c := range cameras {
		if c.Revision > rev {
			rev = c.Revision
		}
	}
	if sites == nil {
		sites = []Site{}
	}
	if cameras == nil {
		cameras = []CameraRow{}
	}
	if operators == nil {
		operators = []json.RawMessage{}
	}
	if grants == nil {
		grants = []json.RawMessage{}
	}
	return json.Marshal(Config{Format: 1, Revision: rev, Sites: sites, Cameras: cameras, Operators: operators, Grants: grants})
}

func Decode(blob []byte) (*Config, error) {
	var c Config
	if err := json.Unmarshal(blob, &c); err != nil {
		return nil, err
	}
	if c.Format != 1 {
		return nil, fmt.Errorf("unknown configuration format %d", c.Format)
	}
	return &c, nil
}

func RevisionOf(blob []byte) (int64, error) {
	var c struct {
		Revision int64 `json:"revision"`
	}
	if err := json.Unmarshal(blob, &c); err != nil {
		return 0, err
	}
	return c.Revision, nil
}
