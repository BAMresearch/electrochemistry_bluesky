# Changelog

<!-- version list -->

## v0.2.0 (2026-11-17)

### Bug Fixes

- **tiled**: Apply the review of the Tiled store
  ([`ca8c6c1`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/ca8c6c17af5e8b9072bdf989db73a79d5d0aec0d))

### Continuous Integration

- Say what GitHub does with checks of a workflow-token PR
  ([`90bd2d7`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/90bd2d730bef49a84f6ca156a47adb35353bab9f))

- Start CI on the release branch for the release PR
  ([`66ec031`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/66ec03128c2c61474192830c26ecd626b7d26c9d))

### Features

- **session**: Name the set-up in every run's start document
  ([`49fe036`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/49fe036e9b40808566d0ab1daac1af4f3f8df16e))

- **tiled**: Rows reach the store within flush_s; SQLite tables for parallel readers
  ([`0cad81d`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/0cad81d9d0ea5cfbdd1f9f7181dcd3e70b7ed5d8))

### Testing

- **queueserver**: Pause the abort test only once the run has opened
  ([`b9f9bd6`](https://github.com/BAMresearch/electrochemistry_bluesky/commit/b9f9bd6a2eb90f6a1b572624092fa41e3afeb6ea))


## v0.1.0 (2026-11-16)

- Initial Release

Initial public release, extracted from an internal development repository and
built on `keithley2460-ophyd`:

- Protocol builders, Excel run sheets (`oe-template`, `oe-validate`) and the
  `RunRequest` that carries sample ID, notes and the run-sheet row.
- `run_sequence` plan, QueueServer startup and permissions (`oe-enqueue`,
  `oe-archive-retained`), HDF5 run files (v2 layout; v1 still readable),
  live plot, viewer, `oe-recover-run`, notebooks (`oe-new-run`, `oe-new-viewer`).
- Synthetic-cell simulator and `SimulatedSequenceBackend`.
- Optional Tiled store (`tiled` extra, session `[tiled]` table): a local
  persistent store in `data_dir/tiled` or an existing server; `oe-tiled`.
- Run-sheet directions are `charge` and `discharge`; a session's optional
  `[directions]` table adds a lab's own words for them (e.g. `sodiation`).
