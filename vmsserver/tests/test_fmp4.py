"""An interval of the archive, turned into something a browser plays.

The tests read the boxes back out: a muxer nobody parses is a muxer that is
wrong in a way only a browser will tell you about, silently, with a black
picture."""
import io
import struct

from vms.fmp4 import Sample, Writer, is_key, nals, param_sets, sample_flags, to_avcc

SPS = bytes([0x67, 0x42, 0xE0, 0x1F, 0xAA, 0xBB])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x11, 0x22, 0x33])
SLICE = bytes([0x41, 0x44, 0x55])
AUD = bytes([0x09, 0x10])


def au(*units, four=True):
    """An Annex-B access unit out of NAL units, with 4- or 3-byte start codes."""
    start = b"\x00\x00\x00\x01" if four else b"\x00\x00\x01"
    return b"".join(start + u for u in units)


def boxes(buf: bytes, at: int = 0, end: int | None = None):
    """[(kind, start, size)] at one level of the tree."""
    out, end = [], len(buf) if end is None else end
    while at + 8 <= end:
        size = struct.unpack(">I", buf[at:at + 4])[0]
        out.append((buf[at + 4:at + 8].decode(), at, size))
        if size < 8:
            break
        at += size
    return out


# Two boxes carry fixed fields before their children: `stsd` a full-box header and an entry count, `avc1`
# the 78 bytes of a visual sample entry. A reader that does not know this finds nothing inside them.
SKIP = {"stsd": 8, "avc1": 78}


def find(buf, path, at=0, end=None):
    """The payload of a box by path, e.g. find(init, "moov/trak/mdia")."""
    for kind in path.split("/"):
        for k, start, size in boxes(buf, at, end):
            if k == kind:
                at, end = start + 8 + SKIP.get(k, 0), start + size
                break
        else:
            raise AssertionError(f"no {kind} in {path}")
    return buf[at:end], at, end


def test_start_codes_of_both_lengths_and_the_zeroes_before_them():
    """A camera may use either, and need not be consistent inside one unit."""
    assert nals(au(SPS, PPS, IDR)) == [SPS, PPS, IDR]
    assert nals(au(SPS, PPS, IDR, four=False)) == [SPS, PPS, IDR]
    mixed = b"\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + IDR
    assert nals(mixed) == [SPS, IDR]
    assert nals(b"") == [] and nals(b"\x00\x00\x00\x01") == []


def test_a_sample_is_length_prefixed_and_carries_no_parameter_sets():
    """They live in the sample entry once; repeated in every key frame they are
    bytes the browser has to skip."""
    s = to_avcc(au(AUD, SPS, PPS, IDR))
    assert s == struct.pack(">I", len(IDR)) + IDR
    assert SPS not in s and PPS not in s
    two = to_avcc(au(SLICE, SLICE))
    assert two == struct.pack(">I", len(SLICE)) + SLICE + struct.pack(">I", len(SLICE)) + SLICE


def test_a_key_frame_is_one_a_player_may_start_at():
    assert is_key(au(SPS, PPS, IDR)) and not is_key(au(SLICE))
    assert not is_key(au(AUD))                       # nothing decidable: not a place to start
    assert sample_flags(True) == 2 << 24             # depends on nothing, and IS a sync sample
    assert sample_flags(False) == (1 << 24) | (1 << 16)


def test_the_parameter_sets_come_out_of_the_stream_and_go_into_avcc():
    sps, pps = param_sets(au(SPS, PPS, IDR))
    assert sps == [SPS] and pps == [PPS]
    out = io.BytesIO(); Writer(out, sps, pps, 1920, 1080).close()
    init = out.getvalue()
    assert [k for k, _, _ in boxes(init)] == ["ftyp", "moov"]
    avcc, _, _ = find(init, "moov/trak/mdia/minf/stbl/stsd/avc1/avcC")
    assert avcc[0] == 1 and avcc[1:4] == SPS[1:4]    # configurationVersion, then the profile out of the SPS
    assert avcc[4] == 0xFF                           # four-byte NAL lengths: the shape to_avcc writes
    assert SPS in avcc and PPS in avcc
    assert find(init, "moov/mvex/trex")              # without mvex a player reports a file with no samples


def test_every_fragment_says_where_it_belongs_without_the_ones_before_it():
    out = io.BytesIO()
    w = Writer(out, [SPS], [PPS], 640, 480)
    w.start_at(90_000)                                # an interval from the middle of a recording
    w.write_fragment([Sample(to_avcc(au(SPS, PPS, IDR)), 40, True), Sample(to_avcc(au(SLICE)), 40, False)])
    w.write_fragment([Sample(to_avcc(au(SPS, PPS, IDR)), 40, True)])
    buf = out.getvalue()
    kinds = [k for k, _, _ in boxes(buf)]
    assert kinds == ["ftyp", "moov", "moof", "mdat", "moof", "mdat"]

    tfdts = []
    for k, start, size in boxes(buf):
        if k != "moof":
            continue
        tfdt, _, _ = find(buf, "traf/tfdt", start + 8, start + size)
        tfdts.append(struct.unpack(">Q", tfdt[4:12])[0])
    assert tfdts == [90_000, 90_080], "the second fragment must begin where the first one's samples ended"


def test_the_data_offset_points_at_the_first_byte_of_the_mdat():
    """Measured from the moof, and wrong by eight bytes plays nothing at all."""
    out = io.BytesIO()
    w = Writer(out, [SPS], [PPS], 640, 480)
    payload = to_avcc(au(SPS, PPS, IDR))
    w.write_fragment([Sample(payload, 40, True)])
    buf = out.getvalue()
    moof = next((start, size) for k, start, size in boxes(buf) if k == "moof")
    trun, _, _ = find(buf, "traf/trun", moof[0] + 8, moof[0] + moof[1])
    count, offset = struct.unpack(">II", trun[4:12])
    assert count == 1
    assert buf[moof[0] + offset:moof[0] + offset + len(payload)] == payload
    dur, size, flags = struct.unpack(">III", trun[12:24])
    assert (dur, size, flags) == (40, len(payload), 2 << 24)


def test_an_interval_without_parameter_sets_is_refused_rather_than_written():
    """A file whose sample entry has no SPS is a file that plays nothing, and it
    is better to say so where the interval is assembled."""
    try:
        Writer(io.BytesIO(), [], [], 640, 480)
        raise AssertionError("accepted an interval with no parameter sets")
    except ValueError as e:
        assert "parameter sets" in str(e)
