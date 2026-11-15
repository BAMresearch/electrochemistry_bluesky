# Contributing

- Keep the instrument layer in `keithley2460-ophyd`: no copies of its code, TSP
  or safety logic here. Fixes there go to that repository; this package uses
  its public, installed interface.
- Settings belong in one place: per run in the run sheet, per environment in the
  session file, per instrument in `SequencerProfile` (device package).
- An aborted run is never reported as completed; retained instrument data are
  archived before they are discarded or overwritten.
- Do not change the meaning of fields in existing run files; version the run-file
  schema when the layout changes and keep older versions readable.
- Review the user guide and the notebook templates when a run-sheet column,
  session setting, command or run-file field changes.
- Example sessions and fixtures contain no real instrument addresses, serial
  numbers, sample names or paths.
- Run the tests, ruff and mypy before opening a pull request
  ([Testing](docs/technical/testing.md)). Commits follow Conventional Commits; do
  not bump versions by hand.
