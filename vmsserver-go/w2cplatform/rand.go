package w2cplatform

import (
	"crypto/rand"
	"encoding/binary"
)

func randomSuffix() uint32 {
	var b [4]byte
	rand.Read(b[:])
	return binary.LittleEndian.Uint32(b[:]) & 0xFFFFFF
}
