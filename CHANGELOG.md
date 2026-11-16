# Changelog

<!-- version list -->

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
