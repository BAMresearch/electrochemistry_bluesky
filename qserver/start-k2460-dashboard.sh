#!/bin/bash
# Event-timeline dashboard (event-timeline-tiled) for one K2460 session whose
# runs go to the session's local Tiled ([tiled] local = true). Under rootless
# Podman, on the host network, listening on 127.0.0.1 only, as its own compose
# project on its own ports (http://127.0.0.1:8081/), so it runs beside another
# event-timeline-tiled stack on the same computer.
#
#   qserver/start-k2460-dashboard.sh SESSION.toml
#       start the stack in the foreground (Ctrl-C stops it)
#   qserver/start-k2460-dashboard.sh SESSION.toml --compose logs adapter
#       anything else, with this stack's compose files and settings
#   qserver/start-k2460-dashboard.sh --install
#       once per event-timeline-tiled release, with network access: build the
#       images and pull the proxy image, then exit
#   qserver/start-k2460-dashboard.sh --viewer
#       once, after the first start (idempotent): the read-only Grafana login "k2460"
#
# The session's Tiled runs only while the worker environment is open, and on a
# new port with a new key each time: start, or restart, the dashboard after
# `qserver environment open`. It shows this session's runs only. The RE
# Manager's control address comes from QSERVER_ZMQ_CONTROL_ADDRESS, as for
# `qserver` (default tcp://127.0.0.1:60615). See docs/user/operation.md,
# "Dashboard".
set -euo pipefail

refuse() {
    printf '%s\n' "dashboard refused: $1" >&2
    exit 1
}

repo=$(cd "$(dirname "$0")/.." && pwd)
python=${K2460_PYTHON:-$repo/.venv/bin/python}
ett_dir=${ETT_DIR:-$HOME/code/event_timeline_tiled}
compose=$HOME/.local/bin/docker-compose
conf=$HOME/.config/k2460-dashboard
admin_password=$conf/grafana-admin.password
viewer_password=$conf/grafana-viewer.password
socket=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock
control=${QSERVER_ZMQ_CONTROL_ADDRESS:-tcp://127.0.0.1:60615}

# GNU stat (Linux), then BSD stat (macOS).
file_stat() {
    stat -c "$1" "$3" 2>/dev/null || stat -f "$2" "$3" 2>/dev/null
}

# A secret is a non-empty regular file, owned by this user, mode 600 or stricter.
check_secret() {
    local file=$1 mode owner
    [ -f "$file" ] && [ -r "$file" ] && [ -s "$file" ] || refuse "missing, unreadable or empty: $file"
    owner=$(file_stat '%u' '%u' "$file") || refuse "cannot check the owner of $file"
    [ "$owner" = "$(id -u)" ] || refuse "not owned by $(id -un): $file"
    mode=$(file_stat '%a' '%Lp' "$file") || refuse "cannot check the permissions of $file"
    (( (8#$mode & 077) == 0 )) || refuse "readable by group or others: $file (chmod 600)"
}

session=
case "${1:-}" in
    --install) mode=install; shift ;;
    --viewer) mode=viewer; shift ;;
    ''|-*) refuse "usage: $0 SESSION.toml [--compose ARGS...] | --install | --viewer" ;;
    *)
        [ -f "$1" ] || refuse "no session file: $1"
        session=$1; shift
        mode=start
        case "${1:-}" in
            '') ;;
            --compose) mode=compose; shift ;;
            *) refuse "unknown option after the session: $1" ;;
        esac
        ;;
