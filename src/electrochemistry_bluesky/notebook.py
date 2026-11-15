"""Per-run notebook helpers; requires the optional ``experiment`` dependencies."""

import argparse
import ast
import math
import re
from collections.abc import Mapping
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

import nbformat
import plotly.graph_objects as go  # type: ignore[import-untyped]
from keithley2460_ophyd.backend import SequenceRequest
from keithley2460_ophyd.exceptions import ValidationError
from keithley2460_ophyd.models import SafetyLimits, expanded, expected_records, nominal_duration_s

from .callbacks import sequence_figure
from .run_sheet import RunRequest, check_directions, direction_notes, load_run_sheet
from .simulator import CellModel, simulate_sequence

_DEFAULT_CELL = CellModel()


def run_summary(request: SequenceRequest, directions: Mapping[str, str] | None = None) -> str:
    """Describe the finite request and its configured bounds without hardware claims.

    Direction words from the session's ``directions`` are shown with their meaning.
    """
    hours, remainder = divmod(math.ceil(nominal_duration_s(request.sequence)), 3600)
    minutes, seconds = divmod(remainder, 60)
    row = request.run_sheet_row if isinstance(request, RunRequest) else {}
    lines = [
        f"measurement_id: {request.measurement_id}",
        f"protocol: {row.get('protocol', '(not supplied)')}",
        f"sample_id: {getattr(request, 'sample_id', None) or ''}",
        f"notes: {getattr(request, 'notes', None) or ''}",
        f"start_mode: {request.start_mode}",
        f"expanded steps: {len(expanded(request.sequence))}",
        f"nominal duration (h:mm:ss): {hours}:{minutes:02d}:{seconds:02d}",
        f"expected records (upper bound before cutoffs): {expected_records(request.sequence)}",
        "Safety limits (V, A, s, records):",
        *(f"  {key}: {value}" for key, value in asdict(request.limits).items()),
        "Run-sheet row:",
        *(f"  {key}: {value}" for key, value in row.items()),
    ]
    notes = direction_notes(row, directions or {})
    if notes:
        lines += ["Direction words (from the session):", *(f"  {note}" for note in notes)]
    return "\n".join(lines)


def preview_figure(
    request: SequenceRequest, cell: CellModel = _DEFAULT_CELL, *, seed: int = 0
) -> go.Figure:
    """Preview a synthetic cell using the same layout as the acquisition plots."""
    result = simulate_sequence(request.sequence, request.limits, cell, seed=seed)
    return sequence_figure(
        result.readout,
        [(t, kind, detail) for t, _, _, kind, detail in result.events],
        "Preview — simulated cell, not a prediction",
    )


def _replace_assignments(source: str, replacements: dict[str, str]) -> str:
    """Replace whole top-level assignments (multi-line ones included) by name."""
    lines = source.splitlines()
    spans: dict[str, tuple[int, int]] = {}
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in replacements
        ):
            name = node.targets[0].id
            if name in spans:
                raise ValidationError(f"Settings cell must assign {name} exactly once")
            assert node.end_lineno is not None
            spans[name] = (node.lineno - 1, node.end_lineno)
    missing = replacements.keys() - spans.keys()
    if missing:
        raise ValidationError(f"Settings cell must assign {', '.join(sorted(missing))}")
    # Replace from the bottom so earlier line numbers stay valid.
    for name, (start, end) in sorted(spans.items(), key=lambda item: -item[1][0]):
        if name == "LIMITS" and start and lines[start - 1].startswith("# DUMMY-CELL"):
            start -= 1  # The placeholder warning no longer applies.
        lines[start:end] = replacements[name].splitlines()
    return "\n".join(lines)


