"""Optional Tiled copy of the run documents: a local persistent store, or a server."""

import math
import stat

import numpy as np
import pytest

pytest.importorskip("tiled")
pytest.importorskip("bluesky_tiled_plugins")
pytest.importorskip("bluesky_queueserver_api")

from keithley2460_ophyd.exceptions import ValidationError  # noqa: E402

from electrochemistry_bluesky import SimulatedSequenceBackend  # noqa: E402
from electrochemistry_bluesky.queueserver import (  # noqa: E402
    Session,
    build_namespace,
    load_session,
    queue_items,
    session_metadata,
)
from electrochemistry_bluesky.run_file import read_run_file  # noqa: E402
from electrochemistry_bluesky.run_sheet import make_template  # noqa: E402
from electrochemistry_bluesky.tiled_store import (  # noqa: E402
    LocalTiledServer,
    SafeTiledWriter,
    check_tiled,
    open_tiled_store,
)


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "session.toml"
    path.write_text(
        f'backend = "simulator"\ndata_dir = "{tmp_path / "data"}"\nsim_speed = inf\n'
        "[limits]\nv_min_abs = 0.0\nv_max_abs = 3.0\ni_max_abs = 0.01\n"
        "max_duration_s = 200000.0\nmax_records = 1000000\n"
        "[tiled]\nlocal = true\n"
    )
    return load_session(path)


def run_row(session, measurement_id="example-pulse-train"):
    """Run one template row in a fresh namespace; return the namespace and the run uid."""
    sheet = make_template(session.data_dir.parent / "runs.xlsx", overwrite=True)
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=math.inf))
    uids = []
    namespace["RE"].subscribe(
        lambda name, doc: uids.append(doc["uid"]) if name == "start" else None
    )
    kwargs = queue_items(session, sheet, [measurement_id], show=lambda text: None)[0]["kwargs"]
    namespace["RE"](namespace["run_from_sheet"](**kwargs))
    return namespace, uids[0]


def test_tiled_settings():
    assert check_tiled({}) == {}
    assert check_tiled({"local": True}) == {"local": True, "timeout_s": 10.0}
    remote = check_tiled({"uri": "https://tiled.example", "api_key_env": "TILED_KEY"})
    assert remote["uri"] == "https://tiled.example"


@pytest.mark.parametrize(
    "table,message",
    [
        ({"local": False}, "either local = true or uri"),
        ({"local": True, "uri": "http://x"}, "either local = true or uri"),
        ({"local": "yes"}, "true or false"),
        ({"uri": "ftp://x"}, "http"),
        ({"local": True, "api_key_env": "K"}, "generates its own"),
        ({"uri": "http://x", "api_key_env": "not a name"}, "environment variable"),
        ({"local": True, "timeout_s": 0}, "positive"),
        ({"local": True, "api_key": "secret"}, "unknown settings api_key"),
        (["local"], "must be a table"),
    ],
)
def test_bad_tiled_settings(table, message):
    with pytest.raises(ValidationError, match=message):
        check_tiled(table)


def test_session_reads_and_records_the_tiled_table(session):
    assert session.tiled == {"local": True, "timeout_s": 10.0}
    assert session_metadata(session)["settings"]["tiled"] == session.tiled
    with pytest.raises(ValidationError, match="either"):
        Session(**{**vars(session), "tiled": {"local": False}})


def test_runs_are_kept_in_a_persistent_local_store_beside_the_run_files(session):
    namespace, uid = run_row(session)
    store = namespace["tiled_store"]
    folder = session.data_dir / "tiled"
    assert store.server.uri.startswith("http://127.0.0.1:")
    assert (folder / "server.json").exists()
    assert stat.S_IMODE((folder / "api-key").stat().st_mode) == 0o600
    store.close()
    assert not (folder / "server.json").exists() and not (folder / "api-key").exists()
    saved = read_run_file(namespace["run_file_writer"].path)

    # A new server on the same folder serves the run written by the first one.
    reopened = open_tiled_store(session.tiled, session.data_dir)
    try:
        run = reopened.client[uid]
        assert run.metadata["start"]["measurement_id"] == "example-pulse-train"
        assert run.metadata["start"]["run_sheet_row"]["protocol"] == "pulse_train"
        assert run.metadata["stop"]["exit_status"] == "success"
        # The session (sample, directions, resolved settings) travels in the RunStart.
        assert run.metadata["start"]["session"]["settings"]["tiled"] == {
            "local": True,
            "timeout_s": 10.0,
        }
        assert saved["session"]["settings"] == run.metadata["start"]["session"]["settings"]
        readout = run["readout"].read()
        np.testing.assert_allclose(np.asarray(readout["t_s"]), saved["readout"]["t_s"])
        np.testing.assert_allclose(np.asarray(readout["voltage_v"]), saved["readout"]["voltage_v"])
        assert "sequence_events" in run
    finally:
        reopened.close()
    assert reopened.callback.errors == 0