esac
[ "$mode" = compose ] || [ $# -eq 0 ] || refuse "unexpected arguments: $*"

check_secret "$admin_password"
if [ "$mode" = viewer ]; then
    check_secret "$viewer_password"
    [ -f "$ett_dir/deploy/grafana_viewer.py" ] || refuse "no event-timeline-tiled checkout at $ett_dir"
    exec python3 "$ett_dir/deploy/grafana_viewer.py" --url "http://127.0.0.1:3001" --login k2460 \
        --password-file "$viewer_password" --admin-password-file "$admin_password"
fi
[ -x "$compose" ] || refuse "no docker-compose at $compose"
[ -S "$socket" ] || refuse "Podman's socket is not running: systemctl --user enable --now podman.socket"
[ -f "$ett_dir/deploy/compose.host.yaml" ] || refuse "no event-timeline-tiled checkout at $ett_dir"
# Every setting comes from here; a .env would add its own, such as a demo profile.
[ ! -e "$ett_dir/deploy/.env" ] || refuse "remove $ett_dir/deploy/.env; this script sets everything"

if [ -n "$session" ]; then
    [ -x "$python" ] || refuse "no Python at $python (uv sync in $repo, or set K2460_PYTHON)"
    # The session's store folder and instrument; the running server's address and PID.
    found=$("$python" - "$session" "$repo" <<'PY'
import json, re, sys, tomllib
from pathlib import Path

def stop(message):
    print("ERROR " + message)
    sys.exit()

try:
    with open(sys.argv[1], "rb") as stream:
        session = tomllib.load(stream)
except (OSError, tomllib.TOMLDecodeError) as exc:
    stop(f"cannot read the session: {exc}")
if session.get("tiled", {}).get("local") is not True:
    stop("the session does not keep a local Tiled ([tiled] local = true)")
if "data_dir" not in session:
    stop("the session has no data_dir")
store = Path(str(session["data_dir"])).expanduser()
store = (store if store.is_absolute() else Path(sys.argv[2]) / store) / "tiled"
instrument = session.get("instrument", "electrochemistry-k2460")
if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(instrument)):
    stop(f"instrument {instrument!r} is not a plain name")
try:
    server = json.loads((store / "server.json").read_text())
    uri, pid = server["uri"], int(server["pid"])
except (OSError, ValueError, KeyError, TypeError):
    stop(f"no Tiled server recorded in {store} (qserver environment open first)")
if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", str(uri)):
    stop(f"unexpected Tiled address {uri!r} in {store}/server.json")
print(store, instrument, uri, pid, sep="\n")
PY
    ) || refuse "cannot read $session with $python"
    case "$found" in ERROR\ *) refuse "${found#ERROR }" ;; esac
    { read -r store; read -r instrument; read -r tiled_url; read -r tiled_pid; } <<<"$found"
    kill -0 "$tiled_pid" 2>/dev/null \
        || refuse "the session's Tiled (pid $tiled_pid) is not running: qserver environment open first"
    tiled_key=$store/api-key
    check_secret "$tiled_key"
    [[ $control =~ ^tcp://[A-Za-z0-9.-]+:[0-9]+$ ]] || refuse "QSERVER_ZMQ_CONTROL_ADDRESS is not tcp://host:port"
else
    # Building needs no Tiled; compose still checks that these are set.
    tiled_url=http://127.0.0.1:9 tiled_key=/dev/null instrument=electrochemistry-k2460
fi

unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
# Its own project: never the containers of another event-timeline-tiled stack.
export COMPOSE_PROJECT_NAME=event-timeline-tiled-k2460
export DOCKER_HOST="unix://$socket"
export ETT_TILED_URL=$tiled_url
# The session server's only key (single-user Tiled has no read-only keys).
export ETT_TILED_KEY_FILE=$tiled_key
export ETT_QUEUE_KEY_FILE=/dev/null
export ETT_QUEUES="$instrument=zmq:$control"
export ETT_CATALOG_ROOTS=
export ETT_DEFAULT_PRESET=electrochemistry
# Under rootless Podman, container root is this user, so the adapter reads the mode-600 key.
export ETT_ADAPTER_USER=0:0
export ETT_HTTP_PORT=8081 ETT_GRAFANA_PORT=3001 ETT_ADAPTER_PORT=8001
export ETT_PUBLIC_URL=http://127.0.0.1:8081/
# Grafana uses this only when it first creates its database.
GF_ADMIN_PASSWORD=$(<"$admin_password")
export GF_ADMIN_PASSWORD

cd "$ett_dir"
files=(-f deploy/compose.yaml -f deploy/compose.host.yaml)
case "$mode" in
    start)
        echo "Dashboard for $instrument at http://127.0.0.1:8081/, Tiled $tiled_url (pid $tiled_pid)"
        exec "$compose" "${files[@]}" up --no-build --pull never
        ;;
    install)
        export DOCKER_BUILDKIT=0
        "$compose" "${files[@]}" build
        exec "$compose" "${files[@]}" pull proxy
        ;;
    compose)
        exec "$compose" "${files[@]}" "$@"
        ;;
esac
