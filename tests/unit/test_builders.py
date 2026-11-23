"""Offline contracts for finite step sequences and battery protocol builders."""

import json
import math
from dataclasses import FrozenInstanceError

import pytest
from keithley2460_ophyd import (
    CutoffAction,
    RepeatBlock,
    SafetyLimits,
    Sampling,
    Sequence,
    Step,
    StepMode,
    expanded,
    expected_records,
    nominal_duration_s,
    sample_schedule,
    sequence_from_dict,
    step_records,
    to_dict,
    validate,
)
from keithley2460_ophyd.exceptions import ValidationError

from electrochemistry_bluesky import (
    bipolar_pulse_sequence,
    cc_step,
    cv_step,
    cycling_sequence,
    log_repeats,
    pulse_train_sequence,
    renard_series,
    rest_step,
    soc_position_sequence,
    two_phase_sampling,
)


def current_step(**changes):
    return Step(
        **(dict(mode=StepMode.CURRENT, level=0.01, duration_s=1, sampling=Sampling(0.1)) | changes)
    )


def limits(**changes):
    return SafetyLimits(
        **(
            dict(v_min_abs=0, v_max_abs=4, i_max_abs=0.1, max_duration_s=1e6, max_records=1_000_000)
            | changes
        )
    )


BAD_NUMBERS = [True, False, "1", None, 1j, math.nan, math.inf, -math.inf, 10**1000]


def test_r5_series_and_closed_endpoints():
    assert renard_series(0.1, 100) == [
        0.1,
        0.16,
        0.25,
        0.4,
        0.63,
        1,
        1.6,
        2.5,
        4,
        6.3,
        10,
        16,
        25,
        40,
        63,
        100,
    ]
    assert renard_series(0.63, 0.63) == [0.63]
    assert renard_series(0.62, 0.63) == [0.63]
    assert renard_series(0.11, 0.15) == []
    assert renard_series(1, 10, 10) == [1, 1.25, 1.6, 2, 2.5, 3.15, 4, 5, 6.3, 8, 10]
    assert renard_series(1e-310, 1e-310) == [1e-310]


def test_log_repeats_anchors_clamps_and_custom_endpoints():
    assert [log_repeats(t) for t in (0.01, 0.1, 1, 100, 1000)] == [100, 100, 39, 6, 6]
    counts = [log_repeats(t) for t in renard_series(0.1, 100)]
    assert counts == sorted(counts, reverse=True)
    assert log_repeats(10, t_lo=1, n_lo=100, t_hi=100, n_hi=1) == 10
    assert log_repeats(1, n_lo=6, n_hi=6) == 6
    assert log_repeats(1, t_lo=1e-300, t_hi=1e300) == 24
    assert log_repeats(1e100 + 2e84, t_lo=1e100, t_hi=1e100 + 4e84) == 24
    with pytest.raises(ValidationError, match="interpolation"):
        log_repeats(1, n_lo=10**1000, n_hi=10**999)


def test_cc_rest_cv_and_soc_direction():
    sampling = Sampling(1)
    assert cc_step(0.01, 3, 10, sampling) == current_step(duration_s=10, sampling=sampling, v_max=3)
    assert cc_step(-0.01, 1, 10, sampling, CutoffAction.STOP) == current_step(
        level=-0.01, duration_s=10, sampling=sampling, v_min=1, on_cutoff=CutoffAction.STOP
    )
    assert rest_step(10, sampling).mode == StepMode.REST
    with pytest.raises(ValidationError, match="nonzero"):
        cc_step(0, 1, 10, sampling)
    assert cv_step(3, 0.001, 10, sampling).i_abs_min == 0.001
    sequence = soc_position_sequence(-0.01, 1, max_s=10, rest_s=20)
    assert sequence.steps == (cc_step(-0.01, 1, 10, sampling), rest_step(20, sampling))
    assert sequence.repeat is None