def test_one_server_per_store_folder(tmp_path):
    first = LocalTiledServer(tmp_path / "tiled")
    first.start()
    try:
        with pytest.raises(ValidationError, match="already serves"):
            LocalTiledServer(tmp_path / "tiled").start()
    finally:
        first.stop()
    second = LocalTiledServer(tmp_path / "tiled")
    second.start()
    second.stop()


def test_a_dead_tiled_server_never_fails_a_run(session):
    sheet = make_template(session.data_dir.parent / "runs.xlsx", overwrite=True)
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=math.inf))
    store = namespace["tiled_store"]
    store.server._process.kill()  # the server dies before the run
    store.server._process.wait()
    kwargs = queue_items(session, sheet, ["example-rest"], show=lambda text: None)[0]["kwargs"]
    assert namespace["RE"](namespace["run_from_sheet"](**kwargs)) is not None
    saved = read_run_file(namespace["run_file_writer"].path)
    assert saved["run"]["outcome"] == "completed"
    store.close()  # writes what the writer thread still holds
    # The whole run went to the backup instead, for later replay.
    backups = list((session.data_dir / "tiled" / "backup").iterdir())
    assert backups and any(path.stat().st_size for path in backups)


def test_errors_outside_the_writer_are_logged_not_raised(caplog):
    def broken(name, doc):
        raise RuntimeError("boom")

    guarded = SafeTiledWriter(broken)
    guarded("start", {"uid": "x"})
    assert guarded.flush(5)
    assert guarded.errors == 1
    assert "the run continues" in caplog.text
    guarded.close()


def test_an_existing_server_with_the_key_from_the_environment(tmp_path, monkeypatch):
    server = LocalTiledServer(tmp_path / "elsewhere")
    server.start()
    try:
        table = {"uri": server.uri, "api_key_env": "OE_TEST_TILED_KEY"}
        with pytest.raises(ValidationError, match="OE_TEST_TILED_KEY is not set"):
            open_tiled_store(table, tmp_path / "data")
        monkeypatch.setenv("OE_TEST_TILED_KEY", server.api_key)
        store = open_tiled_store(table, tmp_path / "data")
        assert store.server is None  # not ours to stop
        store.close()
        monkeypatch.setenv("OE_TEST_TILED_KEY", "0" * 64)
        with pytest.raises(ValidationError, match="cannot open the Tiled store"):
            open_tiled_store(table, tmp_path / "data")
    finally:
        server.stop()


def test_oe_tiled_serves_a_store_until_interrupted(session, tmp_path):
    import signal
    import subprocess
    import sys

    from tiled.client import from_uri

    from electrochemistry_bluesky.tiled_store import serve_main

    assert serve_main([str(tmp_path / "empty")]) == 1  # no store there
    namespace, uid = run_row(session, "example-rest")
    namespace["tiled_store"].close()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from electrochemistry_bluesky.tiled_store "
            "import serve_main; sys.exit(serve_main(sys.argv[1:]))",
            str(session.data_dir),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        lines = {}
        for line in process.stdout:
            key, _, value = line.partition(": ")
            lines[key] = value.strip()
            if key == "api key":
                break
        client = from_uri(lines["uri"], api_key=lines["api key"])
        assert client[uid].metadata["start"]["measurement_id"] == "example-rest"
    finally:
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=30) == 0
    assert not (session.data_dir / "tiled" / "server.json").exists()


def _open_local_store_and_return(data_dir):
    from electrochemistry_bluesky.tiled_store import open_tiled_store

    open_tiled_store({"local": True}, data_dir)  # closed only at process exit


@pytest.mark.parametrize("method", ["fork", "spawn"])
def test_the_server_stops_when_a_worker_process_ends(tmp_path, method):
    import multiprocessing
    import sys

    if method not in multiprocessing.get_all_start_methods():
        pytest.skip(f"no {method} start method here")
    if method == "fork" and sys.platform == "darwin":
        pytest.skip("fork after threads is unsafe on macOS (multiprocessing uses spawn there)")
    # The RE Manager worker is a multiprocessing.Process; a forked one exits by
    # os._exit, which skips atexit handlers.
    process = multiprocessing.get_context(method).Process(
        target=_open_local_store_and_return, args=(tmp_path,)
    )
    process.start()
    process.join(timeout=120)
    assert process.exitcode == 0
    assert not (tmp_path / "tiled" / "server.json").exists()
    server = LocalTiledServer(tmp_path / "tiled")  # the lock is free again
    server.start()
    server.stop()


