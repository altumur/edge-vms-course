"""The request trace: what a cluster's processes say to Nomad, in Nomad's own words.

The lessons show every store call as the HTTP request `NomadVariables` would send for it — the method, the
URL with its `?cas=`, the body, the answer — and the Variables before and after. Written by hand, those
examples drift from the code the first time the code changes. So they are not written by hand: the stand
runs the real controller, workers and console over `FakeVariables`, and `TracedVariables` records each call
in the form `NomadVariables` would have put on the wire, built with the same `safe_path` and `quote`.

    log = TraceLog(roots={"/tmp/clustervms-x1": "/data"})
    vars_ = TracedVariables(FakeVariables(), log, who="console")
    ...
    print(log.render())

Objects on this cluster ARE Variables (`VariablesObjectStore`, `objects/<key>` -> {data}), so an object
store built over a traced Variables shows up in the same trace, with its `data` expanded for reading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import quote

from cluster.variables import Conflict, Forbidden
from w2cplatform.variables import safe_path


@dataclass
class Call:
    who: str
    method: str
    url: str
    body: dict | None
    status: int
    answer: object


@dataclass
class TraceLog:
    roots: dict = field(default_factory=dict)          # temporary paths -> what the lesson shows instead
    calls: list = field(default_factory=list)
    namespace: str = "default"

    def mark(self) -> int:
        return len(self.calls)

    def _clean(self, s: str) -> str:
        for real, shown in self.roots.items():
            s = s.replace(real, shown)
        return s

    @staticmethod
    def _expand(obj):
        """An object stored as a Variable carries its bytes as a JSON string in `data`: show it as JSON."""
        if isinstance(obj, dict) and isinstance(obj.get("Items"), dict) and "data" in obj["Items"]:
            try:
                return {**obj, "Items": {**obj["Items"], "data": json.loads(obj["Items"]["data"])}}
            except ValueError:
                return obj
        return obj

    def render(self, since: int = 0, until: int | None = None, who: bool = True, width: int = 110,
               methods: tuple | None = None) -> str:
        out = []
        for c in self.calls[since:until]:
            if methods and c.method not in methods:
                continue
            if who:
                out.append(f"# {c.who}")
            out.append(f"{c.method} {self._clean(c.url)}")
            if c.body is not None:
                out.append(self._clean(json.dumps(self._expand(c.body), ensure_ascii=False, indent=2 if len(json.dumps(c.body)) > width else None)))
            ans = self._expand(c.answer) if isinstance(c.answer, dict) else c.answer
            text = "" if ans is None else " " + json.dumps(ans, ensure_ascii=False, indent=2 if len(json.dumps(ans)) > width else None)
            out.append(self._clean(f"→ {c.status}{text}"))
            out.append("")
        return "\n".join(out).rstrip() + "\n"


class TracedVariables:
    """A Variables that records every call as the request `NomadVariables` would have sent."""

    def __init__(self, inner, log: TraceLog, who: str):
        self.inner, self.log, self.who = inner, log, who
        self.max_bytes = getattr(inner, "max_bytes", 0)

    def _key(self, path: str) -> str:
        return quote(safe_path(path), safe="/")

    def _q(self, cas=None) -> str:
        return f"namespace={self.log.namespace}" + (f"&cas={cas}" if cas is not None else "")

    def as_writer(self, writer: str, allowed=None):
        return TracedVariables(self.inner.as_writer(writer, allowed), self.log, writer)

    def get(self, path):
        items, idx = self.inner.get(path)
        url = f"/v1/var/{self._key(path)}?{self._q()}"
        if items is None:
            self.log.calls.append(Call(self.who, "GET", url, None, 404, None))
        else:
            self.log.calls.append(Call(self.who, "GET", url, None, 200, {"Path": path, "Items": items, "ModifyIndex": idx}))
        return items, idx

    def put(self, path, items, cas=None):
        url = f"/v1/var/{self._key(path)}?{self._q(cas)}"
        body = {"Items": {k: str(v) for k, v in items.items()}}
        try:
            idx = self.inner.put(path, items, cas)
        except Conflict:
            _, current = self.inner.get(path)
            self.log.calls.append(Call(self.who, "PUT", url, body, 409, {"ModifyIndex": current}))
            raise
        except Forbidden:
            self.log.calls.append(Call(self.who, "PUT", url, body, 403, "Permission denied"))
            raise
        self.log.calls.append(Call(self.who, "PUT", url, body, 200, {"Path": path, "ModifyIndex": idx}))
        return idx

    def list(self, prefix):
        paths = self.inner.list(prefix)
        url = f"/v1/vars?prefix={quote(prefix, safe=chr(47))}&{self._q()}"
        answer = [{"Path": p, "ModifyIndex": self.inner.get(p)[1]} for p in paths]
        self.log.calls.append(Call(self.who, "GET", url, None, 200, answer))
        return paths

    def delete(self, path, cas=None):
        url = f"/v1/var/{self._key(path)}?{self._q(cas)}"
        try:
            self.inner.delete(path, cas)
        except Conflict:
            self.log.calls.append(Call(self.who, "DELETE", url, None, 409, None))
            raise
        self.log.calls.append(Call(self.who, "DELETE", url, None, 200, None))

    def __getattr__(self, name):
        return getattr(self.inner, name)
