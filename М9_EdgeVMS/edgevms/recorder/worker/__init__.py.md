# __init__.py — the `worker` package docstring

**Role in the module.** Marks `recorder/worker/` as the package `worker` (imported as `worker.config`, `worker.store`, … by the console, the tools and the tests) and states the design in four lines: one process, one shard — a reconcile loop (Lesson 6), fifty GStreamer pipelines (Lesson 7), failure handling and retention (Lesson 8), and the console (Lesson 9) served from the same asyncio loop. "Python touches control, never data." No code; `python3 -m worker` enters through `__main__.py`.
