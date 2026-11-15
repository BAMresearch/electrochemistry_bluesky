"""Synthetic battery cell: deterministic offline execution and a simulated backend.

``simulate_sequence`` does no instrument I/O or wall-clock pacing;
``SimulatedSequenceBackend`` releases its result at wall time through the
``keithley2460_ophyd.SequenceBackend`` protocol.

This synthetic cell is for exercising software, not predicting a real cell.
Positive current raises SOC. OCV is linearly interpolated, with endpoint values
outside the grid; SOC itself is not clipped, so charge remains conserved.
"""

import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from keithley2460_ophyd.backend import SequencePoll, SequenceRequest
from keithley2460_ophyd.exceptions import ElectrochemistryError, ValidationError
from keithley2460_ophyd.models import (
    CutoffAction,
    SafetyLimits,
    Sequence,
    Step,
    StepMode,
    expanded,
    expected_records,
    sample_schedule,
    validate,
)
from keithley2460_ophyd.state import DeviceState
from keithley2460_ophyd.validation import integer, number, positive
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class CellModel:
    """OCV + R0 + R1||C1, initially relaxed (zero RC voltage).

    The default curve has a low plateau and a sloping region up to 2 V,
    oriented so positive current raises both SOC and voltage, as in the sequence
    convention. It is illustrative, not a fitted OCV curve of any electrode.
    """

    capacity_ah: float = 0.5e-3
    soc0: float = 0.5
    r0_ohm: float = 20.0
    r1_ohm: float = 50.0
    c1_f: float = 0.2
    ocv_soc: tuple[float, ...] = (0.0, 0.2, 0.5, 0.8, 1.0)
    ocv_v: tuple[float, ...] = (0.08, 0.085, 0.1, 0.8, 2.0)
    noise_v: float = 50e-6

    def __post_init__(self) -> None:
        for name in ("capacity_ah", "r0_ohm", "r1_ohm", "c1_f"):
            positive(getattr(self, name), name)
        number(self.soc0, "soc0")
        if not 0 <= self.soc0 <= 1:
            raise ValidationError("soc0 must be in [0, 1]")
        positive(self.noise_v, "noise_v", zero=True)
        if (
            not isinstance(self.ocv_soc, tuple)
            or not isinstance(self.ocv_v, tuple)
            or len(self.ocv_soc) < 2
            or len(self.ocv_soc) != len(self.ocv_v)
        ):
            raise ValidationError(
                "ocv_soc/ocv_v must be equal-length tuples with at least 2 points"
            )
        for name in ("ocv_soc", "ocv_v"):
            for value in getattr(self, name):
                number(value, name)
        if self.ocv_soc[0] != 0 or self.ocv_soc[-1] != 1:
            raise ValidationError("ocv_soc must span 0..1")
        if any(b <= a for a, b in zip(self.ocv_soc, self.ocv_soc[1:], strict=False)):
            raise ValidationError("ocv_soc must be strictly increasing")


@dataclass(frozen=True)
class SimulationResult:
    readout: dict[str, NDArray[Any]]
    events: list[tuple[float, int, int, str, str]]
    outcome: str
    final_soc: float


def _cutoff(step: Step, voltage: float, current: float) -> str:
    if step.mode == StepMode.VOLTAGE:
        if step.i_abs_min is not None and abs(current) <= step.i_abs_min:
            return "i_abs_min"
    else:
        # Strict, as the compiled LIMIT_OUTSIDE blocks are replayed: a reading
        # equal to the limit does not cut off (2460 equality unverified).
        if step.v_min is not None and voltage < step.v_min:
            return "v_min"
        if step.v_max is not None and voltage > step.v_max:
            return "v_max"
    return ""