def new_run_notebook(
    run_sheet: str | Path,
    measurement_id: str,
    directory: str | Path,
    limits: SafetyLimits,
    *,
    template: str | Path | None = None,
    directions: Mapping[str, str] | None = None,
) -> Path:
    """Validate with session limits, then exclusively create a clean notebook copy.

    The copy's ``LIMITS`` and ``DIRECTIONS`` are the given session values and
    its ``DATA_DIR`` is ``directory``, so the run file is written beside the
    notebook.
    """
    sheet = Path(run_sheet).resolve()
    words = check_directions(directions)
    runs = load_run_sheet(sheet, limits, directions=words)
    if measurement_id not in runs:
        raise ValidationError(
            f"Unknown measurement_id {measurement_id!r}; available IDs: {', '.join(runs)}"
        )
    source = (
        Path(template).read_text(encoding="utf-8")
        if template is not None
        else files(__package__).joinpath("templates/run.ipynb").read_text(encoding="utf-8")
    )
    notebook = nbformat.reads(source, as_version=4)  # type: ignore[no-untyped-call]
    settings = [cell for cell in notebook.cells if "settings" in cell.metadata.get("tags", [])]
    if len(settings) != 1 or settings[0].cell_type != "code":
        raise ValidationError("Template must contain exactly one settings-tagged code cell")
    directory = Path(directory).resolve()
    # The copy must run with the limits the sheet was just validated against,
    # and write its HDF5 file beside the notebook.
    replacements = {
        "MEASUREMENT_ID": f"MEASUREMENT_ID = {measurement_id!r}",
        "RUN_SHEET": f"RUN_SHEET = Path({str(sheet)!r})",
        "DATA_DIR": f"DATA_DIR = Path({str(directory)!r})",
        "LIMITS": "# Session limits given to new_run_notebook; the whole sheet was\n"
        f"# validated against them.\nLIMITS = {limits!r}",
        "DIRECTIONS": f"DIRECTIONS = {words!r}",
    }
    settings[0].source = _replace_assignments(settings[0].source, replacements)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", measurement_id)
    destination = directory / f"{safe}.ipynb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        nbformat.write(notebook, stream)  # type: ignore[no-untyped-call]
    return destination


def new_view_notebook(directory: str | Path) -> Path:
    """Exclusively create ``viewer.ipynb`` in ``directory``, reading run files from there."""
    directory = Path(directory).resolve()
    source = files(__package__).joinpath("templates/view.ipynb").read_text(encoding="utf-8")
    notebook = nbformat.reads(source, as_version=4)  # type: ignore[no-untyped-call]
    first = next(cell for cell in notebook.cells if cell.cell_type == "code")
    first.source = _replace_assignments(
        first.source, {"DATA_DIR": f"DATA_DIR = Path({str(directory)!r})"}
    )
    destination = directory / "viewer.ipynb"
    directory.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        nbformat.write(notebook, stream)  # type: ignore[no-untyped-call]
    return destination


def view_main(argv: list[str] | None = None) -> int:
    """Create a read-only viewer notebook for the run files in a data folder."""
    parser = argparse.ArgumentParser(description=view_main.__doc__)
    parser.add_argument("directory", type=Path, help="the session's data_dir")
    print(new_view_notebook(parser.parse_args(argv).directory))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Create a per-run notebook using explicitly supplied session limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_sheet", type=Path)
    parser.add_argument("measurement_id")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--v-min", type=float, required=True)
    parser.add_argument("--v-max", type=float, required=True)
    parser.add_argument("--i-max", type=float, required=True)
    parser.add_argument("--max-duration-s", type=float, required=True)
    parser.add_argument("--max-records", type=int, required=True)
    parser.add_argument(
        "--direction",
        action="append",
        default=[],
        metavar="WORD=charge|discharge",
        help="a word for start_with / cv_after, as in the session's [directions] (repeatable)",
    )
    args = parser.parse_args(argv)
    directions = {}
    for item in args.direction:
        word, sep, direction = item.partition("=")
        if not sep:
            parser.error(f"--direction {item!r}: expected WORD=charge or WORD=discharge")
        directions[word.strip()] = direction.strip()
    limits = SafetyLimits(args.v_min, args.v_max, args.i_max, args.max_duration_s, args.max_records)
    print(
        new_run_notebook(
            args.run_sheet, args.measurement_id, args.directory, limits, directions=directions
        )
    )
    return 0