def test_a_stalled_writer_never_blocks_the_run(tmp_path):
    import json
    import threading
    import time

    release = threading.Event()
    written = []

    def stalled(name, doc):  # a server that does not answer
        release.wait()
        written.append(name)

    guarded = SafeTiledWriter(stalled, backup_directory=tmp_path, max_pending=5)
    started = time.monotonic()
    guarded("start", {"uid": "run1"})
    for i in range(20):
        guarded("event_page", {"seq_num": [i]})
    guarded("stop", {"exit_status": "success"})
    assert time.monotonic() - started < 0.5  # the RunEngine never waited
    guarded.close(timeout_s=0.2)
    release.set()
    lines = sorted(json.loads(line) for line in (tmp_path / "run1-overflow.jsonl").open())
    # Tiled got (or will get) the first documents; the overflow file, sorted by
    # number, has the rest of the run without a gap.
    numbers = [line[0] for line in lines]
    assert numbers == list(range(numbers[0], 22)) and numbers[0] <= 6
    assert lines[-1] == [21, "stop", {"exit_status": "success"}]
    assert guarded.spilled == len(lines)
    assert guarded.errors == 0


def test_the_api_key_never_reaches_the_server_log(tmp_path):
    folder = tmp_path / "tiled"
    folder.mkdir()
    (folder / "server.log").write_text("old: http://127.0.0.1:1?api_key=0123abcd\n")
    server = LocalTiledServer(folder)
    server.start()
    try:
        log = folder / "server.log"
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        text = log.read_text()
        assert server.api_key not in text and "0123abcd" not in text
        assert "api_key=<api-key>" in text
    finally:
        server.stop()


def test_a_refused_server_leaves_the_running_one_alone(tmp_path):
    first = LocalTiledServer(tmp_path / "tiled")
    first.start()
    try:
        second = LocalTiledServer(tmp_path / "tiled")
        with pytest.raises(ValidationError, match=f"pid {first._process.pid}"):
            second.start()
        second.stop()
        assert (tmp_path / "tiled" / "server.json").exists()
        assert (tmp_path / "tiled" / "api-key").exists()
    finally:
        first.stop()


def _run_until_killed(session_path):
    from electrochemistry_bluesky import SimulatedSequenceBackend
    from electrochemistry_bluesky.queueserver import build_namespace, load_session, queue_items
    from electrochemistry_bluesky.run_sheet import make_template

    session = load_session(session_path)
    sheet = make_template(session.data_dir.parent / "runs.xlsx", overwrite=True)
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=1.0))
    kwargs = queue_items(session, sheet, ["example-rest"], show=lambda text: None)[0]["kwargs"]
    namespace["RE"](namespace["run_from_sheet"](**kwargs))  # killed long before its end


def test_a_killed_worker_takes_its_server_down_and_the_store_keeps_the_start(session, tmp_path):
    import json
    import multiprocessing
    import os
    import signal
    import time

    process = multiprocessing.get_context("spawn").Process(
        target=_run_until_killed, args=(session.source_path,)
    )
    process.start()
    try:
        deadline = time.monotonic() + 120
        while not list(session.data_dir.glob("example-rest_*.h5")):
            assert time.monotonic() < deadline and process.is_alive()
            time.sleep(0.2)
        time.sleep(2)
        server_pid = json.loads((session.data_dir / "tiled" / "server.json").read_text())["pid"]
        os.kill(process.pid, signal.SIGKILL)  # as a killed RE Manager worker or kernel
        process.join(10)
        deadline = time.monotonic() + 30
        while True:  # the server notices within seconds and stops
            try:
                os.kill(server_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "the Tiled server outlived its worker"
            time.sleep(0.2)
    finally:
        if process.is_alive():
            process.kill()
    # The store opens again; the interrupted run has its start and no stop.
    (path,) = session.data_dir.glob("example-rest_*.h5")
    saved = read_run_file(path)
    store = open_tiled_store(session.tiled, session.data_dir)
    try:
        run = store.client[saved["provenance"]["bluesky_uid"]]
        assert run.metadata["start"]["measurement_id"] == "example-rest"
        assert not run.metadata.get("stop")
    finally:
        store.close()