def test_short_bipolar_protocol_duration_cutoffs_and_record_budget():
    sequence = bipolar_pulse_sequence(0.01, 0.1, 100, v_min=0, v_max=4, name="bipolar")
    assert nominal_duration_s(sequence) == pytest.approx(220 + 10)
    assert sequence.repeat == RepeatBlock(1, 4, 100)
    assert [step.level for step in sequence.steps] == [0, 0.01, 0, -0.01, 0]
    assert [step.duration_s for step in sequence.steps] == [10, 0.1, 1, 0.1, 1]
    # 0.1 s at 6 ms: 17 samples, the interval shortened to 5.88 ms to fit the step.
    assert sequence.steps[1].sampling == Sampling(0.006)
    for step in (sequence.steps[1], sequence.steps[3]):
        assert (step.v_min, step.v_max, step.on_cutoff) == (0, 4, CutoffAction.STOP)
    assert expected_records(sequence) == 21_379
    validate(sequence, limits())
    assert nominal_duration_s(bipolar_pulse_sequence(0.01, 2, 3)) == 34
    assert nominal_duration_s(bipolar_pulse_sequence(0.01, 2, 3, rest_s=4, pre_rest_s=1)) == 37


@pytest.mark.parametrize("start", ["charge", "discharge"])
@pytest.mark.parametrize("cv", [None, 0.001])
def test_cycle_order_and_cv_stays_with_charge(start, cv):
    sequence = cycling_sequence(
        0.01, 1, 3, 2, start=start, cv_i_abs_min=cv, rest_s=10, max_step_s=100, cv_max_s=50
    )
    charge = [(StepMode.CURRENT, 0.01)]
    if cv is not None:
        charge.append((StepMode.VOLTAGE, 3))
    charge.append((StepMode.REST, 0))
    discharge = [(StepMode.CURRENT, -0.01), (StepMode.REST, 0)]
    assert [(s.mode, s.level) for s in sequence.steps] == (
        charge + discharge if start == "charge" else discharge + charge
    )
    assert sequence.repeat == RepeatBlock(0, len(sequence.steps) - 1, 2)
    assert all(step.on_cutoff == CutoffAction.NEXT for step in sequence.steps)
    assert nominal_duration_s(sequence) == 440 + (100 if cv is not None else 0)
    validate(sequence, limits())


@pytest.mark.parametrize(
    "factory,args,changes",
    [
        (renard_series, (0.1, 100), {"t_min": 0}),
        (renard_series, (0.1, 100), {"t_max": math.inf}),
        (renard_series, (0.1, 100), {"t_min": 101}),
        (renard_series, (0.1, 100), {"series": 20}),
        (renard_series, (0.1, 100), {"series": True}),
        (log_repeats, (1,), {"t": 0}),
        (log_repeats, (1,), {"t_lo": 0}),
        (log_repeats, (1,), {"t_hi": math.nan}),
        (log_repeats, (1,), {"t_lo": 100}),
        (log_repeats, (1,), {"n_lo": 0}),
        (log_repeats, (1,), {"n_hi": True}),
        (log_repeats, (1,), {"n_hi": 101}),
        (two_phase_sampling, (1,), {"duration_s": 0}),
        (two_phase_sampling, (1,), {"fast_interval_s": 0.1}),
        (two_phase_sampling, (1,), {"fast_window_s": -1}),
        (cc_step, (0.01, 3, 10, Sampling(1)), {"current_a": "1"}),
        (cc_step, (0.01, 3, 10, Sampling(1)), {"target_v": None}),
        (cv_step, (3, 0.001, 10, Sampling(1)), {"i_abs_min": None}),
        (bipolar_pulse_sequence, (0.01, 0.1, 100), {"current_a": -0.01}),
        (bipolar_pulse_sequence, (0.01, 0.1, 100), {"pulse_s": 0}),
        (bipolar_pulse_sequence, (0.01, 0.1, 100), {"repeats": 0}),
        (bipolar_pulse_sequence, (0.01, 0.1, 100), {"rest_s": 0}),
        (bipolar_pulse_sequence, (0.01, 0.1, 100), {"pre_rest_s": 0}),
        (cycling_sequence, (0.01, 1, 3, 2), {"current_a": -0.01}),
        (cycling_sequence, (0.01, 1, 3, 2), {"v_low": 3}),
        (cycling_sequence, (0.01, 1, 3, 2), {"v_high": None}),
        (cycling_sequence, (0.01, 1, 3, 2), {"cycles": True}),
        (cycling_sequence, (0.01, 1, 3, 2), {"start": "unknown"}),
        (cycling_sequence, (0.01, 1, 3, 2), {"cv_max_s": 0}),
        (cycling_sequence, (0.01, 1, 3, 2), {"cv_i_abs_min": -0.001}),
    ],
)
def test_builder_errors_are_validation_errors(factory, args, changes):
    # Bind positional defaults so each parameter can be replaced independently.
    from inspect import signature

    bound = signature(factory).bind(*args)
    with pytest.raises(ValidationError):
        factory(**(bound.arguments | changes))


