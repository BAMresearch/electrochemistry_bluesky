"""Execute the operator template cell by cell against the simulator."""

import contextlib
import io
from importlib.resources import files
from pathlib import Path

import nbformat
import pytest
from keithley2460_ophyd.exceptions import ValidationError
from keithley2460_ophyd.models import expected_records
from plotly.graph_objects import Figure

from electrochemistry_bluesky import SimulatedSequenceBackend
from electrochemistry_bluesky.callbacks import LiveSequencePlot
from electrochemistry_bluesky.run_file import read_run_file
from electrochemistry_bluesky.run_sheet import make_template

TEMPLATE = Path(str(files("electrochemistry_bluesky").joinpath("templates/run.ipynb")))


def execute(
    monkeypatch,
    tmp_path,
    *,
    backend="simulator",
    measurement_id="example-rest",
    namespace=None,
    output=None,
    stop_before=None,
    tiled="{}",
):
    sheet = make_template(tmp_path / "runs.xlsx")
    data_dir = tmp_path / "data"
    figures, live = [], []
    monkeypatch.setattr(Figure, "show", lambda self: figures.append(self))
    # Route the notebook's normal display path through a recording render callback.
    monkeypatch.setattr(LiveSequencePlot, "_display", lambda self, fig: live.append(fig))
    namespace = {} if namespace is None else namespace
    output = io.StringIO() if output is None else output
    replacements = {
        'RUN_SHEET = Path("runs.xlsx")': f"RUN_SHEET = Path({str(sheet)!r})",
        'DATA_DIR = Path.home() / "k2460-data"': f"DATA_DIR = Path({str(data_dir)!r})",
        "SIM_SPEED = 10.0": 'SIM_SPEED = float("inf")',
        'BACKEND = "simulator"': f"BACKEND = {backend!r}",
        "TILED = {}": f"TILED = {tiled}",
        "VISA_RESOURCE = None": 'VISA_RESOURCE = "TCPIP0::192.0.2.1::5025::SOCKET"',
        "EXPECTED_SERIAL = None": 'EXPECTED_SERIAL = "01234567"',
        'MEASUREMENT_ID = "example-rest"': f"MEASUREMENT_ID = {measurement_id!r}",
    }
    with contextlib.redirect_stdout(output):
        for cell in nbformat.read(TEMPLATE, as_version=4).cells:
            if cell.cell_type != "code":
                continue
            if stop_before is not None and stop_before in cell.source:
                break
            source = cell.source
            for before, after in replacements.items():
                source = source.replace(before, after)
            exec(compile(source, f"{TEMPLATE}:{cell.id}", "exec"), namespace)
    return namespace, output.getvalue(), figures, live


def test_simulator_notebook_end_to_end(monkeypatch, tmp_path):
    namespace, output, figures, live = execute(monkeypatch, tmp_path)
    assert namespace["outcome"] == "completed"
    assert namespace["flyer"].output_enabled is False
    path = namespace["writer"].path
    assert path.is_file()
    saved = read_run_file(path)
    assert saved["run"]["outcome"] == "completed"
    assert len(saved["readout"]["t_s"]) == expected_records(namespace["request"].sequence)
    assert saved["run_sheet_row"]["measurement_id"] == "example-rest"
    assert "example-rest: rest <-- selected" in output
    assert "Final outcome: completed" in output
    assert "cutoff=0, stop=0, abort=0" in output
    assert "Records written: 10; expected: 10" in output
    assert len(figures) == 2
    assert figures == [namespace["preview"], namespace["result_figure"]]
    assert all(isinstance(fig, Figure) for fig in [*figures, *live])
    assert live and "completed" in live[-1].layout.title.text


def test_hardware_cells_before_the_run_do_no_io_and_create_no_file(monkeypatch, tmp_path):
    namespace = {}
    execute(monkeypatch, tmp_path, backend="k2460", namespace=namespace, stop_before="RunEngine(")
    assert not namespace["backend"]._transport.connected
    assert not (tmp_path / "data").exists()


def test_failed_run_reports_file_outcome_and_off_state(monkeypatch, tmp_path):
    from bluesky.utils import FailedStatus

    poll = SimulatedSequenceBackend.poll

    def abort_on_poll(self, max_records):
        self.abort("test abort")
        return poll(self, max_records)

    monkeypatch.setattr(SimulatedSequenceBackend, "poll", abort_on_poll)
    namespace, output = {}, io.StringIO()
    with pytest.raises(FailedStatus):
        execute(monkeypatch, tmp_path, namespace=namespace, output=output)
    path = namespace["writer"].path
    assert path.is_file()
    assert read_run_file(path)["run"]["outcome"] != "completed"
    assert namespace["flyer"].outcome == "aborted"
    assert namespace["flyer"].output_enabled is False
    assert str(path) in output.getvalue()
    assert "flyer outcome: aborted" in output.getvalue()
    assert "Output confirmed OFF: True" in output.getvalue()


def test_a_sheet_edited_after_selection_is_refused(monkeypatch, tmp_path):
    # The file must not record a newer sheet's hash than the row run.
    import openpyxl

    namespace = {}
    execute(monkeypatch, tmp_path, namespace=namespace, stop_before="RunEngine(")
    sheet = namespace["RUN_SHEET"]
    book = openpyxl.load_workbook(sheet)
    book.worksheets[0]["A1"] = "edited"
    book.save(sheet)
    run_cell = next(
        cell.source
        for cell in nbformat.read(TEMPLATE, as_version=4).cells
        if "RunEngine(" in cell.source
    )
    with pytest.raises(ValidationError, match="changed since the row was selected"):
        exec(compile(run_cell, "run cell", "exec"), namespace)
    assert not (tmp_path / "data").exists()


def test_unknown_id_lists_available_runs(monkeypatch, tmp_path):
    with pytest.raises(ValidationError, match="available IDs:.*example-rest"):
        execute(monkeypatch, tmp_path, measurement_id="missing")
    assert not (tmp_path / "data").exists()


def test_template_is_clean_and_has_one_settings_cell():
    notebook = nbformat.read(TEMPLATE, as_version=4)
    nbformat.validate(notebook)
    settings = [cell for cell in notebook.cells if "settings" in cell.metadata.get("tags", [])]
    assert len(settings) == 1 and settings[0].cell_type == "code"
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert not cell.outputs
            assert cell.execution_count is None


def test_the_notebook_keeps_a_tiled_copy_and_stops_its_server(monkeypatch, tmp_path):
    pytest.importorskip("tiled")
    from electrochemistry_bluesky.tiled_store import open_tiled_store

    namespace, _, _, _ = execute(monkeypatch, tmp_path, tiled='{"local": True}')
    assert namespace["outcome"] == "completed"
    store = tmp_path / "data" / "tiled"
    assert not (store / "server.json").exists()  # the run cell stopped the server
    saved = read_run_file(namespace["writer"].path)
    reopened = open_tiled_store({"local": True}, tmp_path / "data")
    try:
        run = reopened.client[saved["provenance"]["bluesky_uid"]]
        assert len(run["readout"].read()["t_s"]) == len(saved["readout"]["t_s"])
    finally:
        reopened.close()
