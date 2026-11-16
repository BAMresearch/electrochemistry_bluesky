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

## Releases

Releases come from `main` through a release pull request (python-semantic-release,
configured in `pyproject.toml`):

1. After the checks pass on `main`, CI opens or updates the PR
   `chore(release): vX.Y.Z` from the commits since the last tag: `feat` gives a
   minor version, `fix` and `perf` a patch (major versions only after 1.0).
   It sets `__version__` and adds the version's section to `CHANGELOG.md`;
   edit the notes on its branch if needed.
2. The same job starts CI on the release branch, whose checks appear on the PR
   (a PR opened with the workflow token starts none by itself). If they are
   missing, run CI on `release/next` from the Actions tab. Then merge it.
3. The merge tags `vX.Y.Z` and creates the GitHub release from the changelog
   section. With the repository variable `PYPI_PUBLISH` set to `true` (and a
   PyPI trusted publisher for this repository's `ci.yml`, environment `pypi`),
   the same run uploads the package to PyPI.
