package w2cplatform

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
)

// A field too big for a row: the bytes go in the object store, the row keeps a digest.
//
// Some units carry one opaque lump the platform does not interpret: a detector's mask, a panel's firmware,
// a model. Three things about it are true at once — it belongs to exactly one unit, the platform will never
// read inside it, and it does not fit in a row.
//
// The tempting fix is to put such configuration in the object store and be done. It costs more than it
// looks. A row in Variables has CAS, one writer, and a revision the reconciler watches: change a field and
// the unit restarts with the new value. An object has none of that. Move the configuration there whole and
// the revision never moves, so nothing restarts, and you are back to building a mechanism the platform
// already had.
//
// So the bytes go in the object store and the ROW keeps a reference, and the reference is a DIGEST rather
// than a name. Every useful property follows from that one choice: different bytes are a different digest,
// so a different row, so revision moves and the reconciler restarts the unit; a digest key is written once
// and never overwritten, so last-writer-wins stops mattering; a worker can cache by digest forever; the
// same lump on a hundred units is stored once.
//
// The order of writes is the object FIRST, then the row that names it. A crash between the two leaves an
// object nobody points at — harmless, and collectable. The other order leaves a row pointing at nothing,
// which is a unit that cannot start.
//
// The honest residue: nothing in the platform deletes an object, so unreferenced blobs accumulate and a
// sweep does not exist yet.
//
// The spelling is `sha256-<hex>` — a dash, not a colon, because this string is also a key, and a key
// becomes a path on a filesystem, and a colon is not a path character everywhere (Lesson 24).
const (
	Blobs = "blobs"
	Algo  = "sha256"
)

var digestRe = regexp.MustCompile(`^sha256-[0-9a-f]{64}$`)

// Digest is the name these bytes have, and the only name they have.
func Digest(data []byte) string {
	sum := sha256.Sum256(data)
	return Algo + "-" + hex.EncodeToString(sum[:])
}

func IsDigest(v string) bool { return digestRe.MatchString(v) }

// BlobKey is `<name>/blobs/sha256-<hex>` — the bytes of one blob field, named by what they are. Content
// addressed, so this key is written once and never written again.
func (s Subsystem) BlobKey(d string) (string, error) {
	if !IsDigest(d) {
		return "", fmt.Errorf("not a digest: %q — a blob field holds `sha256-<hex>`, and the bytes go to the object store first", d)
	}
	return s.Name + "/" + Blobs + "/" + d, nil
}
