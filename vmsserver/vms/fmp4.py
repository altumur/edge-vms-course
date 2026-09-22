"""What the archive holds, turned into what a browser can play.

The archive stores H.264 access units as the camera sent them: Annex-B, with
the parameter sets repeated on every key frame (`config-interval=-1` on the
recorder's parser is what puts them there). A browser wants the other shape —
NAL units prefixed with their length, inside an MP4, with the parameter sets
once, in the sample entry.

So this module does two things and nothing else: convert one access unit, and
write the boxes around a run of them. No decoding and no re-encoding: the
coded bytes reach the browser exactly as the camera produced them, which is
the promise the live gateway makes (урок 13), kept on the other path.

Why FRAGMENTED MP4 and not a plain one. A plain MP4 keeps its index at one
end, so the whole file has to exist before its first byte can be written — and
an interval of an archive is read as it is answered. A fragmented file is a
header and then a run of self-describing pieces: nothing is ever rewritten,
and a player can start at any fragment.
"""
from __future__ import annotations

import struct

TIMESCALE = 1000          # the archive counts milliseconds; so does this, and nothing rounds on the way
TRACK_ID = 1              # one track: a recording here is video and nothing else

NAL_SLICE, NAL_IDR, NAL_SEI, NAL_SPS, NAL_PPS, NAL_AUD, NAL_FILLER = 1, 5, 6, 7, 8, 9, 12


def _next_start(b: bytes, i: int) -> tuple[int, int]:
    """Offset of the next start code at or after `i`, and its length (3 or 4)."""
    while i + 2 < len(b):
        if b[i] or b[i + 1]:
            i += 1
            continue
        if b[i + 2] == 1:
            return (i - 1, 4) if i > 0 and b[i - 1] == 0 else (i, 3)
        if i + 3 < len(b) and b[i + 2] == 0 and b[i + 3] == 1:
            return i, 4
        i += 1
    return -1, 0


def nals(au: bytes) -> list[bytes]:
    """One Annex-B access unit split into NAL units, start codes removed.

    Both `00 00 01` and `00 00 00 01` are scanned for: a camera may use either
    and is not obliged to be consistent. Zero bytes just before a start code
    belong to it, not to the payload.
    """
    out, i = [], 0
    while i < len(au):
        start, n = _next_start(au, i)
        if n == 0:
            break
        begin = start + n
        nxt, _ = _next_start(au, begin)
        end = len(au) if nxt < 0 else nxt
        while end > begin and au[end - 1] == 0:
            end -= 1
        if end > begin:
            out.append(au[begin:end])
        if nxt < 0:
            break
        i = nxt
    return out


def param_sets(au: bytes) -> tuple[list[bytes], list[bytes]]:
    """The parameter sets in an access unit: they go into the sample entry ONCE.

    That is the difference between an MP4 and a byte stream, and it is why the
    same frames cannot simply be copied across."""
    sps = [n for n in nals(au) if n[0] & 0x1F == NAL_SPS]
    pps = [n for n in nals(au) if n[0] & 0x1F == NAL_PPS]
    return sps, pps


def to_avcc(au: bytes) -> bytes:
    """One access unit as an MP4 sample: each NAL prefixed with its length.

    The parameter sets and the access-unit delimiter are dropped: the first two
    are in the sample entry already, and the delimiter says where an access
    unit begins — which in an MP4 is what the sample boundary says."""
    out = bytearray()
    for nal in nals(au):
        if nal[0] & 0x1F in (NAL_SPS, NAL_PPS, NAL_AUD, NAL_FILLER):
            continue
        out += struct.pack(">I", len(nal)) + nal
    return bytes(out)


def is_key(au: bytes) -> bool:
    """Whether this access unit carries an IDR slice — whether a player may start here."""
    for nal in nals(au):
        t = nal[0] & 0x1F
        if t == NAL_IDR:
            return True
        if t == NAL_SLICE:
            return False
    return False


