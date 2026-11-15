"""QueueServer session, startup namespace and enqueue (no RE Manager)."""

import math
from pathlib import Path

import pytest
import yaml

pytest.importorskip("bluesky_queueserver_api")

from keithley2460_ophyd.exceptions import (  # noqa: E402
    ElectrochemistryError,
    UnsupportedCapabilityError,
    ValidationError,
)
from keithley2460_ophyd.state import DeviceState  # noqa: E402

from electrochemistry_bluesky import SimulatedSequenceBackend  # noqa: E402
from electrochemistry_bluesky.queueserver import (  # noqa: E402
    Session,
    archive_main,
    build_namespace,
    enqueue,
    load_session,
    queue_items,
    sequence_digest,
    sheet_digest,
)
from electrochemistry_bluesky.run_file import read_run_file  # noqa: E402
from electrochemistry_bluesky.run_sheet import (  # noqa: E402
    load_run_sheet,
    make_template,
    write_run_sheet,
)

QSERVER = Path(__file__).parents[2] / "qserver"


@pytest.fixture
def session(tmp_path):
    example = load_session(QSERVER / "session-simulator.toml")
    return Session(**{**vars(example), "data_dir": tmp_path / "data"})


@pytest.fixture
def sheet(tmp_path):
    return make_template(tmp_path / "runs.xlsx")


def hardware(session):
    return Session(
        **{
            **vars(session),
            "backend": "k2460",
            "resource": "TCPIP0::x::5025::SOCKET",
            "serial": "01234567",
        }
    )


@pytest.mark.parametrize("serial", [None, ""])
def test_a_hardware_session_must_name_the_serial_number(session, serial):
    with pytest.raises(ValidationError, match="serial"):
        Session(**{**vars(hardware(session)), "serial": serial})


def test_example_sessions_parse():
    sim = load_session(QSERVER / "session-simulator.toml")
    assert sim.backend == "simulator" and not sim.hardware
    assert sim.data_dir == Path.home() / "k2460-data" / "qserver-sim"
    k2460 = load_session(QSERVER / "session-k2460.example.toml")
    assert k2460.hardware and k2460.terminals == "front"
    assert k2460.serial == "<instrument-serial-number>"


@pytest.mark.parametrize(
    "text,message",
    [
        ('backend = "simulator"\ndata_dir = "x"\n', r"\[limits\]"),
        (
            'backend = "simulator"\ndata_dir = "x"\nspeed = 2\n[limits]\n',
            "unknown session settings",
        ),
        ('backend = "k2460"\ndata_dir = "x"\n[limits]\n', "invalid session file"),
        (
            'backend = "k2460"\ndata_dir = "x"\n[limits]\nv_min_abs = 0\nv_max_abs = 2\n'
            "i_max_abs = 0.01\nmax_duration_s = 10\nmax_records = 100\n",
            "resource",
        ),
        ('data_dir = "x"\n[limits]\n', "invalid session file"),
    ],
)
def test_bad_session_files(tmp_path, text, message):
    path = tmp_path / "session.toml"
    path.write_text(text)
    with pytest.raises(ValidationError, match=message):
        load_session(path)


def enqueued(session, sheet, measurement_id):
    """The plan arguments oe-enqueue would queue for one row."""
    return queue_items(session, sheet, [measurement_id], show=lambda text: None)[0]["kwargs"]


def test_run_from_sheet_runs_the_enqueued_row(session, sheet):
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=math.inf))
    plan = namespace["run_from_sheet"](**enqueued(session, sheet, "example-rest"))
    namespace["RE"](plan)
    saved = read_run_file(namespace["run_file_writer"].path)
    assert saved["run"]["outcome"] == "completed"
    assert saved["run_sheet_row"]["measurement_id"] == "example-rest"


def test_run_from_sheet_refuses_a_changed_sheet(session, sheet):
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=math.inf))
    kwargs = enqueued(session, sheet, "example-rest")
    sheet.write_bytes(sheet.read_bytes() + b"\0")
    with pytest.raises(ValidationError, match="changed since"):
        namespace["RE"](namespace["run_from_sheet"](**kwargs))
    assert namespace["run_file_writer"].path is None


def test_hardware_session_runs_without_a_per_run_confirmation(session, sheet):
    # The session file (set-up and limits) is the confirmation; rows only validate.
    backend = SimulatedSequenceBackend(speed=math.inf)
    namespace = build_namespace(hardware(session), backend=backend)
    namespace["RE"](namespace["run_from_sheet"](**enqueued(session, sheet, "example-rest")))
    assert read_run_file(namespace["run_file_writer"].path)["run"]["outcome"] == "completed"


def test_recover_plan_returns_an_aborted_backend_to_idle(session, sheet):
    from electrochemistry_bluesky.queueserver import load_request

    backend = SimulatedSequenceBackend(speed=1.0)
    namespace = build_namespace(session, backend=backend)
    backend.connect()
    backend.prepare(load_request(session, **enqueued(session, sheet, "example-rest")))
    backend.kickoff()
    backend.abort("test")
    assert backend.inspect() == (DeviceState.ABORTED, False)
    namespace["RE"](namespace["recover_sequencer"]())
    assert backend.inspect() == (DeviceState.IDLE, False)


