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
    assert check_tiled({"local": True}) == {"local": True, "timeout_s": 10.0, "flush_s": 10.0}
    assert check_tiled({"local": True, "flush_s": 2})["flush_s"] == 2.0
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
        ({"local": True, "flush_s": 0}, "flush_s must be positive"),
        ({"local": True, "flush_s": "10"}, "flush_s must be a number"),
        ({"local": True, "api_key": "secret"}, "unknown settings api_key"),
        (["local"], "must be a table"),
    ],
)
def test_bad_tiled_settings(table, message):
    with pytest.raises(ValidationError, match=message):
        check_tiled(table)


def test_session_reads_and_records_the_tiled_table(session):
    assert session.tiled == {"local": True, "timeout_s": 10.0, "flush_s": 10.0}
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
            "flush_s": 10.0,
        }
        # Which set-up made the run, for catalogs and timelines.
        assert run.metadata["start"]["instrument"] == "electrochemistry-k2460"
        assert run.metadata["start"]["station"] == "simulator"
        assert saved["session"]["settings"] == run.metadata["start"]["session"]["settings"]
        readout = run["readout"].read()
        np.testing.assert_allclose(np.asarray(readout["t_s"]), saved["readout"]["t_s"])
        np.testing.assert_allclose(np.asarray(readout["voltage_v"]), saved["readout"]["voltage_v"])
        assert "sequence_events" in run
        # Generic viewers plot the hinted fields: voltage and current of the readout only.
        assert run["readout"].metadata["hints"] == {"k2460": {"fields": ["voltage_v", "current_a"]}}
        assert run["sequence_events"].metadata["hints"] == {"k2460": {"fields": []}}
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


def _run_in_real_time(session_path, sheet=None, measurement_id="example-rest"):
    from electrochemistry_bluesky import SimulatedSequenceBackend
    from electrochemistry_bluesky.queueserver import build_namespace, load_session, queue_items
    from electrochemistry_bluesky.run_sheet import make_template

    session = load_session(session_path)
    if sheet is None:
        sheet = make_template(session.data_dir.parent / "runs.xlsx", overwrite=True)
    namespace = build_namespace(session, backend=SimulatedSequenceBackend(speed=1.0))
    kwargs = queue_items(session, sheet, [measurement_id], show=lambda text: None)[0]["kwargs"]
    namespace["RE"](namespace["run_from_sheet"](**kwargs))


def test_a_killed_worker_takes_its_server_down_and_the_store_keeps_the_start(session, tmp_path):
    import json
    import multiprocessing
    import os
    import signal
    import time

    process = multiprocessing.get_context("spawn").Process(
        target=_run_in_real_time, args=(session.source_path,)
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


def test_the_row_flusher_is_written_for_the_installed_plugins():
    """_RowFlusher uses TiledWriter internals of one release: update both together."""
    import importlib.metadata

    from bluesky_tiled_plugins.writing.tiled_writer import _RunWriter

    from electrochemistry_bluesky.tiled_store import TESTED_PLUGINS

    assert importlib.metadata.version("bluesky-tiled-plugins") == TESTED_PLUGINS, (
        "check _RowFlusher against this bluesky-tiled-plugins release (or use its own "
        "flush, if it has one), then update TESTED_PLUGINS and the pin in pyproject.toml"
    )
    assert callable(_RunWriter._write_internal_data)


def test_another_plugins_release_is_refused(tmp_path, monkeypatch):
    import importlib.metadata

    real = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "9.9.9" if name == "bluesky-tiled-plugins" else real(name),
    )
    with pytest.raises(ValidationError, match="needs exactly 2.0.13"):
        open_tiled_store({"local": True}, tmp_path)
    assert not (tmp_path / "tiled" / "server.json").exists()  # nothing was started


