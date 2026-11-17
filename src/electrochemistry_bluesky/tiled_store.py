"""Optional Tiled copy of each run's Bluesky documents; the HDF5 run file stays the record.

A session's ``[tiled]`` table chooses one of two stores:

- ``local = true``: this process starts a Tiled server whose catalog and data live
  in ``<data_dir>/tiled``, beside the run files, and stops it on exit. Each start
  generates a new API key; ``server.json`` and ``api-key`` (mode 0600) in that
  folder tell readers how to connect while it runs. A lock keeps a second
  session from serving the same store.
- ``uri = "..."``: an existing Tiled server; ``api_key_env`` names the
  environment variable holding its API key, which never goes in the session
  file (that file's text is stored in every run file).

Writing never waits for or fails a run: the RunEngine only hands each document
to a bounded queue, and a background thread writes it to Tiled. ``TiledWriter``
writes rows in batches of ``BATCH_ROWS``; the writer thread also writes the rows
waiting in a batch every ``flush_s`` seconds (session setting, default 10 s), so
with a responsive server a reader sees an open run's rows within about that
time (plus the poll period); a slow server delays them by its queue and the
client's retries (up to 45 s per failed write). ``TiledWriter`` sends
a run whose Tiled write fails to JSON-lines files in ``<data_dir>/tiled/backup``
instead (the whole run, for replay). If the queue fills (a slow or stalled
server), or documents are still queued when the store closes, the rest of that
run goes to ``backup/<uid>-overflow.jsonl``. Other errors are logged and
dropped. A hard-killed process loses what was still queued or not yet written
(with a responsive server, about the last ``flush_s``); the HDF5 run file stays
complete. Needs the ``tiled`` extra.

The copy's ``readout`` descriptor has hints naming voltage and current, the
fields for generic viewers to plot (``_readout_hints``).

A local store keeps tables in SQLite (``tables.sqlite``, write-ahead log, so
readers never fail while a run is written). A store made by an earlier version
also has ``tables.duckdb``; its tables stay readable, but DuckDB serves one request
at a time (Tiled 0.2.21 shares one connection), so parallel reads of those old
tables can fail.
"""

import argparse
import atexit
import contextlib
import importlib.metadata
import json
import logging
import math
import multiprocessing.util
import os
import queue
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from keithley2460_ophyd.exceptions import ValidationError

logger = logging.getLogger(__name__)

_KEYS = {"local", "uri", "api_key_env", "timeout_s", "flush_s"}
STORE_DIRECTORY = "tiled"
# Rows per table write at high rates (6 s at 6 ms); slower runs are written every flush_s.
BATCH_ROWS = 1000
# Longest time a read row waits before the writer thread writes it to Tiled.
FLUSH_S = 10.0
# bluesky-tiled-plugins release whose TiledWriter internals _RowFlusher uses
# (pinned in pyproject.toml; tests/unit/test_tiled_store.py checks it).
TESTED_PLUGINS = "2.0.13"
# Documents waiting for the writer thread (an event page per poll, ~1 s of data).
MAX_PENDING = 1000
_KEY_IN_LOG = re.compile(r"(api_key=)[^\s&\"']+")


