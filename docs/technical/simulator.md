# Simulator and fakes

Everything above the backend runs without an instrument. There are two kinds of
stand-in, for different purposes.

## The simulated device (`SimulatedSequenceBackend`)

`SimulatedSequenceBackend` (`simulator.py`) implements the device package's
`SequenceBackend` protocol, like the 2460 backend, so the Flyer, the plan, the run file, both notebooks and the
QueueServer run unchanged on it. Select it with `backend = "simulator"` in a
session file (`qserver/session-simulator.toml`) or `BACKEND = "simulator"` in
the per-run notebook.

- At `prepare`, it validates the request and computes the whole run with
  `simulate_sequence`. From `kickoff`, `poll`
  releases the precomputed readings and events as wall time passes, scaled by
  `speed` (10 = ten times real time; `math.inf` releases everything at once,
  which the tests use).
- External start: `external_start_delay_s` supplies a synthetic START, or
  `fire_start()` does so on demand.
- `abort` cuts the release at the current time with outcome `aborted`;
  `inspect` reports the same `DeviceState` values and output state as the
  hardware backend.

### The cell model

`CellModel` is an equivalent circuit: open-circuit voltage as a function of
state of charge, a series resistance R0 and one R1‖C1 element, plus Gaussian
noise. Defaults: 0.5 mAh, R0 20 Ω, R1 50 Ω, C1 0.2 F, an OCV curve with a low
plateau rising to 2 V, 50 µV noise. Positive current raises the state of
charge and the voltage, as in the sequence convention.

`simulate_sequence` runs the sequence in virtual time with the same sampling
schedule (`sample_schedule`), the same cutoff rules and the same repeat
handling as the instrument program. It produces the readout fields and events
of a real run, so the simulator also serves as an independent reference for
the compiler: the replay of a simulated run must reproduce the simulator's
indices, events and outcome.

The curve is illustrative, **not** a fitted model of any electrode. The per-run
notebook's preview uses the same simulation and says so on the plot.

## Fakes of the 2460 for backend tests

`keithley2460_ophyd.testing` (device package, `testing` extra) runs the **real
packaged TSP** in Lua through `lupa`, against a mock of the instrument API that
reproduces behaviour measured on the instrument (off mode stored per source
function, a function change forcing the output OFF, a stale ON reading after an
OFF command), with an event log, a reading buffer and a Python stand-in for the
trigger model. Its `FakeInstrument` replaces the backend's transport, so tests
exercise the actual upload, status parsing, reads, replay, abort and recovery.
The device's own tests fill the buffer from a resistor model;
`tests/unit/test_k2460_backend_run_file.py` here fills it from the synthetic
cell and writes a run file through `run_sequence`.
