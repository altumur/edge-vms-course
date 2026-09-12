#!/usr/bin/env python3
"""Lesson 4, Step 3 — the zombie writer, with real processes and no cameras.

    python3 fencing_demo.py [archive_dir]
    python3 fencing_demo.py --no-fencing     # the same run with one shared path: watch B's files change

  1. start recorder 3 (instance A) writing segments into epoch-000005
  2. kill -STOP it            <- "partitioned, paused, or dead: nobody can tell"
  3. issue epoch 6 by CAS; start instance B writing into epoch-000006
  4. kill -CONT A             <- the zombie wakes up and keeps writing
  5. assert: every file B wrote is intact; A's late files are orphans in a
     path the index never references; nothing was overwritten.

The correctness property has nothing to do with video. A file per segment
and a directory per epoch is the whole mechanism.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from variables import Variables, next_epoch

WRITER = r'''
import os, sys, time, hashlib
node, epoch, root, cam, pathepoch = sys.argv[1], int(sys.argv[2]), sys.argv[3], "cam-7", int(sys.argv[4])
d = os.path.join(root, node, f"epoch-{pathepoch:06d}", cam); os.makedirs(d, exist_ok=True)
i = len(os.listdir(d))                              # "resume": continue the numbering it finds
while True:
    body = f"{node} epoch={epoch} seg={i} {time.time():.3f}".encode() * 64
    p = os.path.join(d, f"seg-{i:05d}.mkv")
    with open(p + ".tmp", "wb") as f: f.write(body)
    os.replace(p + ".tmp", p)                       # a segment is closed atomically
    i += 1; time.sleep(0.05)
'''


def files(root, node, epoch):
    d = os.path.join(root, node, f"epoch-{epoch:06d}", "cam-7")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def main():
    fencing = "--no-fencing" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else tempfile.mkdtemp(prefix="archive-")
    pe = lambda e: e if fencing else 0              # without fencing both instances share epoch-000000
    v = Variables()
    node = "node-3"

    epoch_a, _ = next_epoch(v, node)                       # epoch 1..: seed to 5 for the story
    while epoch_a < 5:
        epoch_a, _ = next_epoch(v, node)
    a = subprocess.Popen([sys.executable, "-c", WRITER, node, str(epoch_a), root, str(pe(epoch_a))])
    time.sleep(0.6)
    n_before = len(files(root, node, pe(epoch_a)))
    print(f"1. instance A (pid {a.pid}) recording into epoch-{epoch_a:06d}: {n_before} segments")

    os.kill(a.pid, signal.SIGSTOP)
    print(f"2. kill -STOP {a.pid}   (Nomad cannot tell this from dead)")
    time.sleep(0.3)

    epoch_b, idx = next_epoch(v, node)
    b = subprocess.Popen([sys.executable, "-c", WRITER, node, str(epoch_b), root, str(pe(epoch_b))])
    time.sleep(0.6)
    index = {"node": node, "epoch": epoch_b, "segments": files(root, node, pe(epoch_b))}   # the live index
    print(f"3. epoch {epoch_b} issued (ModifyIndex {idx}); instance B (pid {b.pid}) recording into "
          f"epoch-{epoch_b:06d}: {len(index['segments'])} segments indexed")

    os.kill(a.pid, signal.SIGCONT)
    print(f"4. kill -CONT {a.pid}   (the zombie wakes and keeps writing)")
    time.sleep(0.6)
    a.terminate(); b.terminate(); a.wait(); b.wait()
    b_after = files(root, node, pe(epoch_b))

    # 5. the proof: is every segment the index names still B's?
    overwritten = []
    for name in index["segments"]:
        with open(os.path.join(root, node, f"epoch-{pe(epoch_b):06d}", "cam-7", name), "rb") as f:
            if not f.read(32).startswith(f"{node} epoch={epoch_b}".encode()):
                overwritten.append(name)
    json.dump(index, open(os.path.join(root, "index.json"), "w"))
    if fencing:
        orphans = len(files(root, node, epoch_a)) - n_before
        assert orphans > 0 and not overwritten
        print(f"5. after the zombie woke: A wrote {orphans} more segments into epoch-{epoch_a:06d} "
              f"— orphans nobody indexes; B's {len(b_after)} segments in epoch-{epoch_b:06d} untouched, "
              f"contents verified")
        print(f"\n{root}/{node}/")
        for e in (epoch_a, epoch_b):
            print(f"  epoch-{e:06d}/cam-7/   {len(files(root, node, e)):3d} files   "
                  f"{'<- the index points here' if e == epoch_b else '<- re-indexed as fenced, then retention'}")
        print("\nYou cannot stop a zombie from writing. You can only make its writes harmless.")
    else:
        print(f"5. after the zombie woke, ONE shared path: {len(overwritten)} of the {len(index['segments'])} "
              f"segments the index names now contain instance A's data — silently. The index is a lie, "
              f"the console will play epoch-5 footage under epoch-6 timestamps, and nothing raised an error.")
        assert overwritten, "without fencing the zombie must have clobbered something"


if __name__ == "__main__":
    main()