def _hold_voltage(
    drive: float, rc_v: float, interval: float, cell: CellModel, i_max: float
) -> tuple[float, float]:
    """Exact R0 + R1||C1 response to a held voltage over one interval.

    ``drive`` is setpoint minus OCV (OCV frozen within the interval). While the
    required current exceeds ``i_max`` the source holds the clamped current
    (compliance) until the RC voltage lets it leave compliance, then holds the
    voltage. Returns the RC voltage at the interval end and the charge in C.
    """
    r0, r1 = cell.r0_ohm, cell.r1_ohm
    tau_s = r1 * cell.c1_f
    charge_c = 0.0
    current = (drive - rc_v) / r0
    if abs(current) > i_max:
        held = math.copysign(i_max, current)
        # RC voltage at which (drive - rc) / R0 falls back to the clamped current.
        ratio = (drive - held * r0 - held * r1) / (rc_v - held * r1)
        leave_s = -tau_s * math.log(ratio) if 0 < ratio < 1 else math.inf
        if leave_s >= interval:
            rc_v = held * r1 + (rc_v - held * r1) * math.exp(-interval / tau_s)
            return rc_v, held * interval
        rc_v, charge_c, interval = drive - held * r0, held * leave_s, interval - leave_s
    # Held voltage: RC relaxes to the R1/(R0+R1) share of the drive with the
    # faster time constant of R0 in parallel with R1.
    final = drive * r1 / (r0 + r1)
    tau_v_s = tau_s * r0 / (r0 + r1)
    response = -math.expm1(-interval / tau_v_s)
    rc_integral = final * interval + (rc_v - final) * tau_v_s * response
    charge_c += (drive * interval - rc_integral) / r0
    rc_v += (final - rc_v) * response
    return rc_v, charge_c


_DEFAULT_CELL = CellModel()


def simulate_sequence(
    sequence: Sequence, limits: SafetyLimits, cell: CellModel = _DEFAULT_CELL, *, seed: int = 0
) -> SimulationResult:
    """Execute in virtual time, checking cutoffs after retaining each sample.

    In voltage mode the RC response and charge are exact for the held voltage
    with current compliance at the session limit (OCV frozen within each
    interval; see ``_hold_voltage``). The sample reports the current that holds
    the setpoint at the interval end, so the measured voltage equals the
    setpoint (plus noise) outside compliance. A held-current shortcut would be
    unstable for intervals longer than about tau·2·R0/(R0+R1).
    Noise affects measured voltage and its cutoffs, never the internal state.
    """
    validate(sequence, limits)
    if not isinstance(cell, CellModel):
        raise ValidationError("cell must be a CellModel")
    rng = np.random.default_rng(seed)
    size = expected_records(sequence)
    # Match run_file.READOUT_FIELDS without importing its optional h5py dependency.
    readout: dict[str, NDArray[Any]] = {
        name: np.zeros(size, dtype="f8" if index < 3 else "i4")
        for index, name in enumerate(
            (
                "t_s",
                "voltage_v",
                "current_a",
                "step_index",
                "repeat_index",
                "source_status",
                "measure_status",
            )
        )
    }
    events: list[tuple[float, int, int, str, str]] = []
    soc, rc_v = cell.soc0, 0.0
    time_s, used = 0.0, 0
    outcome = "completed"
    charge_as = 3600 * cell.capacity_ah
    tau_s = cell.r1_ohm * cell.c1_f
    for step_index, repeat_index, step in expanded(sequence):
        events.append((time_s, step_index, repeat_index, "step_start", step.label))
        step_start = time_s
        phase_start = 0.0
        cut = ""
        for count, interval in sample_schedule(step):
            # expm1 retains precision for intervals much shorter than tau.
            response = -math.expm1(-interval / tau_s)
            for k in range(1, count + 1):
                if step.mode == StepMode.VOLTAGE:
                    drive = step.level - float(np.interp(soc, cell.ocv_soc, cell.ocv_v))
                    rc_v, charge_c = _hold_voltage(drive, rc_v, interval, cell, limits.i_max_abs)
                    soc += charge_c / charge_as
                    # The source holds the setpoint against the OCV change too.
                    drive = step.level - float(np.interp(soc, cell.ocv_soc, cell.ocv_v))
                    current = (drive - rc_v) / cell.r0_ohm
                    current = max(-limits.i_max_abs, min(limits.i_max_abs, current))
                else:
                    current = step.level
                    soc += current * interval / charge_as
                    rc_v += (current * cell.r1_ohm - rc_v) * response
                voltage = float(np.interp(soc, cell.ocv_soc, cell.ocv_v)) + current * cell.r0_ohm
                voltage += rc_v + rng.normal(0.0, cell.noise_v)
                time_s = step_start + phase_start + k * interval
                readout["t_s"][used] = time_s
                readout["voltage_v"][used] = voltage
                readout["current_a"][used] = current
                readout["step_index"][used] = step_index
                readout["repeat_index"][used] = repeat_index
                used += 1
                if voltage < limits.v_min_abs or voltage > limits.v_max_abs:
                    outcome = "stopped_cutoff"
                    events.append(
                        (time_s, step_index, repeat_index, "stop", "absolute voltage window")
                    )
                    break
                cut = _cutoff(step, voltage, current)
                if cut:
                    events.append((time_s, step_index, repeat_index, "cutoff", cut))
                    if step.on_cutoff == CutoffAction.STOP:
                        outcome = "stopped_cutoff"
                        events.append((time_s, step_index, repeat_index, "stop", cut))
                    break
            if cut or outcome != "completed":
                break
            phase_start += count * interval
        if outcome != "completed":
            break
        if not cut:
            time_s = step_start + step.duration_s
    return SimulationResult(
        {name: values[:used].copy() for name, values in readout.items()}, events, outcome, soc
    )


