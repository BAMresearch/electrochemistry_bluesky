# Testing

```sh
uv run --locked pytest                 # everything (≈ 430 tests, < 1 min)
uv run --locked pytest tests/unit      # fast, no processes started
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy src
```

No test touches an instrument. Tests marked `queueserver` start `redis-server`
and an RE Manager and are skipped if redis is not installed, unless
`OE_REQUIRE_REDIS=1` is set (as in CI), which makes a missing redis an error. The device package
is installed as a built distribution (not from its source tree), so the tests
also check that this package uses only its public, installed interface.

## Layout

| Directory | What it tests | How |
|---|---|---|
| `tests/unit/` | Each module in isolation, and the 2460 backend through application code | Pure functions, the simulator, the device's Lua mock; a real RunEngine where a Flyer is involved |
| `tests/contract/` | The pieces working together as a user drives them | A real RE Manager with redis (`test_queueserver_manager.py`); the per-run and viewer notebooks executed cell by cell |

## What covers what

| Area | Main tests |
|---|---|
| Protocol builders, Renard series | `test_builders.py` |
| Run sheets: loading, aggregated errors, template | `test_run_sheet.py` |
| Synthetic cell and its virtual timing | `test_sequence_simulator.py` |
| Compiler and replay against the independent simulator, including noisy and cutoff cases | `test_k2460_compiler_protocols.py` |
| 2460 backend (Lua mock) with the simulator's readings, `run_sequence` and the run file; every equipment key has a place in the run file | `test_k2460_backend_run_file.py` |
| Flyer lifecycle with the simulated backend: streaming, external start, timeouts, abort and drain | `test_sequence_flyer.py` |
| HDF5 run file (SWMR, crashes, repair, v1 and v2) and callbacks | `test_run_file.py`, `test_run_file_v2.py`, `test_sequence_callbacks.py` |
| Recovery from a retained archive | `test_recovery.py` |
| Viewer helpers | `test_viewer.py` |
| Session files, namespace, plans, enqueue, validate | `test_queueserver.py` |
| Tiled store: settings, local server and lock, persistence, a dead server never failing a run, remote key handling, `oe-tiled` | `test_tiled_store.py` (skipped without the `tiled` extra) |
| Real RE Manager: queue of rows, cutoff continues the queue, abort, recover, archive, a local Tiled store started and stopped by the worker | `tests/contract/test_queueserver_manager.py` |
| Notebooks | `test_notebook_helpers.py`, `tests/contract/test_run_notebook.py`, `test_view_notebook.py` |

Compiler golden layouts, the TSP runtime, the transport and the backend's
safety paths are tested in the device package.

## Conventions

- **Independent references.** Expected values come from an independent path,
  not from the code under test: the replay is checked against the simulator,
  the backend against the real TSP under Lua.
- **Real processes.** QueueServer tests run their own redis with a temporary
  `--dir`, so a `dump.rdb` in the repository is never loaded.
- **Synthetic fixtures.** `tests/fixtures/run-file-v1.h5` is a file written by
  the v1 writer, with a synthetic instrument identity.
