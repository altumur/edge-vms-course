"""The payload type is the viewer's number, not the stream's.

A payloader fixed in the shared source serves exactly one browser; every other
one is fed packets it silently throws away, with no line in any log. These are
the offers real browsers send, and the number picked out of each."""
from gstvms.payload import h264_payload_type

CHROME = """v=0
m=video 9 UDP/TLS/RTP/SAVPF 96 97 102 103 108 109
a=rtpmap:96 VP8/90000
a=rtpmap:97 rtx/90000
a=rtpmap:102 H264/90000
a=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f
a=rtpmap:103 rtx/90000
a=rtpmap:108 H264/90000
a=fmtp:108 level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42e01f
a=rtpmap:109 H264/90000
a=fmtp:109 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f
"""

FIREFOX = """v=0
m=video 9 UDP/TLS/RTP/SAVPF 120 126 97
a=rtpmap:120 VP8/90000
a=rtpmap:126 H264/90000
a=fmtp:126 profile-level-id=42e01f;level-asymmetry-allowed=1;packetization-mode=1
a=rtpmap:97 H264/90000
a=fmtp:97 profile-level-id=42e01f;level-asymmetry-allowed=1
"""


def test_the_number_is_never_assumed_to_be_96():
    """96 is what the lesson's shared payloader used to send. To Chrome it is VP8."""
    assert h264_payload_type(CHROME) == 102
    assert h264_payload_type(FIREFOX) == 126
    assert h264_payload_type(CHROME) != 96 and h264_payload_type(FIREFOX) != 96


def test_packetization_mode_1_is_preferred():
    """In mode 0 one NAL goes in one packet, and a camera's key frame does not fit."""
    assert h264_payload_type(CHROME) == 102          # 102 is mode 1; 108, a lower-numbered H.264, is mode 0
    only_mode0 = CHROME.replace("packetization-mode=1", "packetization-mode=0")
    assert h264_payload_type(only_mode0) == 102      # nothing better on offer: take what there is


def test_the_profile_breaks_a_tie_and_only_that():
    """Two payload types, both mode 1, different profiles: the one we carry wins."""
    assert h264_payload_type(CHROME, "42") == 102
    assert h264_payload_type(CHROME.replace("42001f", "4d001f"), "4d") == 102
    assert h264_payload_type(CHROME, "64") == 102    # no match: fall back to mode 1, lowest number


def test_numbers_outside_the_dynamic_range_are_not_taken():
    """A browser may name H.264 below 96; `rtph264pay` publishes its pad above it,
    and the link would simply not be made."""
    odd = "a=rtpmap:35 H264/90000\na=fmtp:35 packetization-mode=1\n"
    assert h264_payload_type(odd) is None
    assert h264_payload_type(odd + "a=rtpmap:100 H264/90000\na=fmtp:100 packetization-mode=1\n") == 100


def test_an_offer_without_h264_is_answered_by_nobody():
    assert h264_payload_type("a=rtpmap:96 VP8/90000\na=rtpmap:98 AV1/90000\n") is None
    assert h264_payload_type("") is None
