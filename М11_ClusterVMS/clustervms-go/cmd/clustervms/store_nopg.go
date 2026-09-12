//go:build !pg

package main

import (
	"context"
	"errors"

	"clustervms/cluster"
)

// Built without -tags pg: no Postgres driver is compiled in. The binary
// still links everything else (Nomad Variables, SigV4, the host) and this
// is what measure.sh sizes; a recorder needs `go get github.com/jackc/pgx/v5`
// and `-tags pg`.
func openStore(context.Context, string) (cluster.Store, func(), error) {
	return nil, nil, errors.New("clustervms was built without -tags pg: no Postgres driver compiled in")
}