def _synthetic_run(callback, rows, *, start=0, bundle=None, descriptor=None, stop=True):
    """Feed a small run (or more rows of one) to ``callback``; return bundle and descriptor."""
    import time

    from event_model import compose_run

    if bundle is None:
        bundle = compose_run(metadata={"plan_name": "synthetic"})
        callback("start", bundle.start_doc)
        keys = {k: {"source": k, "dtype": "number", "shape": []} for k in ("t_s", "voltage_v")}
        descriptor = bundle.compose_descriptor(name="readout", data_keys=keys)
        callback("descriptor", descriptor.descriptor_doc)
    if rows:
        numbers = list(range(start, start + rows))
        callback(
            "event_page",
            descriptor.compose_event_page(
                data={"t_s": [float(n) for n in numbers], "voltage_v": [0.1] * rows},
                timestamps={"t_s": [time.time()] * rows, "voltage_v": [time.time()] * rows},
                seq_num=[n + 1 for n in numbers],
                time=[time.time()] * rows,
            ),
        )
    if stop:
        callback("stop", bundle.compose_stop())
    return bundle, descriptor


def _rows(client, uid):
    try:
        return len(client[uid]["readout"].read()["t_s"])
    except Exception:  # no table before the first write
        return 0


def test_an_open_run_reaches_tiled_within_flush_s_and_readers_never_fail(tmp_path):
    """A slow run (1 row/s) is in Tiled long before 1000 rows; parallel full reads
    of a finished and the open run, from 12 threads, all succeed."""
    import json
    import multiprocessing
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tiled.client import from_uri

    from electrochemistry_bluesky.run_sheet import write_run_sheet

    path = tmp_path / "session.toml"
    path.write_text(
        f'backend = "simulator"\ndata_dir = "{tmp_path / "data"}"\nsim_speed = inf\n'
        "[limits]\nv_min_abs = 0.0\nv_max_abs = 3.0\ni_max_abs = 0.01\n"
        "max_duration_s = 200000.0\nmax_records = 1000000\n"
        "[tiled]\nlocal = true\nflush_s = 1\n"
    )
    session = load_session(path)
    namespace, finished = run_row(session)  # a finished pulse train, 4,000 rows
    namespace["tiled_store"].close()
    sheet = write_run_sheet(
        tmp_path / "slow.xlsx", [dict(measurement_id="slow-rest", protocol="rest", rest_s=15)]
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_run_in_real_time, args=(path, sheet, "slow-rest")
    )
    process.start()
    try:
        folder = session.data_dir / "tiled"
        deadline = time.monotonic() + 120
        while not list(session.data_dir.glob("slow-rest_*.h5")):
            assert time.monotonic() < deadline and process.is_alive()
            time.sleep(0.1)
        opened = time.monotonic()  # the RunStart was just emitted; t_s starts soon after
        server = json.loads((folder / "server.json").read_text())
        client = from_uri(server["uri"], api_key=(folder / "api-key").read_text().strip())

        def age_of_newest_row():
            """Seconds since the newest row in Tiled was sampled, on the run's clock."""
            t_s = np.asarray(client[uid]["readout"].read()["t_s"])
            return time.monotonic() - opened - float(t_s.max())

        # flush_s + poll period + sampling interval, plus 2 s for writes and reads
        bound = 1 + 1 + 1 + 2
        uid = None
        while uid is None or _rows(client, uid) < 2:
            assert time.monotonic() - opened < 30, "no rows in Tiled"
            uid = uid or next(
                (u for u in client if client[u].metadata["start"]["measurement_id"] == "slow-rest"),
                None,
            )
            time.sleep(0.2)
        assert age_of_newest_row() < bound
        with ThreadPoolExecutor(12) as pool:
            counts = list(pool.map(lambda u: _rows(client, u), [finished, uid] * 16))
        finished_rows = len(read_run_file(namespace["run_file_writer"].path)["readout"]["t_s"])
        assert counts[0::2] == [finished_rows] * 16
        assert all(count >= 2 for count in counts[1::2])
        while time.monotonic() - opened < 10:  # later in the run: fresh rows again
            time.sleep(0.2)
        assert not client[uid].metadata.get("stop")  # the run is still open
        assert age_of_newest_row() < bound and _rows(client, uid) >= 6
        process.join(60)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
    (saved,) = session.data_dir.glob("slow-rest_*.h5")
    store = open_tiled_store(session.tiled, session.data_dir)
    try:  # at the stop document every row is there
        assert _rows(store.client, uid) == len(read_run_file(saved)["readout"]["t_s"])
        assert store.client[uid].metadata["stop"]["exit_status"] == "success"
    finally:
        store.close()