class SecuringBackend(SimulatedSequenceBackend):
    """Records the off-mode call the 2460 backend serves when the environment opens."""

    def __init__(self, error=None):
        super().__init__()
        self.secured = []
        self.error = error

    def secure_off_mode(self, limits):
        if self.error is not None:
            raise self.error
        self.secured.append(limits)


def test_opening_the_environment_secures_the_off_mode(session):
    backend = SecuringBackend()
    build_namespace(session, backend=backend)
    assert backend.secured == [session.limits]


def test_environment_does_not_open_without_a_confirmed_off_mode(session):
    with pytest.raises(ElectrochemistryError, match="not confirmed"):
        build_namespace(session, backend=SecuringBackend(ElectrochemistryError("not confirmed")))


class RetainingBackend(SimulatedSequenceBackend):
    """Records the archive call the 2460 backend would serve."""

    def __init__(self) -> None:
        super().__init__(speed=1.0)
        self.archived: list[Path] = []
        self.connected = False

    def connect(self) -> None:
        super().connect()
        self.connected = True

    def close(self) -> None:
        super().close()
        self.connected = False

    def archive_retained(self, destination: Path) -> Path:
        assert self.connected
        destination.write_bytes(b"")
        self.archived.append(destination)
        return destination


def test_archive_plan_saves_into_the_data_folder_and_disconnects(session):
    backend = RetainingBackend()
    namespace = build_namespace(session, backend=backend)
    namespace["RE"](namespace["archive_retained"]())
    [path] = backend.archived
    assert path.parent == session.data_dir and path.name.startswith("retained-")
    assert path.suffix == ".npz" and not backend.connected


def test_archive_plan_needs_a_backend_that_keeps_records(session):
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=1.0))
    with pytest.raises(Exception) as caught:
        namespace["RE"](namespace["archive_retained"]())
    assert isinstance(caught.value, UnsupportedCapabilityError) or isinstance(
        caught.value.__cause__, UnsupportedCapabilityError
    )


def test_archive_command_uses_the_session(tmp_path, session, monkeypatch, capsys):
    from electrochemistry_bluesky import queueserver

    backend = RetainingBackend()
    monkeypatch.setattr(queueserver, "make_backend", lambda _: backend)
    monkeypatch.setattr(queueserver, "load_session", lambda _: session)
    assert archive_main(["--session", str(tmp_path / "s.toml")]) == 0
    assert str(backend.archived[0]) in capsys.readouterr().out


@pytest.mark.parametrize("hardware_session", [False, True])
def test_items_are_built_without_a_prompt(session, sheet, hardware_session, monkeypatch):
    monkeypatch.setattr("builtins.input", pytest.fail)
    shown = []
    chosen = hardware(session) if hardware_session else session
    items = queue_items(chosen, sheet, ["example-rest"], show=shown.append)
    assert items == [
        {
            "name": "run_from_sheet",
            "kwargs": {
                "run_sheet": str(sheet.resolve()),
                "measurement_id": "example-rest",
                "sheet_sha256": sheet_digest(sheet),
                "sequence_sha256": sequence_digest(
                    load_run_sheet(sheet, chosen.limits)["example-rest"].sequence
                ),
            },
        }
    ]
    assert "measurement_id: example-rest" in shown[0]


@pytest.mark.parametrize("hardware_session", [False, True])
def test_rows_the_2460_cannot_run_are_not_enqueued(session, tmp_path, hardware_session):
    # Valid for the session limits, but 65 trigger blocks.
    row = {
        "measurement_id": "too-many-blocks",
        "protocol": "pulse_train",
        "start_mode": "external",
        "current_a": -0.001,
        "pulse_s": 1,
        "pre_rest_s": 1,
        "rest_s": 3,
        "repeats": 2,
        "rest_end_v": 0.005,
    }
    rows = [{**row, "measurement_id": "fits", "start_mode": "immediate", "rest_end_v": None}, row]
    sheet = write_run_sheet(tmp_path / "blocks.xlsx", rows)
    chosen = hardware(session) if hardware_session else session
    assert len(queue_items(chosen, sheet, ["fits"], show=lambda _: None)) == 1
    with pytest.raises(ValidationError, match="too-many-blocks: 65 trigger blocks exceed 63"):
        queue_items(chosen, sheet, ["fits", "too-many-blocks"], show=lambda _: None)


def test_unknown_ids_are_reported_before_anything_is_built(session, sheet):
    with pytest.raises(ValidationError, match="Not in the sheet: nope"):
        queue_items(hardware(session), sheet, ["nope"], show=pytest.fail)


def test_enqueue_adds_plans_in_order(session, sheet):
    class Api:
        added = []

        def item_add(self, item):
            self.added.append(item)

    api = Api()
    items = queue_items(session, sheet, ["example-rest", "example-rest"], show=lambda _: None)
    enqueue(items, "tcp://unused", api=api)
    assert [(p.name, p.kwargs["measurement_id"]) for p in api.added] == [
        ("run_from_sheet", "example-rest")
    ] * 2


