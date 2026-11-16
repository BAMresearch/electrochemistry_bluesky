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
    # The whole run went to the backup instead, for later replay.
    backups = list((session.data_dir / "tiled" / "backup").iterdir())
    assert backups and any(path.stat().st_size for path in backups)
    store.close()


def test_errors_outside_the_writer_are_logged_not_raised(caplog):
    def broken(name, doc):
        raise RuntimeError("boom")

    guarded = SafeTiledWriter(broken)
    guarded("start", {"uid": "x"})
    assert guarded.errors == 1
    assert "the run continues" in caplog.text


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
