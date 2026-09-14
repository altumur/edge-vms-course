# __main__.py — `python3 -m worker` entry point

**Role in the module.** Four lines: import `main` from `worker.worker` and `asyncio.run(main())`. This is the `CMD` of `recorder/Containerfile` and the last step of the README's bench recipe. Everything — settings, the column key, the Postgres pool, the four tasks and the console — is set up inside `worker.main()`; the process exits when `Worker.run()` returns after SIGTERM/SIGINT.