def test_a_failed_timed_write_sends_the_whole_run_to_the_backup(tmp_path, monkeypatch, caplog):
    import json
    import time

    import tiled.client.utils

    monkeypatch.setattr(tiled.client.utils, "TILED_RETRY_TIMEOUT", 1.0)  # default 45 s
    store = open_tiled_store({"local": True, "flush_s": 0.2}, tmp_path)
    try:
        bundle, descriptor = _synthetic_run(store.callback, 3, stop=False)
        uid = bundle.start_doc["uid"]
        deadline = time.monotonic() + 30
        while _rows(store.client, uid) < 3:  # written by the timer, not a batch
            assert time.monotonic() < deadline
            time.sleep(0.1)
        store.server._process.kill()
        store.server._process.wait()
        _synthetic_run(store.callback, 3, start=3, bundle=bundle, descriptor=descriptor, stop=False)
        deadline = time.monotonic() + 30
        while "Tiled write failed" not in caplog.text:  # the timed write, before the stop
            assert time.monotonic() < deadline
            time.sleep(0.1)
        _synthetic_run(store.callback, 0, bundle=bundle, descriptor=descriptor)
        assert store.callback.flush(30)
    finally:
        store.close()
    (backup,) = (tmp_path / "tiled" / "backup").glob(f"*{uid[:8]}*.jsonl")
    names = [json.loads(line)["name"] for line in backup.open()]
    assert names == ["start", "descriptor", "event_page", "event_page", "stop"]


def test_a_store_with_duckdb_tables_stays_readable(tmp_path):
    """Stores made by earlier versions keep tables in DuckDB; new tables go to SQLite."""
    import os
    import secrets
    import subprocess
    import sys
    import time
    import urllib.request

    from bluesky_tiled_plugins import TiledWriter
    from tiled.client import from_uri

    from electrochemistry_bluesky.tiled_store import _free_port

    folder = tmp_path / "tiled"
    folder.mkdir()
    port, key = _free_port(), secrets.token_hex(16)
    old = subprocess.Popen(
        [sys.executable, "-m", "electrochemistry_bluesky._tiled_server", str(os.getpid())]
        + ["serve", "catalog", str(folder / "catalog.db"), "--init"]
        + ["--write", str(folder / "data"), "--write", f"duckdb:///{folder / 'tables.duckdb'}"]
        + ["--host", "127.0.0.1", "--port", str(port)],
        env={**os.environ, "TILED_SINGLE_USER_API_KEY": key},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/", timeout=1)
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.2)
        bundle, _ = _synthetic_run(
            TiledWriter(from_uri(f"http://127.0.0.1:{port}", api_key=key)), 5
        )
    finally:
        old.terminate()
        old.wait(10)
    store = open_tiled_store({"local": True}, tmp_path)
    try:
        old_uid = bundle.start_doc["uid"]
        assert _rows(store.client, old_uid) == 5
        new, _ = _synthetic_run(store.callback, 4)
        assert store.callback.flush(30)
        assert _rows(store.client, new.start_doc["uid"]) == 4

        def scheme(uid):
            table = store.client[uid]["readout"].base["internal"]
            return table.data_sources()[0].assets[0].data_uri.split(":")[0]

        assert (scheme(old_uid), scheme(new.start_doc["uid"])) == ("duckdb", "sqlite")
    finally:
        store.close()
