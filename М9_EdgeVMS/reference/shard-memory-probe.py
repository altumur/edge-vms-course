#!/usr/bin/env python3
"""
shard-memory-probe.py — measure what sharding actually saves.

The claim in worker-and-process-model.md is that a recording pipeline costs
far less as the Nth pipeline inside a running process than as the 1st pipeline
inside a new one, because the GStreamer plugin registry, the GLib type system
and the thread pools are paid once per PROCESS, not once per camera.

This script measures that instead of asserting it.

    B = process baseline    : GStreamer initialised, zero pipelines
    I = marginal increment  : cost of pipeline N+1 inside a live process

    shared mode, N cameras  ->  B + N*I
    split  mode, N cameras  ->  N*(B + I)
    saving  ->  N*B - B  =  (N-1)*B

Both modes are also measured directly, so the model can be checked against
reality rather than trusted.

WHY PSS AND NOT RSS
-------------------
RSS counts a shared library page once per process, so summing RSS across N
processes double-counts every page of libgstreamer and inflates the split
mode. PSS (proportional set size) divides each shared page by the number of
processes mapping it. Summed PSS is the only honest way to compare "one
process with N pipelines" against "N processes with one pipeline each".
This is the whole reason the naive measurement gets the wrong answer.

WHAT IS AND IS NOT SIMULATED
----------------------------
The pipeline is the real recording shape -- jitter buffer, depayloader,
parser, muxer, file sink -- fed by RTP over loopback UDP rather than by an
RTSP session. What is missing is RTSP setup/teardown and the per-session
socket state, worth a few hundred KB per camera. Nothing that decodes is
present, because recording does not decode. That is the point being made.

The absolute baseline B includes the Python interpreter (~10-12 MB). A
recorder written in C++, Rust or Go has a smaller B and therefore saves
proportionally less. Report B separately for exactly this reason: the shape
of the curve transfers, the constant does not.

USAGE
    ./shard-memory-probe.py --cameras 50
    ./shard-memory-probe.py --cameras 50 --mode shared
    ./shard-memory-probe.py --cameras 100 --settle 20

REQUIREMENTS
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad python3-gi gir1.2-gstreamer-1.0
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BASE_PORT = 55000
CAPS = ('application/x-rtp,media=(string)video,clock-rate=(int)90000,'
        'encoding-name=(string)H264,payload=(int)96')


# --------------------------------------------------------------------------
# memory accounting
# --------------------------------------------------------------------------

def mem_kb(pid="self"):
    """Return (rss_kb, pss_kb) for one process.

    smaps_rollup is the cheap kernel-summed view and exists on Linux >= 4.14.
    Fall back to summing smaps, then to statm (RSS only) if unreadable.
    """
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            rss = pss = 0
            for line in fh:
                if line.startswith("Rss:"):
                    rss = int(line.split()[1])
                elif line.startswith("Pss:"):
                    pss = int(line.split()[1])
            if rss:
                return rss, pss
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/smaps") as fh:
            rss = pss = 0
            for line in fh:
                if line.startswith("Rss:"):
                    rss += int(line.split()[1])
                elif line.startswith("Pss:"):
                    pss += int(line.split()[1])
            if rss:
                return rss, pss
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // 1024, 0
    except OSError:
        return 0, 0


def mb(kb):
    return kb / 1024.0


# --------------------------------------------------------------------------
# pipelines
# --------------------------------------------------------------------------

def sender_command(n, width, height, kbps):
    """One encoder, N loopback destinations. Kept in its own process so its
    memory never lands in the measurement."""
    clients = ",".join(f"127.0.0.1:{BASE_PORT + i}" for i in range(n))
    return [
        "gst-launch-1.0", "-q",
        "videotestsrc", "is-live=true", "pattern=smpte", "!",
        f"video/x-raw,width={width},height={height},framerate=25/1", "!",
        "x264enc", "tune=zerolatency", "speed-preset=ultrafast",
        f"bitrate={kbps}", "key-int-max=50", "!",
        "h264parse", "!",
        "rtph264pay", "config-interval=1", "pt=96", "!",
        "multiudpsink", f"clients={clients}", "sync=false",
    ]


def receiver_description(port, outdir):
    """The recording pipeline. Note what is absent: no decoder, no encoder.
    Recording demuxes and remuxes; it never touches pixels."""
    return (
        f'udpsrc port={port} caps="{CAPS}" ! '
        'rtpjitterbuffer latency=200 ! '
        'rtph264depay ! '
        'h264parse ! '
        'splitmuxsink max-size-time=10000000000 muxer=matroskamux '
        f'location={outdir}/cam{port}-%05d.mkv'
    )


def build_pipelines(count, outdir, Gst):
    made = []
    for i in range(count):
        p = Gst.parse_launch(receiver_description(BASE_PORT + i, outdir))
        p.set_state(Gst.State.PLAYING)
        made.append(p)
    return made


def teardown(pipelines, Gst):
    for p in pipelines:
        p.set_state(Gst.State.NULL)


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def run_shared(n, outdir, settle, step):
    """All N pipelines in one process. Report the baseline and the curve."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    # Force the registry to load so the baseline is honest rather than lazy.
    Gst.ElementFactory.find("rtpjitterbuffer")
    Gst.ElementFactory.find("splitmuxsink")
    time.sleep(1)

    base_rss, base_pss = mem_kb()
    rows = [(0, base_rss, base_pss)]

    pipelines = []
    marks = sorted({1, *range(step, n + 1, step), n})
    for target in marks:
        pipelines += build_pipelines(target - len(pipelines), outdir, Gst)
        time.sleep(settle)
        rss, pss = mem_kb()
        rows.append((target, rss, pss))

    teardown(pipelines, Gst)
    return base_rss, base_pss, rows


