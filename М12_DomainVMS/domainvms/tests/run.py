#!/usr/bin/env python3
"""Run the millisecond suite without pytest (the appliance image has none):
    python3 tests/run.py
Discovers test_* functions in tests/test_*.py, runs coroutines with
asyncio.run, prints one line per test like Lesson 21 does."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import domain  # noqa: E402,F401  — puts М11 and М10 on sys.path

try:
    import pydantic  # noqa: F401
except ImportError:
    import types
    class _BaseModel:
        def __init__(self, **kw): self.__dict__.update(kw)
    _pyd = types.ModuleType("pydantic"); _pyd.BaseModel = _BaseModel
    sys.modules["pydantic"] = _pyd

try:
    import pytest  # noqa: F401
except ImportError:
    import contextlib
    import types

    class _Skip:
        def __init__(self, cond, reason=""):
            self.name, self.args, self.kwargs = "skipif", (cond,), {"reason": reason}

    @contextlib.contextmanager
    def _raises(exc):
        try:
            yield
        except exc:
            return
        raise AssertionError(f"{exc.__name__} not raised")

    shim = types.ModuleType("pytest")
    shim.mark = types.SimpleNamespace(skipif=_Skip)
    shim.raises = _raises
    sys.modules["pytest"] = shim


class _Monkeypatch:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name))); setattr(obj, name, value)
    def undo(self):
        for obj, name, old in reversed(self._undo): setattr(obj, name, old)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(f for f in os.listdir(here) if f.startswith("test_") and f.endswith(".py"))
    passed = failed = skipped = 0
    for f in files:
        mod = importlib.import_module(f"tests.{f[:-3]}")
        mark = getattr(mod, "pytestmark", None)
        if mark is not None and getattr(mark, "name", "") == "skipif" and mark.args and mark.args[0]:
            print(f"{f}: skipped ({mark.kwargs.get('reason', '')})"); skipped += 1; continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            mp = _Monkeypatch()
            kwargs = {"monkeypatch": mp} if "monkeypatch" in inspect.signature(fn).parameters else {}
            try:
                res = fn(**kwargs)
                if inspect.iscoroutine(res):
                    asyncio.run(res)
                print(f"{f}::{name} ... OK"); passed += 1
            except Exception:                              # noqa: BLE001
                print(f"{f}::{name} ... FAIL"); traceback.print_exc(); failed += 1
            finally:
                mp.undo()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
