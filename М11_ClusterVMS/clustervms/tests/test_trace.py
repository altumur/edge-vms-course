"""The trace the lessons print is the trace `NomadVariables` would send — checked, not asserted.

The same operations go once through the real `NomadVariables`, against a small HTTP server that records
what arrives and answers the way Nomad does, and once through `TracedVariables` over `FakeVariables`. The
requests must be the same: method, path, query, body.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cluster.variables import Conflict, FakeVariables, NomadVariables
from tests.trace import TraceLog, TracedVariables


class _Nomad:
    """Just enough of Nomad's Variables API to answer `NomadVariables`: a dict, an index, and CAS."""

    def __init__(self):
        self.items, self.index, self.seen = {}, 1000, []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                me.seen.append(("GET", self.path, None))
                path, _, query = self.path.partition("?")
                if path == "/v1/vars":
                    prefix = dict(p.split("=", 1) for p in query.split("&"))["prefix"]
                    return self._answer(200, [{"Path": k, "ModifyIndex": v[1]} for k, v in sorted(me.items.items()) if k.startswith(prefix)])
                key = path[len("/v1/var/"):]
                if key not in me.items:
                    return self._answer(404)
                return self._answer(200, {"Path": key, "Items": me.items[key][0], "ModifyIndex": me.items[key][1]})

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.seen.append(("PUT", self.path, body))
                path, _, query = self.path.partition("?")
                key, q = path[len("/v1/var/"):], dict(p.split("=", 1) for p in query.split("&"))
                current = me.items.get(key, (None, 0))[1]
                if "cas" in q and int(q["cas"]) != current:
                    return self._answer(409, {"ModifyIndex": current})
                me.index += 1
                me.items[key] = (body["Items"], me.index)
                return self._answer(200, {"Path": key, "ModifyIndex": me.index})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addr = f"http://127.0.0.1:{self.srv.server_address[1]}"


def _script(v):
    v.put("vms/cameras/7", {"name": "Ворота", "labels": "vlan:cctv-a"}, cas=0)
    items, idx = v.get("vms/cameras/7")
    v.put("vms/cameras/7", {**items, "name": "Северные ворота"}, cas=idx)
    try:
        v.put("vms/cameras/7", {"name": "stale"}, cas=idx)           # the second writer, with the old index
    except Conflict:
        pass
    v.list("vms/cameras/")
    v.get("vms/cameras/8")


def test_the_trace_is_what_nomad_variables_sends():
    nomad = _Nomad()
    _script(NomadVariables(addr=nomad.addr, token="t"))
    log = TraceLog()
    _script(TracedVariables(FakeVariables(), log, who="console"))
    real = [(m, p, b) for m, p, b in nomad.seen]
    traced = [(c.method, c.url, c.body) for c in log.calls]
    assert real == traced
    assert [c.status for c in log.calls] == [200, 200, 200, 409, 200, 404]
    nomad.srv.shutdown()


def test_an_object_is_shown_as_the_json_it_carries():
    """A heartbeat on this cluster is a Variable `objects/vms/heartbeats/w-0` whose `data` is a JSON string.
    The trace shows the JSON — the request is unchanged, only its printing is."""
    from cluster.objectstore import VariablesObjectStore
    log = TraceLog()
    VariablesObjectStore(TracedVariables(FakeVariables(), log, who="vmsworker w-0")).put(
        "vms/heartbeats/w-0", json.dumps({"worker": "w-0", "capacity": 50}).encode())
    text = log.render()
    assert "PUT /v1/var/objects/vms/heartbeats/w-0?namespace=default" in text
    assert '"data": {"worker": "w-0", "capacity": 50}' in text
