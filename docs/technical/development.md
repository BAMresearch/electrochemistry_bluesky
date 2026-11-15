# Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then in
the repository:

```sh
uv sync --locked --group dev --extra queueserver
```

| Task | Command |
|---|---|
| Tests, lint, types | see [Testing](testing.md) |
| Strict docs build | `uv run --locked mkdocs build --strict` |
| Distributions | `uv build` |

CI (`.github/workflows/ci.yml`) runs on every push and pull request: lint,
types and the strict docs build; the tests on Python 3.11–3.14 (Linux) and 3.14
(macOS), with redis installed and required (`OE_REQUIRE_REDIS=1`), so the RE
Manager tests run instead of skipping; and a build whose wheel must hold the
notebook templates and run `oe-template`, `oe-validate` and `oe-new-viewer`
from outside the source tree.

The development Python is 3.14 (`.python-version`); the package declares 3.11
or later. After changing dependencies run `uv lock` and commit `pyproject.toml`
and `uv.lock` together.

`keithley2460-ophyd` is a normal versioned dependency. Until it is on PyPI,
`[tool.uv.sources]` installs a tagged release from its GitHub repository; move
the tag (and run `uv lock`) to pick up a new device release, and remove the
source once PyPI has it. To test against a local device checkout, point the
source temporarily at `{ path = "../keithley2460_ophyd" }` (a built,
non-editable install) and do not commit that. Never import the device package
from its source tree, and do not copy its code here: fixes to the instrument
layer go to that repository.

Commits follow Conventional Commits. Keep the changelog's
`<!-- version list -->` marker; do not bump versions by hand.

## Run files and privacy

Run files deliberately record provenance: the session file's full text, the
host name, the run sheet's path, the instrument identity and package versions.
Review them before sharing data outside your group, and remove or replace those
fields in an exported copy if needed.
