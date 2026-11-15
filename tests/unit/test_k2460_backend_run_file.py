"""The 2460 backend (Lua mock) through application code: run files and the simulator.

``keithley2460_ophyd.testing.FakeInstrument`` executes every line the backend
sends in a Lua mock of the 2460; readings come from the synthetic cell.
"""

import math

import numpy as np
import pytest

lupa = pytest.importorskip("lupa.lua51")

from bluesky import RunEngine  # noqa: E402
from keithley2460_ophyd import READOUT_FIELDS, SequenceFlyer, SequenceRequest  # noqa: E402
from keithley2460_ophyd.models import (  # noqa: E402
    CutoffAction,
    SafetyLimits,
    Sampling,
    Sequence,
    StepMode,
)
from keithley2460_ophyd.sequence_backend import Keithley2460SequenceBackend  # noqa: E402
from keithley2460_ophyd.testing import SERIAL, T0, FakeClock, FakeInstrument  # noqa: E402

from electrochemistry_bluesky import run_sequence  # noqa: E402
from electrochemistry_bluesky.builders import bipolar_pulse_sequence, cc_step, rest_step  # noqa: E402
from electrochemistry_bluesky.simulator import CellModel, simulate_sequence  # noqa: E402


def instrument_records(sequence, limits, *, seed=0, t0=T0):
    """Simulated readings as the instrument stores them (time, reading, source value)."""
    sim = simulate_sequence(sequence, limits, CellModel(noise_v=1e-6), seed=seed)
    r = sim.readout
    voltage_step = np.array([s.mode == StepMode.VOLTAGE for s in sequence.steps])[r["step_index"]]
    reading = np.where(voltage_step, r["current_a"], r["voltage_v"])
    source = np.where(voltage_step, r["voltage_v"], r["current_a"])
    rows = []
    for i, t_s in enumerate(r["t_s"]):
        t = t0 + float(t_s)
        seconds = math.floor(t)
        rows.append(
            (
                seconds,
                t - seconds,
                reading[i],
                source[i],
                r["measure_status"][i],
                r["source_status"][i],
            )
        )
    return sim, rows


@pytest.fixture
def limits():
    return SafetyLimits(0, 4, 0.01, 10000, 100000)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def instrument(clock):
    return FakeInstrument(clock)


def make_backend(instrument, clock, **kwargs):
    kwargs.setdefault("expected_serial_number", SERIAL)
    return Keithley2460SequenceBackend(instrument, terminals="front", wall_clock=clock, **kwargs)


@pytest.fixture
def backend(instrument, clock):
    b = make_backend(instrument, clock)
    b.connect()
    return b


def pulse_request(limits, **kwargs):
    sequence = bipolar_pulse_sequence(0.001, 0.1, 2, fast_window_s=1.0)  # 59 of 63 blocks
    return SequenceRequest("pulse 001", sequence, limits, **kwargs)


def drain(backend, max_records=100):
    readouts, events, outcome, detail = [], [], None, ""
    for _ in range(1000):
        poll = backend.poll(max_records)
        readouts.append(poll.readout)
        events.extend(poll.events)
        if poll.outcome is not None and len(poll.readout["t_s"]) < max_records:
            outcome, detail = poll.outcome, poll.detail
            break
    readout = {k: np.concatenate([r[k] for r in readouts]) for k in READOUT_FIELDS}
    return readout, events, outcome, detail


# Helpers


# Connection


# Prepare


def test_every_equipment_key_has_a_described_place_in_the_run_file(backend, instrument, limits):
    from electrochemistry_bluesky.run_file_fields import instrument_place

    instrument.g.push_event(-363, "Input buffer overrun", 1, 0)
    backend.prepare(pulse_request(limits))
    equipment = backend.equipment()
    assert "clock_offset_s" in equipment and "pre_run_instrument_events" in equipment
    for key in equipment:
        assert instrument_place(key, equipment)[0] != "other", key


# Runs


def test_stop_cutoff_run(backend, instrument):
    limits = SafetyLimits(-2, 2.5, 0.005, 1e6, 999984)
    sequence = Sequence(
        (
            rest_step(0.5, Sampling(0.1)),
            cc_step(0.001, 1.04, 30, Sampling(0.1), CutoffAction.STOP),
            rest_step(1, Sampling(0.1)),
        )
    )
    request = SequenceRequest("stop 001", sequence, limits)
    cell = CellModel(ocv_soc=(0.0, 1.0), ocv_v=(1.0, 1.0), noise_v=1e-6)
    sim = simulate_sequence(sequence, limits, cell, seed=1)
    assert sim.outcome == "stopped_cutoff"
    rows = [
        (math.floor(T0 + t), T0 + t - math.floor(T0 + t), v, i, 0, 0)
        for t, v, i in zip(
            sim.readout["t_s"], sim.readout["voltage_v"], sim.readout["current_a"], strict=True
        )
    ]
    backend.prepare(request)
    backend.kickoff()
    instrument.start()
    instrument.add(rows)
    instrument.finish("stop")
    readout, events, outcome, _ = drain(backend)
    assert outcome == "stopped_cutoff"
    assert [e[3] for e in events][-2:] == ["cutoff", "stop"]
    np.testing.assert_array_equal(readout["step_index"], sim.readout["step_index"])


# Retained data


# Through the Flyer and the RunEngine


def test_run_sequence_end_to_end(backend, instrument, limits, tmp_path):
    from electrochemistry_bluesky.callbacks import RunFileWriter
    from electrochemistry_bluesky.run_file import read_run_file

    request = pulse_request(limits)
    sim, rows = instrument_records(request.sequence, limits)
    instrument.instant = (rows, "end")
    flyer = SequenceFlyer(backend, name="k2460", poll_period_s=0.01, chunk_records=97)
    re = RunEngine({}, call_returns_result=True)
    docs = []
    re.subscribe(lambda name, doc: docs.append((name, doc)))
    writer = RunFileWriter(tmp_path)
    re.subscribe(writer)
    assert re(run_sequence(flyer, request)).plan_result == "completed"
    saved = read_run_file(writer.path)
    assert saved["run"]["outcome"] == "completed"
    assert saved["equipment"]["clock_method"].startswith("eventlog.post")
    np.testing.assert_array_equal(saved["readout"]["step_index"], sim.readout["step_index"])
    descriptor = next(d["uid"] for n, d in docs if n == "descriptor" and d["name"] == "readout")
    pages = [d for n, d in docs if n == "event_page" and d["descriptor"] == descriptor]
    np.testing.assert_array_equal(
        np.concatenate([p["data"]["step_index"] for p in pages]), sim.readout["step_index"]
    )
    start = next(d for n, d in docs if n == "start")
    assert start["equipment"]["backend"] == "k2460-sequencer"
    assert start["equipment"]["marker_line"] == 4
    # Unstaging closed the session with the output OFF.
    assert not instrument.connected and instrument.g.src.output == "smu.OFF"


def configured(instrument):
    return [c for c in instrument.sent if "oe_seq_configure" in c]