# -- the boxes ----------------------------------------------------------------------------------------

def _box(kind: str, *parts: bytes) -> bytes:
    """A payload in its header. Built innermost first, so a size is never
    written before what it measures exists."""
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind.encode() + body


def _full(version: int, flags: int) -> bytes:
    return bytes([version]) + flags.to_bytes(3, "big")


_U32, _U16, _U64 = (lambda v: struct.pack(">I", v)), (lambda v: struct.pack(">H", v)), (lambda v: struct.pack(">Q", v))
_MATRIX = b"".join(_U32(v) for v in (0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000))


class Sample:
    """One access unit ready to be written: already length-prefixed."""

    __slots__ = ("data", "duration_ms", "key")

    def __init__(self, data: bytes, duration_ms: int, key: bool):
        self.data, self.duration_ms, self.key = data, duration_ms, key


class Writer:
    """A fragmented MP4 onto a file-like object.

    Holds one fragment's worth of bytes at a time and nothing else: the header
    is written on the first fragment, and every box is complete when written.
    """

    def __init__(self, out, sps: list[bytes], pps: list[bytes], width: int, height: int):
        if not sps or not pps:
            raise ValueError("an MP4 sample entry needs the parameter sets: no SPS/PPS in this interval")
        self.out, self.sps, self.pps = out, sps, pps
        self.width, self.height = width, height
        self.seq, self.decode_time, self._wrote_init = 0, 0, False

    def start_at(self, ms: int) -> None:
        """The decode time the first fragment begins at — an interval taken out
        of the middle of a recording. Zero is what a player wants otherwise."""
        self.decode_time = ms

    def write_fragment(self, samples: list[Sample]) -> None:
        """One `moof` and its `mdat`. A fragment begins at a key frame, and that
        is what makes a long interval seekable without an index."""
        if not samples:
            return
        if not self._wrote_init:
            self.out.write(_ftyp()); self.out.write(self._moov()); self._wrote_init = True
        self.seq += 1
        mdat_size = 8 + sum(len(s.data) for s in samples)
        # The trun's data offset counts from the first byte of the moof, so the moof has to be MEASURED
        # before it can be written. Built twice with the same argument: the size does not depend on the
        # offset's value.
        moof = self._moof(samples, 0)
        moof = self._moof(samples, len(moof) + 8)
        self.out.write(moof)
        self.out.write(struct.pack(">I", mdat_size) + b"mdat")
        for s in samples:
            self.out.write(s.data)
            self.decode_time += s.duration_ms

    def close(self) -> None:
        """There is nothing to finish — that is the whole point of the format."""
        if not self._wrote_init:
            self.out.write(_ftyp()); self.out.write(self._moov()); self._wrote_init = True

    # -- the initialisation segment --
    def _moov(self) -> bytes:
        return _box("moov", self._mvhd(), self._trak(), _mvex())

    def _mvhd(self) -> bytes:
        b = _full(0, 0) + _U32(0) + _U32(0) + _U32(TIMESCALE) + _U32(0)      # duration unknown: a fragmented
        b += _U32(0x00010000) + _U16(0x0100) + bytes(10) + _MATRIX           # file does not know its length
        return _box("mvhd", b + bytes(24) + _U32(TRACK_ID + 1))

    def _trak(self) -> bytes:
        return _box("trak", self._tkhd(), self._mdia())

    def _tkhd(self) -> bytes:
        b = _full(0, 3) + _U32(0) + _U32(0) + _U32(TRACK_ID) + _U32(0) + _U32(0) + bytes(8)
        b += _U16(0) + _U16(0) + _U16(0) + _U16(0) + _MATRIX
        return _box("tkhd", b + _U32(self.width << 16) + _U32(self.height << 16))

    def _mdia(self) -> bytes:
        mdhd = _box("mdhd", _full(0, 0) + _U32(0) + _U32(0) + _U32(TIMESCALE) + _U32(0) + _U16(0x55C4) + _U16(0))
        hdlr = _box("hdlr", _full(0, 0) + _U32(0) + b"vide" + bytes(12) + b"VideoHandler\x00")
        return _box("mdia", mdhd, hdlr, self._minf())

    def _minf(self) -> bytes:
        vmhd = _box("vmhd", _full(0, 1) + bytes(8))
        dinf = _box("dinf", _box("dref", _full(0, 0) + _U32(1) + _box("url ", _full(0, 1))))
        return _box("minf", vmhd, dinf, self._stbl())

    def _stbl(self) -> bytes:
        """Every table empty: in a fragmented file the samples are described by
        the fragments, and these boxes exist because the format requires them."""
        empty = lambda k: _box(k, _full(0, 0) + _U32(0))                       # noqa: E731
        stsz = _box("stsz", _full(0, 0) + _U32(0) + _U32(0))
        return _box("stbl", self._stsd(), empty("stts"), empty("stsc"), stsz, empty("stco"))

    def _stsd(self) -> bytes:
        return _box("stsd", _full(0, 0) + _U32(1) + self._avc1())

    def _avc1(self) -> bytes:
        b = bytes(6) + _U16(1) + bytes(16) + _U16(self.width) + _U16(self.height)
        b += _U32(0x00480000) + _U32(0x00480000) + _U32(0) + _U16(1) + bytes(32) + _U16(0x0018) + b"\xff\xff"
        return _box("avc1", b + self._avcc())

    def _avcc(self) -> bytes:
        """The decoder configuration: the parameter sets, and the fact that every
        NAL in a sample is prefixed with four bytes of length."""
        sps = self.sps[0].ljust(4, b"\x00")
        b = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE0 | len(self.sps)])
        for s in self.sps:
            b += _U16(len(s)) + s
        b += bytes([len(self.pps)])
        for p in self.pps:
            b += _U16(len(p)) + p
        return _box("avcC", b)

    # -- a fragment --
    def _moof(self, samples: list[Sample], data_offset: int) -> bytes:
        mfhd = _box("mfhd", _full(0, 0) + _U32(self.seq))
        return _box("moof", mfhd, self._traf(samples, data_offset))

    def _traf(self, samples: list[Sample], data_offset: int) -> bytes:
        # default-base-is-moof: sample offsets are measured from THIS moof and not from the start of the
        # file, which is what makes a fragment something a player can be handed on its own.
        tfhd = _box("tfhd", _full(0, 0x020000) + _U32(TRACK_ID))
        # tfdt is how a player knows where in the timeline this fragment belongs without having read the
        # ones before it: seeking into the middle of an interval works because of this box.
        tfdt = _box("tfdt", _full(1, 0) + _U64(self.decode_time))
        return _box("traf", tfhd, tfdt, _trun(samples, data_offset))


def _ftyp() -> bytes:
    return _box("ftyp", b"iso5", _U32(512), b"iso5", b"iso6", b"mp41", b"avc1")


def _mvex() -> bytes:
    """Says the movie continues in fragments. Without it a player stops at the
    `moov` and reports a file with no samples in it."""
    trex = _full(0, 0) + _U32(TRACK_ID) + _U32(1) + _U32(0) + _U32(0) + _U32(0)
    return _box("mvex", _box("trex", trex))


def _trun(samples: list[Sample], data_offset: int) -> bytes:
    flags = 0x000001 | 0x000100 | 0x000200 | 0x000400            # offset, duration, size, flags
    b = _full(0, flags) + _U32(len(samples)) + _U32(data_offset)
    for s in samples:
        b += _U32(s.duration_ms) + _U32(len(s.data)) + _U32(sample_flags(s.key))
    return _box("trun", b)


def sample_flags(key: bool) -> int:
    """Whether a player may start here. A key sample depends on nothing and is a
    sync sample; anything else depends on what came before and is not one. A seek
    that lands on a non-sync sample shows nothing until the next key frame."""
    return 2 << 24 if key else (1 << 24) | (1 << 16)
