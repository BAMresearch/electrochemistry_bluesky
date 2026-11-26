"""qserver/start-k2460-dashboard.sh with a recording docker-compose: its own
project and ports, the session's local Tiled, and no secret ever printed."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "qserver" / "start-k2460-dashboard.sh"
FILES = ["-f", "deploy/compose.yaml", "-f", "deploy/compose.host.yaml"]
RECORDED = [
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "DOCKER_HOST",
    "DOCKER_BUILDKIT",
    "ETT_TILED_URL",
    "ETT_TILED_KEY_FILE",
    "ETT_QUEUE_KEY_FILE",
    "ETT_QUEUES",
    "ETT_CATALOG_ROOTS",
    "ETT_DEFAULT_PRESET",
    "ETT_ADAPTER_USER",
    "ETT_HTTP_PORT",
    "ETT_GRAFANA_PORT",
    "ETT_ADAPTER_PORT",
    "ETT_PUBLIC_URL",
    "GF_ADMIN_PASSWORD",
]
RECORDER = f"""#!{sys.executable}
import json, os, sys
names = {RECORDED!r}
call = {{"program": os.path.basename(sys.argv[0]), "args": sys.argv[1:], "cwd": os.getcwd(),
        "env": {{name: os.environ.get(name) for name in names}}}}
