"""The two GStreamer elements and the actuator that uses them. Needs
PyGObject and GStreamer (python3-gi, gst-plugins-good/bad) — Track 2. The
tests skip this package when `gi` is absent; the logic it calls
(vms.archive) is tested without it.

    driverpacksrc   uri=driverpack://file/<name>: a file from MEDIA_DIR, looping, timestamps rebased
    archivesink     splitmuxsink into the spool; on fragment-closed, promote to the archive resource
    webrtc          the gateway's media path: one udpsrc/tee per camera, one webrtcbin per viewer, WHEP
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # __init__.py — the package docstring: the two GStreamer elements and the actuator that uses them (Track
# 2)
#
# **Role in the module.** No code, only the docstring. `gstvms` is the part of the VMS that needs a media
# stack: PyGObject and GStreamer (`python3-gi`, `gst-plugins-good/bad` — the Containerfile installs exactly
# these). The docstring names the two elements: `driverpacksrc` (`uri=driverpack://file/<name>`: a file from
# `MEDIA_DIR`, looping, timestamps rebased) and `archivesink` (splitmuxsink into the spool; on
# fragment-closed, promote to the archive resource). The tests skip this package when `gi` is absent
# (`test_lesson2_driverpacksrc.py::test_the_element_runs_when_gstreamer_is_present` prints a skip), and the
# logic it calls — `vms.archive.ArchiveResource.promote`, `gstvms.uri.resolve` — is tested without it.
#
# ## Module-level names
# None, and no imports: importing `gstvms` does not import `gi`. `gstvms.uri` stays importable everywhere;
# `gstvms.driverpacksrc`, `gstvms.archivesink` and `gstvms.actuator` each `import gi` at the top, which is
# why `vms/__main__.worker` wraps `from gstvms.actuator import GstActuator` in `try/except ImportError` and
# falls back to `FakeActuator`.
#
# ## Notes
# - Dependency direction: `gstvms` imports from `vms` (`archivesink` uses `vms.archive`), never the other
#   way round at import time; the worker reaches `gstvms` only through that guarded import in `__main__`.
# ================================================================================================
