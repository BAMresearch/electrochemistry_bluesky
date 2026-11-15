"""Golden trigger tables and independent simulator/replay contract checks."""

import numpy as np
import pytest
from keithley2460_ophyd.backend import READOUT_FIELDS
from keithley2460_ophyd.models import CutoffAction as A
from keithley2460_ophyd.models import RepeatBlock, SafetyLimits, Sampling, Sequence
from keithley2460_ophyd.models import StepMode as M
from keithley2460_ophyd.sequencer import BlockKind as K
from keithley2460_ophyd.sequencer import Replayer, compile_program

from electrochemistry_bluesky.builders import (
    bipolar_pulse_sequence,
    cc_step,
    cycling_sequence,
    pulse_train_sequence,
    rest_step,
)
from electrochemistry_bluesky.simulator import CellModel, simulate_sequence


@pytest.fixture
def limits():
    return SafetyLimits(-2, 2.5, 0.005, 1e6, 999984, 0.001)


def branches(program):
    return {
        i: tuple(b.args)
        for i, b in enumerate(program.blocks, 1)
        if b.kind in (K.COUNTER, K.RESET_COUNTER, K.LIMIT_OUTSIDE, K.LIMIT_INSIDE, K.ALWAYS)
    }


def assert_reachable(program):
    todo, seen = [1], set()
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        assert 1 <= i <= len(program.blocks)
        seen.add(i)
        block = program.blocks[i - 1]
        if block.kind == K.ALWAYS:
            todo.append(int(block.args[0]))
        else:
            if i < len(program.blocks):
                todo.append(i + 1)
            if block.kind in (K.LIMIT_INSIDE, K.LIMIT_OUTSIDE):
                todo.append(int(block.args[2]))
                assert program.blocks[int(block.args[3]) - 1].kind == K.MEASURE
            if block.kind == K.COUNTER:
                todo.append(int(block.args[1]))
            if block.kind == K.RESET_COUNTER:
                assert program.blocks[int(block.args[0]) - 1].kind == K.COUNTER
    assert seen == set(range(1, len(program.blocks) + 1))


def replay_simulation(sequence, limits, cell, seed):
    p = compile_program(sequence, limits)
    sim = simulate_sequence(sequence, limits, cell, seed=seed)
    r = sim.readout
    modes = np.array([s.mode == M.VOLTAGE for s in sequence.steps])[r["step_index"]]
    reading = np.where(modes, r["current_a"], r["voltage_v"])
    source = np.where(modes, r["voltage_v"], r["current_a"])
    replay = Replayer(p)
    results, events = [], []
    rng = np.random.default_rng(seed)
    start = 0
    while start < len(reading):
        end = start + int(rng.integers(1, 27))
        chunk, new_events = replay.feed(
            *(
                a[start:end]
                for a in (r["t_s"], reading, source, r["measure_status"], r["source_status"])
            )
        )
        results.append(chunk)
        events.extend(new_events)
        start = end
    for field in READOUT_FIELDS:
        np.testing.assert_array_equal(np.concatenate([c[field] for c in results]), r[field])
    assert [e[1:] for e in events] == [e[1:] for e in sim.events]
    np.testing.assert_allclose([e[0] for e in events], [e[0] for e in sim.events], atol=1e-12)
    assert replay.finish("end" if sim.outcome == "completed" else "stop") == (sim.outcome, "")
    return sim


@pytest.mark.parametrize("seed", [0, 3, 17])
@pytest.mark.parametrize("case", ["pulse", "bipolar", "relaxed", "cc_next", "cc_stop", "cc_cv"])
def test_replay_equals_independent_noisy_simulator(limits, case, seed):
    cell = CellModel(ocv_soc=(0.0, 1.0), ocv_v=(1.0, 1.0), noise_v=1e-6)
    if case == "pulse":
        sequence = pulse_train_sequence(
            0.001,
            0.1,
            4,
            rest_s=0.6,
            pre_rest_s=0.6,
            fast_interval_s=0.006,
        )
    elif case == "bipolar":
        sequence = bipolar_pulse_sequence(  # one sampling rate: 53 blocks
            0.001, 0.1, 4, rest_s=0.6, pre_rest_s=0.6, fast_interval_s=0.006, fast_window_s=0.6
        )
    elif case == "relaxed":
        cell = CellModel(capacity_ah=1e-5, noise_v=1e-6)
        sequence = pulse_train_sequence(-1e-3, 1, 20, rest_s=60, pre_rest_s=2, rest_end_v=0.095)
    elif case == "cc_cv":
        sequence = cycling_sequence(
            0.001,
            0.96,
            1.04,
            2,
            rest_s=1,
            cv_i_abs_min=0.0007,
            cv_max_s=30,
            max_step_s=30,
            interval_s=0.1,
        )
    else:
        action = A.NEXT if case == "cc_next" else A.STOP
        sequence = Sequence(
            (
                cc_step(0.001, 1.04, 30, Sampling(0.1), action),
                rest_step(1, Sampling(0.1)),
                cc_step(-0.001, 0.96, 30, Sampling(0.1), action),
            ),
            RepeatBlock(0, 2, 3),
        )
    sim = replay_simulation(sequence, limits, cell, seed)
    if case == "relaxed":
        assert sim.events[-1][1] == 3 and sim.outcome == "stopped_cutoff"
    if case not in ("pulse", "bipolar"):
        assert any(e[3] == "cutoff" for e in sim.events)
    if case == "cc_cv":
        assert any(e[4] == "i_abs_min" for e in sim.events)


def feed(replay, readings, sources=None, times=None, status=None, source_status=None):
    n = len(readings)
    return replay.feed(
        np.arange(1, n + 1) * 0.1 if times is None else times,
        readings,
        np.zeros(n) if sources is None else sources,
        np.zeros(n, dtype=int) if status is None else status,
        np.zeros(n, dtype=int) if source_status is None else source_status,
    )
