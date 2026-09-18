package w2cplatform

import "fmt"

// What a store can hold, said by the store.
//
// A store has a ceiling and the platform has to know it. Nomad Variables cap a whole Variable — every key
// and every value in it — at 64 KiB, and that number is not the platform's to choose: it is a constant in
// the scheduler (maxVariableSize = 65536), and the request to make it configurable has been open since 2022
// and answered with "we don't want to give users a new way to break their clusters".
//
// Before this the number lived in prose. FsObjectStore never refuses anything, so a write that production
// would reject succeeded in every test, and the platform found its ceiling the way you find a ceiling in
// the dark. The snapshot (Lesson 25) was found that way: one object for a full cluster's worth of units,
// discovered by arithmetic on paper rather than by a failing test.
//
// So the ceiling becomes a PROPERTY THE STORE DECLARES — MaxBytes, zero meaning no ceiling — and a write
// over it is refused. Refused, and not truncated: half a row is worse than no row, and a store that
// silently drops the tail of an object is a store that lies about Get.
const NoCeiling = 0

// TooLarge is a write over the store's declared ceiling. It carries the numbers rather than a sentence
// about them: the caller that knows what it was writing adds the part a person needs.
type TooLarge struct {
	Key    string
	Size   int
	Limit  int
	Detail string
}

func (e *TooLarge) Error() string {
	s := fmt.Sprintf("%s: %d bytes over this store's limit of %d", e.Key, e.Size, e.Limit)
	if e.Detail != "" {
		s += " — " + e.Detail
	}
	return s
}

// Check returns a *TooLarge when limit is set and size is over it.
func Check(key string, size, limit int) error {
	if limit != NoCeiling && size > limit {
		return &TooLarge{Key: key, Size: size, Limit: limit}
	}
	return nil
}

// ItemsBytes is the size a store charges for a path's items: keys and values, as bytes, which is how Nomad
// measures a Variable. A row is small — a couple of hundred bytes — and that is the point: what makes a row
// big is one field that should not be in a row.
func ItemsBytes(items Items) int {
	n := 0
	for k, v := range items {
		n += len(k) + len(Str(v))
	}
	return n
}
