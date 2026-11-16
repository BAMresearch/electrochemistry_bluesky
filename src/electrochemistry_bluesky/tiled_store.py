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

Writing never affects a run. ``TiledWriter`` sends a run whose Tiled write fails
to JSON-lines files in ``<data_dir>/tiled/backup`` instead (the whole run, for
replay), and any other error is logged and dropped. Needs the ``tiled`` extra.
"""

import argparse
import atexit
import json
import logging
import math
import multiprocessing.util
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from keithley2460_ophyd.exceptions import ValidationError

logger = logging.getLogger(__name__)

_KEYS = {"local", "uri", "api_key_env", "timeout_s"}
STORE_DIRECTORY = "tiled"


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
    timeout = table.get("timeout_s", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValidationError("tiled: timeout_s must be a number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValidationError("tiled: timeout_s must be positive")
    return {**dict(table), "timeout_s": float(timeout)}


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
            raise ValidationError(
                f"another Tiled server already serves {self.directory}; "
                "use one session per data folder"
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
        port = _free_port()
        # Tiled accepts alphanumeric API keys only.
        self.api_key = secrets.token_hex(32)
        args = [
            sys.executable,
            "-m",
            "tiled",
            "serve",
            "catalog",
            str(catalog),
            "--write",
            str(self.directory / "data"),
            "--write",
            f"duckdb:///{self.directory / 'tables.duckdb'}",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        if not catalog.exists():
            args.append("--init")
        self._log = (self.directory / "server.log").open("ab")
        # The key goes through the environment, not the command line (ps shows that).
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
        for name in ("server.json", "api-key"):
            (self.directory / name).unlink(missing_ok=True)
        for stream in (self._log, self._lock):
            if stream is not None:
                stream.close()  # closing the lock file releases the lock
        self._log = self._lock = None


class SafeTiledWriter:
    """A ``TiledWriter`` whose failures never reach the RunEngine."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self.errors = 0

    def __call__(self, name: str, doc: Mapping[str, Any]) -> None:
        try:
            self._writer(name, doc)
        except Exception:
            self.errors += 1
            logger.exception("Tiled copy failed on a %r document; the run continues", name)


class TiledStore:
    """The open store: its callback for the RunEngine, and the local server, if any."""

    def __init__(self, callback: SafeTiledWriter, client: Any, server: LocalTiledServer | None):
        self.callback, self.client, self.server = callback, client, server

    def close(self) -> None:
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
        writer = TiledWriter(client, backup_directory=str(store_dir / "backup"))
    except Exception as exc:
        if server is not None:
            server.stop()
        raise ValidationError(f"cannot open the Tiled store at {uri}: {exc}") from exc
    store = TiledStore(SafeTiledWriter(writer), client, server)
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
