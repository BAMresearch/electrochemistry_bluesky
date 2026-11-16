# electrochemistry-bluesky

[![CI](https://github.com/BAMresearch/electrochemistry_bluesky/actions/workflows/ci.yml/badge.svg)](https://github.com/BAMresearch/electrochemistry_bluesky/actions/workflows/ci.yml)

Bluesky control of battery experiments on a Keithley 2460 SourceMeter:
galvanostatic and potentiostatic cycling, state-of-charge positioning, and
current pulses with rests. The instrument runs each sequence on its own
(timing, voltage cutoffs, step markers, output OFF at the end); the computer
prepares, starts and reads it. Usable at the bench and alongside other
instruments through the 2460's START, READY, BUSY and MARKER lines.

```text
Excel run sheet + session file (limits)
        │
Bluesky QueueServer      queues one plan per run-sheet row
        │
Bluesky RunEngine        run_sequence plan ──► HDF5 run file, live plot
        │
SequenceFlyer            ┐
        │                │ keithley2460-ophyd
2460 sequencer backend   ┘   ─or─   simulated backend (synthetic cell, this package)
        │
TSP runtime on the 2460  trigger model: timing, cutoffs, markers, output OFF
```

- **Protocols and run sheets.** `rest`, `soc_position`, `pulse_train`,
  `bipolar_pulse` and `cycling` (with an optional CV hold), written as rows of an
  Excel workbook and validated against the limits in a session file (TOML).
- **QueueServer deployment.** A startup that turns run-sheet rows into queued
  runs; the RE Manager worker is the only owner of the instrument.
- **Data.** One HDF5 file per run (NeXus-style, readable while it is written)
  with every reading, the events and the full settings; recovery of a run whose
  host crashed; a viewer notebook to follow and compare runs.
- **Optional Tiled store** of every run's documents, either a local persistent
  store beside the run files or an existing Tiled server.
- **A simulator** with a synthetic cell, so the whole stack runs and is tested
  without hardware.

The instrument layer (Flyer, sequence models, 2460 backend and its packaged TSP
runtime) is the separate package
[keithley2460-ophyd](https://github.com/BAMresearch/keithley2460_ophyd).

**Status:** alpha; not yet released on PyPI. The sequencer path has run on a
2460 with a resistor and with electrochemical cells.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/), git and
redis. uv installs Python 3.14 itself.

```sh
git clone https://github.com/BAMresearch/electrochemistry_bluesky.git
cd electrochemistry_bluesky
uv sync --locked --extra visa --extra queueserver
source .venv/bin/activate
oe-template runs.xlsx
oe-validate runs.xlsx --session qserver/session-simulator.toml
```

The [Quickstart](docs/user/quickstart.md) continues from there: run the rows
through the QueueServer on the simulator and look at the data.

## Safety

Each session file sets an absolute voltage window and a maximum current; every
row is validated against them before anything reaches the instrument, and the
2460 stops the run with the output OFF if a reading leaves the window. Output-off
mode is HIGH-Z. **Loading a session is the confirmation for every run in it:**
load one only for the instrument, wiring and cell actually connected. The example
session in `qserver/` is deliberately not runnable until you fill it in. See
[Sessions](docs/user/sessions.md) and [At the instrument](docs/user/operation.md).

## Documentation

- [User guide](docs/user/index.md): quickstart, run sheets, sessions, the
  procedure at the instrument, data and the viewer, command reference.
- [Technical documentation](docs/technical/index.md): which component does
  what, the simulator, testing and development.

Build the documentation site with `uv run --locked mkdocs build --strict`.

## Acknowledgments

Parts of the code were implemented with the assistance of OpenAI Codex and
Anthropic Claude, and were reviewed and tested by the authors. We are aware
that tools of this kind are built on knowledge and code contributed by people
who were never asked, and who cannot be credited individually here. We thank
them nonetheless.

## License

[BSD 3-Clause](LICENSE.md).
