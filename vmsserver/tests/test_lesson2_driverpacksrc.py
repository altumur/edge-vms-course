"""Lesson 2 — driverpacksrc. The URI resolution is pure; the element itself
needs GStreamer (Track 2) and is skipped without `gi`."""


from gstvms.uri import resolve


def test_uri_resolution_and_the_refusal():
    assert resolve("driverpack://file/lobby.mp4", "/data/media") == "/data/media/lobby.mp4"
    for bad in ("rtsp://10.0.0.7/s", "driverpack://hikvision/10.0.0.7", "driverpack://file/../etc/passwd", "driverpack://file/"):
        try:
            resolve(bad); raise AssertionError(bad)
        except ValueError as e:
            if "hikvision" in bad:
                assert "vendor driver" in str(e)


def test_the_element_runs_when_gstreamer_is_present():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):                     # no gi, or gi without the Gst namespace
        print("  (skipped: no GStreamer here — Track 2)"); return
    import gstvms.driverpacksrc  # noqa: F401
    Gst.init(None)
    assert Gst.ElementFactory.make("driverpacksrc", None) is not None
