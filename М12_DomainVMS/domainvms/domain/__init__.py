"""DomainVMS — М12. The smallest layer that can sit above a set of clusters,
be switched off, and be the top of the product.

Built on М11's clustervms (imported, not copied) and through it on М10's
vmsserver. Exactly three things a cluster cannot know — lookup across
clusters, which cluster gets a camera, whether an answer is complete — plus
the discipline of a layer that may be down: a signer that is the top of
its own trust, identity that never reaches a worker, grants that expire
per cluster, a server that joins with nobody typing a secret, and a read
model that says how old it is.
"""
import os
import sys

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_course = os.path.dirname(os.path.dirname(_here))
for env, cands in (("CLUSTERVMS_PATH", (os.path.join(_here, "clustervms"),
                                        os.path.join(_course, "М11_ClusterVMS", "clustervms"))),):
    for cand in (os.environ.get(env, ""),) + cands:
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.append(cand)          # clustervms/cluster/__init__ then finds vmsserver itself
            break
import cluster  # noqa: E402,F401  — М11 on sys.path, and М10 through it