@pytest.mark.parametrize(
    "sequence",
    [
        Sequence((cc_step(0.01, 3, 10, Sampling(1)),)),
        Sequence((cv_step(3, 0.001, 10, Sampling(1)),)),
        Sequence((rest_step(10, Sampling(1)),)),
        bipolar_pulse_sequence(0.01, 0.1, 100, v_min=0, v_max=4, name="cell α"),
        cycling_sequence(0.01, 1, 3, 2),
        cycling_sequence(0.01, 1, 3, 2, cv_i_abs_min=0.001, start="discharge"),
        soc_position_sequence(-0.01, 1, max_s=100, rest_s=10),
    ],
)
def test_all_protocol_builders_round_trip_through_json_metadata(sequence):
    data = to_dict(sequence)
    assert data["steps"][0]["mode"] in ("current", "voltage", "rest")
    assert type(data["steps"][0]["mode"]) is str
    assert isinstance(data["steps"], list)
    assert sequence_from_dict(json.loads(json.dumps(data, allow_nan=False))) == sequence
    data["steps"][0]["level"] = 999
    assert sequence.steps[0].level != 999


def test_frozen_slotted_models_and_detached_expansion():
    sequence = bipolar_pulse_sequence(0.01, 0.1, 2)
    for obj, field in [
        (sequence, "name"),
        (sequence.steps[0], "level"),
        (sequence.steps[0].sampling, "interval_s"),
        (sequence.repeat, "count"),
        (limits(), "max_records"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
        assert not hasattr(obj, "__dict__")
    with pytest.raises(TypeError):
        sequence.steps[0] = current_step()
    execution = expanded(sequence)
    execution.clear()
    assert len(sequence.steps) == 5


@pytest.mark.parametrize("pulse_s", renard_series(0.1, 100))
def test_sample_schedule_keeps_exact_step_duration(pulse_s):
    for duration in (pulse_s, max(pulse_s, 1.0)):
        step = current_step(duration_s=duration, sampling=two_phase_sampling(duration))
        schedule = sample_schedule(step)
        assert math.isclose(sum(n * dt for n, dt in schedule), duration, rel_tol=1e-12)
        assert sum(n for n, _ in schedule) == step_records(step)
        requested = (
            step.sampling.fast_interval_s or step.sampling.interval_s,
            step.sampling.interval_s,
        )
        for (_, dt), limit in zip(schedule, requested, strict=False):
            assert dt <= limit * (1 + 1e-12)


def test_bipolar_pulses_can_start_with_discharge():
    sequence = bipolar_pulse_sequence(0.0005, 1, 3, start="discharge")
    assert [step.level for step in sequence.steps] == [0, -0.0005, 0, 0.0005, 0]
    assert sequence.repeat == RepeatBlock(1, 4, 3)
    with pytest.raises(ValidationError, match="start must be charge or discharge"):
        bipolar_pulse_sequence(0.0005, 1, 3, start="negative")


def test_unipolar_pulse_train_in_either_direction():
    # Charge pulses from a discharged state, stopped at 0.2 V under load.
    sequence = pulse_train_sequence(6e-4, 10, 60, rest_s=50, v_max=0.2)
    assert [step.level for step in sequence.steps] == [0, 6e-4, 0]
    assert sequence.repeat == RepeatBlock(1, 2, 60)
    pulse = sequence.steps[1]
    assert (pulse.v_max, pulse.on_cutoff) == (0.2, CutoffAction.STOP)
    assert nominal_duration_s(sequence) == 10 + 60 * 60
    assert pulse.sampling.fast_interval_s == 0.006
    discharge = pulse_train_sequence(-6e-4, 1, 3, v_min=0.005)
    assert discharge.steps[1].level == -6e-4 and discharge.steps[2].duration_s == 1
    validate(discharge, SafetyLimits(0.0, 0.5, 0.001, 1e6, 10**7))
    with pytest.raises(ValidationError, match="nonzero"):
        pulse_train_sequence(0, 1, 3)


def test_pulse_train_ends_on_the_relaxed_voltage():
    discharge = pulse_train_sequence(-6e-3, 1, 500, rest_s=300, rest_end_v=0.005)
    assert [step.duration_s for step in discharge.steps] == [10, 1, 299, 1]
    assert discharge.repeat == RepeatBlock(1, 3, 500)
    pulse, relaxing, check = discharge.steps[1:]
    assert (pulse.v_min, pulse.v_max) == (None, None)  # under load: session window only
    assert (relaxing.v_min, relaxing.v_max) == (None, None)
    assert (check.mode, check.v_min, check.v_max) == (StepMode.REST, 0.005, None)
    assert check.on_cutoff == CutoffAction.STOP
    assert nominal_duration_s(discharge) == 10 + 500 * 301
    charge = pulse_train_sequence(6e-3, 1, 5, rest_s=60, rest_end_v=0.2, v_max=1.5)
    assert (charge.steps[3].v_min, charge.steps[3].v_max) == (None, 0.2)
    assert charge.steps[1].v_max == 1.5
    with pytest.raises(ValidationError, match="rest_s must exceed 2 s"):
        pulse_train_sequence(-6e-3, 1, 5, rest_s=2, rest_end_v=0.005)
    with pytest.raises(ValidationError, match="interval_s must be at most 1 s"):
        pulse_train_sequence(-6e-3, 1, 5, rest_s=60, interval_s=2, rest_end_v=0.005)


@pytest.mark.parametrize("start", ["charge", "discharge"])
def test_cv_hold_after_the_discharge(start):
    # Discharge to 5 mV, hold there, rest, charge to 0.2 V, rest.
    sequence = cycling_sequence(
        2e-5, 0.005, 0.2, 2, start=start, cv_after="discharge", cv_i_abs_min=4e-6, rest_s=60
    )
    discharge = [(StepMode.CURRENT, -2e-5, 0.005, None), (StepMode.VOLTAGE, 0.005, None, 4e-6)]
    charge = [(StepMode.CURRENT, 2e-5, None, 0.2)]
    rest = [(StepMode.REST, 0.0, None, None)]
    halves = (discharge + rest, charge + rest)
    expected = halves[0] + halves[1] if start == "discharge" else halves[1] + halves[0]
    steps = [
        (s.mode, s.level, s.v_min, s.v_max if s.mode != StepMode.VOLTAGE else s.i_abs_min)
        for s in sequence.steps
    ]
    assert steps == expected
    assert sequence.repeat == RepeatBlock(0, 4, 2)
    with pytest.raises(ValidationError, match="cv_after must be charge or discharge"):
        cycling_sequence(2e-5, 0.005, 0.2, 2, cv_after="reduction")


def test_cv_holds_are_limited_to_their_cc_current():
    # A CC step that ends on its time limit short of the voltage must not be
    # followed by a hold at the session's i_max_abs (B6, 20 Nov: 100 uA after a
    # 24 uA CC step); the hold continues at the CC current instead.
    soc = soc_position_sequence(-24.3e-6, 0.005, max_s=10, rest_s=5, cv_i_abs_min=12.2e-6)
    assert soc.steps[1].i_limit == 24.3e-6
    assert soc_position_sequence(-24.3e-6, 0.005, max_s=10, rest_s=5).steps[1].i_limit is None
    cycle = cycling_sequence(
        2e-5, 0.005, 0.2, 1, start="discharge", cv_after="discharge", cv_i_abs_min=4e-6
    )
    assert [s.i_limit for s in cycle.steps if s.mode == StepMode.VOLTAGE] == [2e-5]