with open(os.environ["DASHBOARD_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps(call) + "\\n")
"""


def _secret(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    path.chmod(mode)


class Launcher:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.ett = self.home / "code" / "event_timeline_tiled"
        self.runtime = tmp / "run"
        self.data = self.home / "k2460-data" / "session-1"
        self.store = self.data / "tiled"
        self.session = self.data / "session.toml"
        self.calls_file = tmp / "calls"
        self.values = {name: secrets.token_hex(16) for name in ("tiled", "admin", "viewer")}
        conf = self.home / ".config" / "k2460-dashboard"
        self.paths = {
            "tiled": self.store / "api-key",
            "admin": conf / "grafana-admin.password",
            "viewer": conf / "grafana-viewer.password",
        }

    def write_session(self, extra: str = "", tiled: str = "local = true") -> None:
        self.session.parent.mkdir(parents=True, exist_ok=True)
        self.session.write_text(f'data_dir = "~/k2460-data/session-1"\n{extra}\n[tiled]\n{tiled}\n')

    def write_server(self, pid: int, uri: str = "http://127.0.0.1:57705") -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / "server.json").write_text(json.dumps({"uri": uri, "pid": pid}) + "\n")

    def __call__(self, *args: str, env: dict[str, str] | None = None) -> tuple[Any, list[Any]]:
        base = {
            "HOME": str(self.home),
            "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.defpath}",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "DASHBOARD_TEST_CALLS": str(self.calls_file),
            "K2460_PYTHON": sys.executable,
        }
        self.calls_file.unlink(missing_ok=True)
        result = subprocess.run(
            [str(LAUNCHER), *args],
            env={**base, **(env or {})},
            cwd=self.tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        for value in self.values.values():
            assert value not in result.stdout + result.stderr
        lines = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        return result, [json.loads(line) for line in lines]


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Launcher]:
    run = Launcher(tmp_path)
    (run.ett / "deploy").mkdir(parents=True)
    (run.ett / "deploy" / "compose.host.yaml").touch()
    (run.ett / "deploy" / "grafana_viewer.py").touch()
    for fake in (run.home / ".local" / "bin" / "docker-compose", tmp_path / "bin" / "python3"):
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(RECORDER)
        fake.chmod(0o755)
    (run.runtime / "podman").mkdir(parents=True)
    # Bind by a relative name: macOS limits socket paths to 104 bytes.
    monkeypatch.chdir(run.runtime / "podman")
    podman = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    podman.bind("podman.sock")
    monkeypatch.chdir(tmp_path)
    for name, path in run.paths.items():
        _secret(path, run.values[name])
    run.write_session()
    run.write_server(os.getpid())  # a process that is running
    yield run
    podman.close()


def test_start_runs_its_own_project_against_the_sessions_tiled(launcher: Launcher) -> None:
    result, calls = launcher(str(launcher.session), env={"COMPOSE_PROFILES": "demo"})
    assert result.returncode == 0, result.stderr
    [call] = calls
    assert call["program"] == "docker-compose"
    assert call["args"] == [*FILES, "up", "--no-build", "--pull", "never"]
    assert call["cwd"] == str(launcher.ett.resolve())
    assert call["env"] == {
        "COMPOSE_PROJECT_NAME": "event-timeline-tiled-k2460",
        "COMPOSE_FILE": None,
        "COMPOSE_PROFILES": None,
        "DOCKER_HOST": f"unix://{launcher.runtime}/podman/podman.sock",
        "DOCKER_BUILDKIT": None,
        "ETT_TILED_URL": "http://127.0.0.1:57705",
        "ETT_TILED_KEY_FILE": str(launcher.paths["tiled"]),
        "ETT_QUEUE_KEY_FILE": "/dev/null",
        "ETT_QUEUES": "electrochemistry-k2460=zmq:tcp://127.0.0.1:60615",
        "ETT_CATALOG_ROOTS": "",
        "ETT_DEFAULT_PRESET": "electrochemistry",
        "ETT_ADAPTER_USER": "0:0",
        "ETT_HTTP_PORT": "8081",
        "ETT_GRAFANA_PORT": "3001",
        "ETT_ADAPTER_PORT": "8001",
        "ETT_PUBLIC_URL": "http://127.0.0.1:8081/",
        "GF_ADMIN_PASSWORD": launcher.values["admin"],
    }
    assert "http://127.0.0.1:8081/" in result.stdout


def test_the_queue_address_and_instrument_follow_the_session(launcher: Launcher) -> None:
    launcher.write_session('instrument = "k2460-bench"')
    env = {"QSERVER_ZMQ_CONTROL_ADDRESS": "tcp://127.0.0.1:60715"}
    result, [call] = launcher(str(launcher.session), "--compose", "ps", env=env)
    assert result.returncode == 0, result.stderr
    assert call["args"] == [*FILES, "ps"]
    assert call["env"]["ETT_QUEUES"] == "k2460-bench=zmq:tcp://127.0.0.1:60715"


def test_install_builds_without_a_session(launcher: Launcher) -> None:
    result, calls = launcher("--install")
    assert result.returncode == 0, result.stderr
    assert [c["args"][len(FILES) :] for c in calls] == [["build"], ["pull", "proxy"]]
    assert {c["env"]["COMPOSE_PROJECT_NAME"] for c in calls} == {"event-timeline-tiled-k2460"}
    assert calls[0]["env"]["DOCKER_BUILDKIT"] == "0"


def test_viewer_creates_the_k2460_login(launcher: Launcher) -> None:
    result, [call] = launcher("--viewer")
    assert result.returncode == 0, result.stderr
    assert call["program"] == "python3"
    assert call["args"] == [
        str(launcher.ett / "deploy" / "grafana_viewer.py"),
        "--url", "http://127.0.0.1:3001", "--login", "k2460",
        "--password-file", str(launcher.paths["viewer"]),
        "--admin-password-file", str(launcher.paths["admin"]),
    ]  # fmt: skip


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.write_server(_dead_pid()), "is not running"),
        (lambda r: (r.store / "server.json").unlink(), "no Tiled server recorded"),
        (lambda r: r.write_server(os.getpid(), "http://0.0.0.0:1"), "unexpected Tiled address"),
        (lambda r: r.write_session(tiled='uri = "https://t.test"'), "local Tiled"),
        (lambda r: r.paths["tiled"].chmod(0o644), "readable by group or others"),
        (lambda r: r.paths["admin"].unlink(), "missing, unreadable or empty"),
        (lambda r: (r.ett / "deploy" / ".env").touch(), "remove"),
    ],
)
def test_refuses_without_starting_anything(launcher: Launcher, change: Any, message: str) -> None:
    change(launcher)
    result, calls = launcher(str(launcher.session))
    assert result.returncode == 1 and calls == []
    assert message in result.stderr


def test_refuses_bad_arguments(launcher: Launcher) -> None:
    for args in ([], ["--start"], [str(launcher.tmp / "none.toml")], ["--install", "x"]):
        result, calls = launcher(*args)
        assert result.returncode == 1 and calls == [], args
    result, _ = launcher(str(launcher.session), "--compose", "ps", env={
        "QSERVER_ZMQ_CONTROL_ADDRESS": "tcp://h:1; rm"})  # fmt: skip
    assert "QSERVER_ZMQ_CONTROL_ADDRESS" in result.stderr
