# pyproject.toml — the recorder package: runtime dependencies, test extras, pytest settings

**Role.** Package metadata for `recorder/`. `pip install -e '.[test]'` on the bench (README step 2) installs the runtime and test dependencies; the Containerfile copies it but installs the same list by hand. Also configures pytest so the async tests run without decorators.

## Stanza by stanza
### `[project]`
- `name = "recorder"`, `version = "0.10.0"` — the console reports `version="0.10"` in its FastAPI title; М9 is the tenth course module in that scheme.
- `description = "М9 Recorder — one recorder learns what it should be"`.
- `requires-python = ">=3.10"` — `X | None` unions and `str.removeprefix` are used throughout.
- `dependencies` — `asyncpg>=0.29` (`store.py`), `fastapi>=0.110` (`console/app.py`, pydantic v2 `field_validator`), `uvicorn>=0.29` (served from the worker's loop), `argon2-cffi>=23` (`console/auth.py`), `cryptography>=42` (`secrets.py` AES-GCM). The comment records that GStreamer comes from the OS: `python3-gi` and the plugin packages.

### `[project.optional-dependencies]`
- `test = ["pytest>=8", "pytest-asyncio>=0.23", "httpx>=0.27"]` — pytest and its asyncio plugin for the `async def test_*` functions; `httpx` for FastAPI's test client (no shipped test uses it yet; the HTTP layer is listed as unexecuted in the README).

### `[tool.pytest.ini_options]`
- `asyncio_mode = "auto"` — every coroutine test is run by pytest-asyncio without `@pytest.mark.asyncio`; `tests/run.py` mirrors this by `asyncio.run`-ing coroutines when pytest is absent.
- `testpaths = ["tests"]`.