def test_permissions_allow_only_the_sequencer_plans():
    groups = yaml.safe_load((QSERVER / "startup" / "user_group_permissions.yaml").read_text())
    for group in groups["user_groups"].values():
        assert group["allowed_plans"] == [
            "run_from_sheet",
            "recover_sequencer",
            "archive_retained",
        ]
        assert group["allowed_functions"] == []


def session_file(tmp_path, extra="", **limits):
    values = {
        "v_min_abs": 0.0,
        "v_max_abs": 3.0,
        "i_max_abs": 0.01,
        "max_duration_s": 200000.0,
        "max_records": 1000000,
    }
    values.update(limits)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "session.toml"
    path.write_text(
        f'backend = "simulator"\ndata_dir = "{tmp_path}"\n[limits]\n'
        + "".join(f"{k} = {v}\n" for k, v in values.items())
        + extra
    )
    return path


def test_validate_compiles_every_row(tmp_path, sheet, capsys):
    from electrochemistry_bluesky.queueserver import validate_main

    assert validate_main([str(sheet), "--session", str(session_file(tmp_path))]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("37 of 37 run(s) valid")
    assert any(
        line.startswith("ok   example-pulse: bipolar_pulse") and "blocks" in line for line in out
    )


def test_validate_reports_rows_the_sequencer_cannot_run(tmp_path, sheet, capsys):
    from electrochemistry_bluesky.queueserver import validate_main

    path = session_file(tmp_path, i_max_abs=10.0)  # valid limits, beyond the 2460's 7 A
    assert validate_main([str(sheet), "example-rest", "nope", "--session", str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL example-rest: i_max_abs exceeds runtime maximum 7 A" in out
    assert "FAIL nope: not in the sheet" in out
    assert "0 of 2 run(s) valid" in out


def test_validate_reports_sheet_level_errors(tmp_path, sheet, capsys):
    from electrochemistry_bluesky.queueserver import validate_main

    path = session_file(tmp_path, i_max_abs=0.00005)  # template rows use 0.1 mA
    assert validate_main([str(sheet), "--session", str(path)]) == 1
    assert capsys.readouterr().out.startswith(f"INVALID {sheet}:")


DIRECTIONS_TABLE = '[directions]\nsodiation = "discharge"\ndesodiation = "charge"\n'


def word_sheet(tmp_path):
    row = dict(protocol="bipolar_pulse", current_a=1e-4, pulse_s=0.1, fast_window_s=1)
    return write_run_sheet(
        tmp_path / "words.xlsx",
        [dict(row, measurement_id="sod-first", start_with="sodiation")],
    )


def test_the_session_directions_table_is_loaded_and_recorded(tmp_path):
    from electrochemistry_bluesky.queueserver import session_metadata

    session = load_session(session_file(tmp_path, DIRECTIONS_TABLE))
    assert session.directions == {"sodiation": "discharge", "desodiation": "charge"}
    assert session_metadata(session)["settings"]["directions"] == session.directions
    with pytest.raises(ValidationError, match="cannot be redefined"):
        load_session(session_file(tmp_path, '[directions]\ncharge = "discharge"\n'))


def test_validate_enqueue_and_run_use_the_session_directions(tmp_path, capsys):
    from electrochemistry_bluesky.queueserver import validate_main

    sheet = word_sheet(tmp_path)
    path = session_file(tmp_path, DIRECTIONS_TABLE)
    session = load_session(path)
    assert validate_main([str(sheet), "--session", str(path)]) == 0
    assert "start_with: sodiation = discharge" in capsys.readouterr().out
    plain = session_file(tmp_path / "plain")  # the same sheet without the table
    assert validate_main([str(sheet), "--session", str(plain)]) == 1
    shown = []
    queue_items(session, sheet, ["sod-first"], show=shown.append)
    assert "start_with: sodiation = discharge" in shown[0]
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=math.inf))
    kwargs = enqueued(session, sheet, "sod-first")
    namespace["RE"](namespace["run_from_sheet"](**kwargs))
    saved = read_run_file(namespace["run_file_writer"].path)
    assert saved["run"]["outcome"] == "completed"
    assert saved["run_sheet_row"]["start_with"] == "sodiation"
    assert saved["sequence"]["steps"][1]["level"] < 0  # discharge first
    assert saved["extra"]["sequence_sha256"] == kwargs["sequence_sha256"]


def test_a_worker_that_reads_the_row_differently_refuses_it(tmp_path):
    """The client's table says discharge first; the worker's session says charge."""
    sheet = word_sheet(tmp_path)
    client = load_session(session_file(tmp_path / "client", DIRECTIONS_TABLE))
    kwargs = enqueued(client, sheet, "sod-first")
    reversed_table = '[directions]\nsodiation = "charge"\n'
    worker = load_session(session_file(tmp_path / "worker", reversed_table))
    namespace = build_namespace(worker, backend=SimulatedSequenceBackend(speed=math.inf))
    with pytest.raises(ValidationError, match="different sequence than when it was enqueued"):
        namespace["RE"](namespace["run_from_sheet"](**kwargs))
    assert namespace["run_file_writer"].path is None  # refused before any run opened