def check_tiled(table: Mapping[str, object] | None) -> dict[str, Any]:
    """Validate a session's ``[tiled]`` table; empty means no Tiled store."""
    if not table:
        return {}
    if not isinstance(table, Mapping):
        raise ValidationError("tiled must be a table")
    unknown = table.keys() - _KEYS
    if unknown:
        raise ValidationError(f"tiled: unknown settings {', '.join(sorted(map(str, unknown)))}")
    local, uri = table.get("local", False), table.get("uri")
    if not isinstance(local, bool):
        raise ValidationError("tiled: local must be true or false")
    if local == (uri is not None):
        raise ValidationError("tiled: set either local = true or uri, not both")
    if uri is not None and (
        not isinstance(uri, str) or not uri.startswith(("http://", "https://"))
    ):
        raise ValidationError("tiled: uri must be an http(s) URL")
    key_env = table.get("api_key_env")
    if key_env is not None:
        if local:
            raise ValidationError("tiled: a local store generates its own API key")
        if not isinstance(key_env, str) or not key_env.isidentifier():
            raise ValidationError("tiled: api_key_env must name an environment variable")
    numbers = {}
    for key, default in (("timeout_s", 10.0), ("flush_s", FLUSH_S)):
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"tiled: {key} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ValidationError(f"tiled: {key} must be positive")
        numbers[key] = float(value)
    return {**dict(table), **numbers}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class LocalTiledServer:
    """A Tiled server on 127.0.0.1 for one store folder, as a child process."""

    def __init__(self, directory: str | Path, *, startup_timeout_s: float = 60.0) -> None:
        self.directory = Path(directory).resolve()
        self.startup_timeout_s = startup_timeout_s
        self.uri: str | None = None
        self.api_key: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._lock: IO[str] | None = None
        self._log: IO[bytes] | None = None
        self._owns_files = False  # server.json and api-key were written by this instance

    def _acquire_lock(self) -> None:
        self._lock = (self.directory / "server.lock").open("a")
        try:
            import fcntl
        except ImportError:  # not POSIX: no lock
            return
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            self._lock = None
            try:
                running = json.loads((self.directory / "server.json").read_text())
                who = f" (pid {running['pid']}, {running['uri']})"
            except (OSError, ValueError, KeyError):
                who = ""
            raise ValidationError(
                f"another Tiled server already serves {self.directory}{who}; "
                "use one session per data folder. A server whose session was killed "
                "stops within seconds; if it does not, stop that pid."
            ) from exc

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._launch()
        except BaseException:
            self.stop()
            raise

    def _launch(self) -> None:
        catalog = self.directory / "catalog.db"
        tables = self.directory / "tables.sqlite"
        # Write-ahead log (kept in the file): readers and the writer do not block
        # each other. Tiled's default SQLite journal fails reads and writes with
        # "database is locked" when they overlap.
        with contextlib.closing(sqlite3.connect(tables)) as connection:
            (mode,) = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if str(mode).lower() != "wal":  # SQLite keeps the old mode without an error
            raise ValidationError(
                f"cannot use a write-ahead log for {tables} (journal mode {mode}); "
                "keep data_dir on a local disk"
            )
        legacy = self.directory / "tables.duckdb"  # stores made by earlier versions
        port = _free_port()
        # Tiled accepts alphanumeric API keys only.
        self.api_key = secrets.token_hex(32)
        args = [
            sys.executable,
            "-m",
            "electrochemistry_bluesky._tiled_server",
            str(os.getpid()),  # the server stops when this process is gone
            "serve",
            "catalog",
            str(catalog),
            "--write",
            str(self.directory / "data"),
            "--write",
            f"sqlite:///{tables}",
            *(["--read", f"duckdb:///{legacy}"] if legacy.exists() else []),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        if not catalog.exists():
            args.append("--init")
        self._log = _private_log(self.directory / "server.log")
        # The key goes through the environment, not the command line (ps shows that);
        # the wrapper redacts it from the server's output.
        env = {**os.environ, "TILED_SINGLE_USER_API_KEY": self.api_key}
        # The server inherits the lock: if this process dies first, the lock is held
        # until the server is gone too, so no second server opens the same catalog.
        lock_fd = [self._lock.fileno()] if self._lock is not None else []
        self._process = subprocess.Popen(
            args,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            pass_fds=lock_fd,
        )
        uri = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.startup_timeout_s
        while True:
            if self._process.poll() is not None:
                tail = (self.directory / "server.log").read_text(errors="replace")[-2000:]
                raise ValidationError(f"the local Tiled server exited at start:\n{tail}")
            try:
                with urllib.request.urlopen(f"{uri}/api/v1/", timeout=2):
                    break
            except urllib.error.HTTPError:
                break  # it answers
            except OSError:
                if time.monotonic() > deadline:
                    raise ValidationError(
                        f"the local Tiled server did not answer within "
                        f"{self.startup_timeout_s:g} s (log: {self.directory / 'server.log'})"
                    ) from None
                time.sleep(0.2)
        self.uri = uri
        key_file = self.directory / "api-key"
        key_file.unlink(missing_ok=True)
        self._owns_files = True
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(self.api_key + "\n")
        (self.directory / "server.json").write_text(
            json.dumps({"uri": uri, "pid": self._process.pid}) + "\n"
        )

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        self._process = None
        if self._owns_files:  # never remove the files of a server another process runs
            for name in ("server.json", "api-key"):
                (self.directory / name).unlink(missing_ok=True)
            self._owns_files = False
        for stream in (self._log, self._lock):
            if stream is not None:
                stream.close()  # closing the lock file releases the lock
        self._log = self._lock = None


# Readout fields generic viewers should plot.
READOUT_PLOT = ("voltage_v", "current_a")


def _readout_hints(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """A ``readout`` descriptor with hints naming ``READOUT_PLOT`` for its object.

    The flyer cannot carry these hints itself: Bluesky gives an object's hints
    to every stream it collects, and ``sequence_events`` has no such fields
    (BEC fails on hinted fields a stream lacks). So they are added here, to the
    Tiled copy's descriptor only.
    """
    if doc.get("name") != "readout":
        return doc
    for name, keys in doc.get("object_keys", {}).items():
        if set(READOUT_PLOT) <= set(keys):
            hints = {**doc.get("hints", {}), name: {"fields": list(READOUT_PLOT)}}
            return {**doc, "hints": hints}
    return doc


class _RowFlusher:
    """Writes the rows ``TiledWriter`` holds for a batch, before the batch is full.

    Interim, until bluesky-tiled-plugins can flush on its own: it uses three
    private names of ``_RunWriter`` (``_internal_data_cache``,
    ``_write_internal_data``, ``_desc_nodes``) as of ``TESTED_PLUGINS``, which
    the ``tiled`` extra pins. ``normalizer`` goes to ``TiledWriter(normalizer=)``
    and records each run's writer when the run starts. A run whose timed write
    fails gets the error at its next document, inside ``TiledWriter``, which
    then sends the whole run to its backup, as for a failed batch. Called on the
    writer thread only. The normalizer also adds the readout hints
    (``_readout_hints``).
    """

    def __init__(self) -> None:
        from bluesky_tiled_plugins.writing.tiled_writer import RunNormalizer

        self._open: dict[int, Any] = {}  # normalizer id -> that run's _RunWriter
        self._failed: dict[int, Exception] = {}
        flusher = self

        class _Normalizer(RunNormalizer):  # type: ignore[misc]
            def subscribe(self, func: Any, name: str = "all") -> Any:
                flusher._open[id(self)] = func
                return super().subscribe(func, name)

            def __call__(self, name: str, doc: Any, *args: Any, **kwargs: Any) -> Any:
                key = id(self)
                try:
                    if (error := flusher._failed.pop(key, None)) is not None:
                        raise error
                    if name == "descriptor":
                        doc = _readout_hints(doc)
                    result = super().__call__(name, doc, *args, **kwargs)
                except BaseException:
                    # TiledWriter abandons a run whose writer raised: no timed writes.
                    flusher._open.pop(key, None)
                    raise
                if name == "stop":
                    flusher._open.pop(key, None)
                return result

        self.normalizer = _Normalizer

    def write_pending_rows(self) -> None:
        for key, run in list(self._open.items()):
            try:
                for stream, rows in run._internal_data_cache.items():
                    if rows:
                        run._write_internal_data(rows, desc_node=run._desc_nodes[stream])
                        rows.clear()
            except Exception as exc:
                del self._open[key]
                self._failed[key] = exc
                logger.error("Tiled write failed (%s); the run goes to the backup folder", exc)


class SafeTiledWriter:
    """Hands documents to a writer thread: Tiled never blocks or fails the RunEngine.

    Documents wait in a queue of at most ``max_pending``. When it is full, the
    rest of that run is appended to ``<backup>/<uid>-overflow.jsonl`` instead; so
    are documents still queued when ``close`` gives up waiting. Each line is
    ``[number, name, document]``, numbered in RunEngine order: sort by the number
    to replay; Tiled holds the documents numbered before the file's first.

    With ``write_pending_rows`` (a ``_RowFlusher``'s), the thread calls it every
    ``flush_s`` seconds, so rows reach Tiled before a batch is full.
    """

    def __init__(
        self,
        writer: Any,
        *,
        backup_directory: str | Path | None = None,
        max_pending: int = MAX_PENDING,
        write_pending_rows: Any = None,
        flush_s: float = FLUSH_S,
    ) -> None:
        self._writer = writer
        self._write_pending_rows = write_pending_rows
        self._flush_s = flush_s
        self._backup = Path(backup_directory) if backup_directory is not None else None
        self._queue: queue.Queue[tuple[int, str, str, Mapping[str, Any]] | None] = queue.Queue(
            max_pending
        )
        self._run_uid = ""
        self._number = 0
        self._spilling: set[str] = set()
        self._closed = False
        self.errors = 0
        self.spilled = 0
        self._thread = threading.Thread(target=self._work, name="tiled-writer", daemon=True)
        self._thread.start()

    def __call__(self, name: str, doc: Mapping[str, Any]) -> None:
        try:
            if name == "start":
                self._run_uid, self._number = str(doc["uid"]), 0
            uid, number = self._run_uid, self._number
            self._number += 1
            if self._closed or uid in self._spilling:
                self._spill(number, uid, name, doc)
                return
            try:
                self._queue.put_nowait((number, uid, name, doc))
            except queue.Full:
                self._spilling.add(uid)
                logger.error(
                    "Tiled is %d documents behind; the rest of run %s goes to the backup "
                    "folder; the run continues",
                    self._queue.maxsize,
                    uid,
                )
                self._spill(number, uid, name, doc)
        except Exception:
            self.errors += 1
            logger.exception("Tiled copy failed on a %r document; the run continues", name)

    def _work(self) -> None:
        due = time.monotonic() + self._flush_s
        while True:
            timeout = None
            if self._write_pending_rows is not None:
                timeout = max(0.0, due - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                pass  # time to write the pending rows
            else:
                if item is None:
                    break
                _, _, name, doc = item
                try:
                    self._writer(name, doc)
                except Exception:
                    self.errors += 1
                    logger.exception("Tiled copy failed on a %r document; the run continues", name)
                finally:
                    self._queue.task_done()
            if self._write_pending_rows is not None and time.monotonic() >= due:
                try:
                    self._write_pending_rows()
                except Exception:
                    self.errors += 1
                    logger.exception("Tiled timed write failed; the run continues")
                due = time.monotonic() + self._flush_s
        self._queue.task_done()

    def _spill(self, number: int, uid: str, name: str, doc: Mapping[str, Any]) -> None:
        self.spilled += 1
        if self._backup is None:
            return
        self._backup.mkdir(parents=True, exist_ok=True)
        with (self._backup / f"{uid or 'unknown'}-overflow.jsonl").open("a") as stream:
            stream.write(json.dumps([number, name, doc], default=_plain) + "\n")

    def flush(self, timeout_s: float) -> bool:
        """Wait until every queued document is written; False on timeout."""
        deadline = time.monotonic() + timeout_s
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self, timeout_s: float = 20.0) -> None:
        """Write what is queued (spilling the rest after ``timeout_s``), then stop."""
        if self._closed:
            return
        self._closed = True
        if not self.flush(timeout_s):
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    self._spill(*item)
                self._queue.task_done()
            logger.error("Tiled did not take every document in time; the rest is in backup")
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # the thread is stuck in a write; it is a daemon thread
            pass
        self._thread.join(timeout=1.0)


def _plain(value: Any) -> Any:
    """JSON for numpy values in documents."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _private_log(path: Path) -> IO[bytes]:
    """Open the server log 0600 for appending; redact keys an older log holds."""
    if path.exists():
        os.chmod(path, 0o600)
        text = path.read_text(errors="replace")
        redacted = _KEY_IN_LOG.sub(r"\1<api-key>", text)
        if redacted != text:
            path.write_text(redacted)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    return os.fdopen(descriptor, "ab")


class TiledStore:
    """The open store: its callback for the RunEngine, and the local server, if any."""

    def __init__(self, callback: SafeTiledWriter, client: Any, server: LocalTiledServer | None):
        self.callback, self.client, self.server = callback, client, server

    def close(self) -> None:
        self.callback.close()
        if self.server is not None:
            self.server.stop()
            self.server = None


def open_tiled_store(tiled: Mapping[str, Any], data_dir: str | Path) -> TiledStore | None:
    """Connect to (or start) the session's Tiled store; None without a ``[tiled]`` table.

    Fails loudly when the store cannot be opened, so the operator sees it before
    any run; remove the ``[tiled]`` table to measure without a Tiled copy.
    """
    settings = check_tiled(tiled)
    if not settings:
        return None
    try:
        from bluesky_tiled_plugins import TiledWriter
        from tiled.client import from_uri
    except ImportError as exc:
        raise ValidationError("a [tiled] session needs the 'tiled' extra installed") from exc
    plugins = importlib.metadata.version("bluesky-tiled-plugins")
    if plugins != TESTED_PLUGINS:  # _RowFlusher relies on that release's internals
        raise ValidationError(
            f"bluesky-tiled-plugins {plugins} is installed; the Tiled store needs exactly "
            f"{TESTED_PLUGINS} (install the 'tiled' extra of this package)"
        )
    store_dir = Path(data_dir).expanduser().resolve() / STORE_DIRECTORY
    (store_dir / "backup").mkdir(parents=True, exist_ok=True)
    server = None
    if settings.get("local"):
        server = LocalTiledServer(store_dir)
        server.start()
        uri, api_key = server.uri, server.api_key
    else:
        uri = settings["uri"]
        key_env = settings.get("api_key_env")
        api_key = None
        if key_env is not None:
            api_key = os.environ.get(key_env)
            if not api_key:
                raise ValidationError(f"tiled: environment variable {key_env} is not set")
    try:
        client = from_uri(uri, api_key=api_key, timeout=settings["timeout_s"])
        flusher = _RowFlusher()
        writer = TiledWriter(
            client,
            backup_directory=str(store_dir / "backup"),
            batch_size=BATCH_ROWS,
            normalizer=flusher.normalizer,
        )
    except Exception as exc:
        if server is not None:
            server.stop()
        raise ValidationError(f"cannot open the Tiled store at {uri}: {exc}") from exc
    callback = SafeTiledWriter(
        writer,
        backup_directory=store_dir / "backup",
        write_pending_rows=flusher.write_pending_rows,
        flush_s=settings["flush_s"],
    )
    store = TiledStore(callback, client, server)
    if server is not None:
        # Stop the server when this process ends: atexit for a plain process;
        # multiprocessing's finalizers for a worker Process (the RE Manager's),
        # which exits without running atexit handlers.
        atexit.register(store.close)
        multiprocessing.util.Finalize(store, store.close, exitpriority=10)
    return store


def serve_main(argv: list[str] | None = None) -> int:
    """Serve a data folder's Tiled store (from a [tiled] local session) until Ctrl-C."""
    parser = argparse.ArgumentParser(prog="oe-tiled", description=serve_main.__doc__)
    parser.add_argument("data_dir", type=Path, help="the session's data_dir")
    store = parser.parse_args(argv).data_dir.expanduser().resolve() / STORE_DIRECTORY
    if not (store / "catalog.db").exists():
        print(f"No Tiled store in {store}")
        return 1
    server = LocalTiledServer(store)
    try:
        server.start()
        print(
            f"Tiled store {store}\nuri: {server.uri}\napi key: {server.api_key}\n"
            "Python: from tiled.client import from_uri; "
            "runs = from_uri(uri, api_key=key)\nCtrl-C stops the server.",
            flush=True,
        )
        assert server._process is not None
        while server._process.poll() is None:
            time.sleep(0.5)
        print("The Tiled server exited; see server.log in the store.")
        return 1
    except KeyboardInterrupt:
        return 0
    except ValidationError as exc:
        print(exc)
        return 1
    finally:
        server.stop()
