"""Which number a viewer's H.264 is, in this viewer's offer.

A payload type is not a property of the stream: it is a LABEL the receiver
assigns in its offer, and an answer may use only numbers the offer named.
Chrome's 96 is VP8; its H.264 lives at 102, 108, 116, 118 and further, a pair
per profile and packetization mode, and the numbers differ between browsers
and between versions of one browser.

That is why this is a function of the offer and why the payloader belongs to
the viewer's branch rather than to the shared source: two browsers watching
one camera do not agree on the number, and one payloader fixed at one number
cannot serve them both.
"""
from __future__ import annotations

import re

RTPMAP = re.compile(r"^a=rtpmap:(\d+)\s+(\S+)", re.I)
FMTP = re.compile(r"^a=fmtp:(\d+)\s+(.*)$", re.I)


def h264_payload_type(offer: str, want_profile: str | None = None) -> int | None:
    """The best H.264 payload type in `offer`, or None if it offers no H.264.

    Best is: packetization-mode=1 and the profile we carry, then mode 1, then
    anything. Mode 1 matters because in mode 0 one NAL goes in one packet and a
    camera's key frame does not fit. Only 96..127 are considered: a browser may
    name H.264 below the dynamic range, but `rtph264pay` publishes its source
    pad above it, and the link simply would not be made.
    """
    found: dict[int, dict] = {}
    for line in offer.splitlines():
        m = RTPMAP.match(line.strip())
        if m and m.group(2).upper().startswith("H264/"):
            n = int(m.group(1))
            if 96 <= n <= 127:
                found[n] = {"mode1": False, "profile": ""}
    for line in offer.splitlines():
        m = FMTP.match(line.strip())
        if not m or int(m.group(1)) not in found:
            continue
        params, e = m.group(2), found[int(m.group(1))]
        e["mode1"] = "packetization-mode=1" in params
        for kv in params.split(";"):
            k, _, v = kv.strip().partition("=")
            if k == "profile-level-id" and len(v) >= 2:
                e["profile"] = v[:2].lower()
    for pick in (lambda e: e["mode1"] and bool(want_profile) and e["profile"] == want_profile,
                 lambda e: e["mode1"],
                 lambda e: True):
        for n in sorted(found):
            if pick(found[n]):
                return n
    return None
