# __init__.py — the `console` package marker

**Role in the module.** Empty. Makes `recorder/console/` importable as `console` (`console.app`, `console.auth`) from the `recorder/` directory, which is `/app` in the container and the cwd on the bench. `worker/worker.py` imports `console.app.create_app` lazily inside `Worker.run()`; `tools/provision.py` imports `console.auth.hash_password`.