def run_one(port, outdir, hold):
    """A single-pipeline worker. Used as the child of split mode."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    Gst.ElementFactory.find("rtpjitterbuffer")
    p = Gst.parse_launch(receiver_description(port, outdir))
    p.set_state(Gst.State.PLAYING)
    print("ready", flush=True)
    time.sleep(hold)
    p.set_state(Gst.State.NULL)


def run_split(n, outdir, settle):
    """N processes, one pipeline each. Sum PSS, never RSS."""
    hold = settle + 30 + n * 0.2
    children = [
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--one",
             "--port", str(BASE_PORT + i), "--outdir", outdir,
             "--hold", str(hold)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for i in range(n)
    ]
    time.sleep(settle + n * 0.15)

    rss = pss = alive = 0
    for c in children:
        if c.poll() is not None:
            continue
        r, p = mem_kb(c.pid)
        if r:
            rss += r
            pss += p
            alive += 1

    for c in children:
        c.send_signal(signal.SIGTERM)
    for c in children:
        try:
            c.wait(timeout=10)
        except subprocess.TimeoutExpired:
            c.kill()

    return rss, pss, alive


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cameras", type=int, default=50)
    ap.add_argument("--mode", choices=("both", "shared", "split"), default="both")
    ap.add_argument("--settle", type=float, default=12.0,
                    help="seconds to let buffers reach steady state (default 12)")
    ap.add_argument("--step", type=int, default=0,
                    help="sample the shared curve every STEP pipelines (default: cameras/5)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--kbps", type=int, default=2000)
    ap.add_argument("--keep", action="store_true", help="keep recorded segments")
    # internal
    ap.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--outdir", help=argparse.SUPPRESS)
    ap.add_argument("--hold", type=float, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one:
        run_one(args.port, args.outdir, args.hold)
        return 0

    if not shutil.which("gst-launch-1.0"):
        sys.exit("gst-launch-1.0 not found -- install gstreamer1.0-tools")
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401
    except Exception as exc:
        sys.exit(f"python GStreamer bindings unavailable: {exc}\n"
                 "install python3-gi gir1.2-gstreamer-1.0")

    n = args.cameras
    step = args.step or max(1, n // 5)
    outdir = tempfile.mkdtemp(prefix="shardprobe-")
    print(f"# {n} cameras @ {args.width}x{args.height} {args.kbps}kbps"
          f"  segments -> {outdir}\n")

    sender = subprocess.Popen(sender_command(n, args.width, args.height, args.kbps),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    if sender.poll() is not None:
        sys.exit("sender failed to start -- is x264enc present? "
                 "(gstreamer1.0-plugins-ugly)")

    base_pss = inc = None
    try:
        if args.mode in ("both", "shared"):
            b_rss, b_pss, rows = run_shared(n, outdir, args.settle, step)
            base_pss = b_pss
            print("SHARED -- one process, N pipelines")
            print(f"{'pipelines':>10} {'RSS MB':>9} {'PSS MB':>9} {'PSS/cam KB':>11}")
            for count, rss, pss in rows:
                per = f"{(pss - b_pss) / count:9.0f}" if count else " " * 9
                print(f"{count:>10} {mb(rss):9.1f} {mb(pss):9.1f} {per:>11}")
            last = rows[-1]
            inc = (last[2] - b_pss) / last[0] if last[0] else 0
            print(f"\n  B (baseline, 0 pipelines) = {mb(b_pss):.1f} MB PSS")
            print(f"  I (marginal per pipeline) = {inc:.0f} KB PSS\n")

        if args.mode in ("both", "split"):
            s_rss, s_pss, alive = run_split(n, outdir, args.settle)
            print(f"SPLIT -- {alive}/{n} processes, one pipeline each")
            print(f"  summed RSS = {mb(s_rss):8.1f} MB   (double-counts shared pages)")
            print(f"  summed PSS = {mb(s_pss):8.1f} MB   <- compare this one")
            if alive:
                print(f"  per camera = {s_pss / alive:8.0f} KB PSS\n")

            if base_pss is not None and inc is not None and alive:
                predicted = base_pss + n * inc
                print("MODEL CHECK")
                print(f"  shared predicted  B + N*I = {mb(predicted):8.1f} MB")
                print(f"  split  measured            = {mb(s_pss):8.1f} MB")
                if predicted:
                    print(f"  sharding saves             = {s_pss / predicted:8.1f}x")
                print(f"  theory: (N-1)*B            = {mb((n - 1) * base_pss):8.1f} MB\n")
    finally:
        sender.send_signal(signal.SIGTERM)
        try:
            sender.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sender.kill()
        if not args.keep:
            shutil.rmtree(outdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