def iter_simulated_chunks(
    result: SimulationResult, chunk_records: int = 500
) -> Iterator[dict[str, NDArray[Any]]]:
    """Yield ordered, bounded readout slices, including the final partial chunk."""
    integer(chunk_records, "chunk_records")
    for start in range(0, len(result.readout["t_s"]), chunk_records):
        yield {
            name: values[start : start + chunk_records] for name, values in result.readout.items()
        }


_DEFAULT_CELL = CellModel()


class SimulatedSequenceBackend:
    """Synthetic cell only; the host releases, but never paces, source points."""

    def __init__(
        self,
        cell: CellModel = _DEFAULT_CELL,
        *,
        speed: float = 1.0,
        seed: int = 0,
        external_start_delay_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed < 1:
            raise ValidationError("speed must be >= 1 (or infinity)")
        if math.isnan(speed):
            raise ValidationError("speed must be >= 1 (or infinity)")
        if not isinstance(cell, CellModel):
            raise ValidationError("cell must be a CellModel")
        integer(seed, "seed", minimum=0)
        if external_start_delay_s is not None:
            positive(external_start_delay_s, "external_start_delay_s", zero=True)
        self.cell, self.speed, self.seed = cell, speed, seed
        self._delay = external_start_delay_s
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._connected = False
        self._state = DeviceState.IDLE
        self._request: SequenceRequest | None = None
        self._result: SimulationResult | None = None
        self._start: float | None = None
        self._origin: float | None = None
        self._kickoff = 0.0
        self._record = self._event = 0
        self._abort_time: float | None = None
        self._detail = ""

    def connect(self) -> None:
        with self._lock:
            self._connected = True

    def close(self) -> None:
        with self._lock:
            if self._state in (DeviceState.RUNNING, DeviceState.WAITING_START):
                raise ElectrochemistryError("Cannot close an active simulation")
            self._connected = False

    def _check_connected(self) -> None:
        if not self._connected:
            raise ElectrochemistryError("Backend is not connected")

    def _auto_start(self) -> None:
        if (
            self._state == DeviceState.WAITING_START
            and self._delay is not None
            and self._clock() >= self._kickoff + self._delay
        ):
            # Anchor to the scheduled edge, even if the first poll comes later.
            self._begin(self._kickoff + self._delay)

    def inspect(self) -> tuple[DeviceState, bool | None]:
        with self._lock:
            self._auto_start()
            return self._state, self._state == DeviceState.RUNNING

    def equipment(self) -> dict[str, str | int | float | bool]:
        result: dict[str, str | int | float | bool] = {
            "backend": "simulator",
            "seed": self.seed,
            "speed": str(self.speed),
        }
        for key, value in asdict(self.cell).items():
            result[key] = json.dumps(value) if isinstance(value, tuple) else value
        return result

    def prepare(self, request: SequenceRequest) -> None:
        with self._lock:
            self._check_connected()
            # Re-preparing replaces a loaded sequence that was never kicked off
            # (e.g. the run failed to open); output is OFF throughout.
            if self._state not in (DeviceState.IDLE, DeviceState.COMPLETE, DeviceState.PREPARED):
                raise ElectrochemistryError("Prepare requires IDLE, PREPARED or COMPLETE")
            validate(request.sequence, request.limits)
            result = simulate_sequence(request.sequence, request.limits, self.cell, seed=self.seed)
            self._request, self._result = request, result
            self._record = self._event = 0
            self._start = self._origin = self._abort_time = None
            self._detail = ""
            self._state = DeviceState.PREPARED

    def _begin(self, moment: float) -> None:
        self._start = moment
        self._origin = self._wall_clock() - (self._clock() - moment)
        self._state = DeviceState.RUNNING

    def kickoff(self) -> None:
        with self._lock:
            self._check_connected()
            if self._state != DeviceState.PREPARED or self._request is None:
                raise ElectrochemistryError("Kickoff requires PREPARED")
            self._kickoff = self._clock()
            if self._request.start_mode == "external":
                self._state = DeviceState.WAITING_START
            else:
                self._begin(self._kickoff)

    def fire_start(self) -> None:
        with self._lock:
            if self._state == DeviceState.WAITING_START:
                self._begin(self._clock())

    def time_origin_epoch_s(self) -> float | None:
        with self._lock:
            self._auto_start()
            return self._origin

    def _virtual_time(self) -> float:
        if self._start is None:
            return 0.0
        if math.isinf(self.speed):
            return math.inf
        return max(0.0, self.speed * (self._clock() - self._start))

    def poll(self, max_records: int) -> SequencePoll:
        integer(max_records, "max_records")
        with self._lock:
            self._check_connected()
            self._auto_start()
            result = self._result
            if result is None or self._state == DeviceState.PREPARED:
                raise ElectrochemistryError("Poll requires kickoff")
            elapsed = self._abort_time if self._abort_time is not None else self._virtual_time()
            if self._state == DeviceState.WAITING_START:
                elapsed = -1.0
            available = int(np.searchsorted(result.readout["t_s"], elapsed, side="right"))
            end = min(available, self._record + max_records)
            readout = {
                key: values[self._record : end].copy() for key, values in result.readout.items()
            }
            self._record = end
            events = []
            while self._event < len(result.events) and result.events[self._event][0] <= elapsed:
                events.append(result.events[self._event])
                self._event += 1
            drained = self._record == len(result.readout["t_s"]) and self._event == len(
                result.events
            )
            outcome = None
            if drained and self._state != DeviceState.WAITING_START:
                if self._state == DeviceState.ABORTED:
                    outcome = "aborted"
                else:
                    self._state = DeviceState.COMPLETE
                    outcome = result.outcome
            return SequencePoll(readout, events, outcome, self._detail)

    def abort(self, reason: str) -> None:
        with self._lock:
            if self._state not in (DeviceState.WAITING_START, DeviceState.RUNNING):
                return
            assert self._result is not None
            elapsed = self._virtual_time()
            # Infinite speed means the whole simulation has already happened.
            if math.isinf(elapsed):
                elapsed = float(self._result.readout["t_s"][-1])
            self._abort_time = elapsed
            size = int(np.searchsorted(self._result.readout["t_s"], elapsed, side="right"))
            events = (
                [event for event in self._result.events if event[0] <= elapsed]
                if self._start is not None
                else []
            )
            step, repeat = events[-1][1:3] if events else (0, 0)
            events.append((elapsed, step, repeat, "abort", reason))
            self._result = SimulationResult(
                {key: values[:size] for key, values in self._result.readout.items()},
                events,
                "aborted",
                self._result.final_soc,
            )
            self._detail, self._state = reason, DeviceState.ABORTED

    def recover(self) -> None:
        with self._lock:
            if self._state not in (DeviceState.ABORTED, DeviceState.ERROR):
                raise ElectrochemistryError("Recover requires ABORTED or ERROR")
            self._state = DeviceState.IDLE
