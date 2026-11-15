# Changelog

<!-- version list -->

## Unreleased

Initial public release, extracted from an internal development repository and
built on `keithley2460-ophyd`:

- Protocol builders, Excel run sheets (`oe-template`, `oe-validate`) and the
  `RunRequest` that carries sample ID, notes and the run-sheet row.
- `run_sequence` plan, QueueServer startup and permissions (`oe-enqueue`,
  `oe-archive-retained`), HDF5 run files (v2 layout; v1 still readable),
  live plot, viewer, `oe-recover-run`, notebooks (`oe-new-run`, `oe-new-viewer`).
- Synthetic-cell simulator and `SimulatedSequenceBackend`.
- Run-sheet directions are `charge` and `discharge`; a session's optional
  `[directions]` table adds a lab's own words for them (e.g. `sodiation`).
