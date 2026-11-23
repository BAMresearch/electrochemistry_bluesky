# Sessions: set-up and limits

A session file describes one experimental environment: which instrument, where
the data go, and the operational limits every run must stay within. The
QueueServer loads one session for as long as it runs; every queued row is
validated against it.

## Loading a session is the confirmation

You confirm the set-up and limits **once, for the session**, not for each run.
Starting the RE Manager with a session file and opening its environment is that
confirmation. Before you do, check that the file matches what is connected:

- the cell (name it in the file's header comment), the wiring (front or rear
  terminals, four-wire) and the polarity;
- the voltage window and the current limit;
- the data folder.

Within the session, any row that passes validation runs without further
questions. For a different cell, wiring or window, use a different session file
and restart the RE Manager with it.

## The file

Start from the example and review every value:

```sh
cp qserver/session-k2460.example.toml ~/sessions/cell01.toml
```

```toml
# cell01: working electrode on HI (force + sense), counter on LO,
# front terminals, four-wire. Pulses, window -3..3 V.
backend = "k2460"
data_dir = "~/k2460-data/cell01"
resource = "TCPIP0::<instrument-ip>::5025::SOCKET"
serial = "<instrument-serial>" # refuses any other instrument
terminals = "front"
poll_period_s = 1.0            # how often the laptop reads new data
# start_timeout_s = 600.0      # external START: abort if none arrives in time
# facility = "<facility>"      # optional set-up names in every run (below)
# station = "glovebox-1"       # default "k2460-<serial>"

[limits]
v_min_abs = -3.0               # absolute voltage window, V
v_max_abs = 3.0
i_max_abs = 0.03               # A; the largest current of any step
max_duration_s = 172800.0      # per run
max_records = 1000000          # per run (the 2460 buffer holds 1,000,000)

[sample]                       # optional; only written to the run files
cell = "cell01"
electrode_mass_mg = 1.5
capacity_mah = 0.6
electrolyte = "<electrolyte>"
```

| Setting | Meaning |
|---|---|
| `backend` | `k2460`, or `simulator` for the synthetic cell |
| `data_dir` | Where run files go. Use a local disk: live reading needs it |
| `resource`, `serial` | The instrument's address and serial number; both required for `k2460`, and a run refuses any instrument with another serial |
| `terminals` | `front` or `rear` |
| `start_timeout_s` | Optional. An external-start run aborts if no START comes in time |
| `facility`, `instrument`, `station` | Optional names of this set-up, in every run's start document (below) |
| `[limits]` | The window, maximum current, and per-run duration and record bounds |
| `[sample]` | Optional description of the cell (below) |
| `[directions]` | Optional words for charge and discharge in the run sheet (below) |
| `[tiled]` | Optional Tiled copy of every run's documents (below) |

### Set-up names

Every run's start document says which set-up made it, so catalogs and
timelines (for example a Grafana timeline of a Tiled store) can group runs by
set-up and show them with the right plots:

| Key | Default | Meaning |
|---|---|---|
| `instrument` | `"electrochemistry-k2460"` | The kind of set-up; keep the default unless a viewer is configured for another name |
| `station` | `"k2460-<serial>"`, or `"simulator"` | Which set-up of that kind; name it (e.g. `"glovebox-1"`) when there are several |
| `facility` | none (left out) | Your lab or facility, e.g. `"BAM"` |

Plain text; keep the same values for every session of one set-up. The run
files keep them with the session's resolved settings.

### The sample table

`[sample]` describes the cell for the run files (`/entry/sample`); nothing in a
run reads it. Values are text, numbers or `true`/`false`, one level deep (no
sub-tables, lists or dates). Keys are lower case; a key ending in a unit gets
that unit in the file: `_mg`, `_g`, `_mah`, `_uah`, `_cm2`, `_mm`, `_um`, `_ul`,
`_v`, `_mv`, `_a`, `_ma`, `_ua`, `_s`, `_h`, `_degc`, `_pct`. `name` and
`session_description` are taken (the run sheet's sample ID and the file's header
comment). The start script and `oe-enqueue` print it. Like the rest of the
file, it is part of what you confirm by loading the session: keep it matching
the cell that is connected. The whole file, header comment included, is stored
in every run file.

### Direction words

The run sheet's `start_with` and `cv_after` columns take `charge` (positive
current, the voltage rises) or `discharge`. To write your own terms there
instead, define them for the session:

```toml
[directions]          # this cell, working electrode on HI
sodiation = "discharge"
desodiation = "charge"
```

Each word means `charge` or `discharge`. Words are matched exactly (a sheet
cell `Sodiation` is refused), and `charge` and `discharge` cannot be redefined.
A row that uses a word the session does not define is refused.

The table belongs to the session, not the run sheet, because what a word means
depends on the cell and its wiring: lithiating the electrode on HI lowers the
voltage in a half cell against lithium, but lithiating the anode of a full cell
charges it, and swapped leads reverse both. Check the table together with the
rest of the session before loading it. `oe-validate` and `oe-enqueue` print what
each word in a row was taken to mean (`start_with: sodiation = discharge`). The
run file keeps the row as written and the session text with the table.

The words set which half of a cycle comes first, the polarity of the first
pulse in each `bipolar_pulse` block, and after which half a CV hold is placed
(the hold itself ends on its taper current or time limit). The sign of
`current_a` in `pulse_train` and `soc_position` rows is always explicit.

`oe-enqueue` records a checksum of each row's resolved sequence. If the RE
Manager's session reads the row differently (for example because its
`[directions]` table differs from the session given to `oe-enqueue`), the run
is refused before anything is prepared; enqueue it again with the right
session.

### Tiled store

The run files are the record of each run. A session can also keep every run's
Bluesky documents (metadata, readings and events) in a
[Tiled](https://blueskyproject.io/tiled/) store, which makes them searchable
later. Install the `tiled` extra (`uv sync --locked --extra tiled …`), then:

```toml
[tiled]
local = true          # a store in data_dir/tiled, beside the run files
# flush_s = 10.0      # write waiting rows at least this often, in seconds
```

Opening the environment starts a Tiled server for the session (listening on
this computer only), and closing it stops the server; the store persists in
`data_dir/tiled` (catalog, tables and a log). While it runs,
`data_dir/tiled/server.json` and `api-key` say how to connect. Later,
`oe-tiled DATA_DIR` serves the store again for reading. One session per data
folder: a second server on the same store is refused, and the message names
the running server's process. The server stops with the process that started it,
also when that process was killed; if a refusal persists, stop that process.
The key never appears in `server.log` (mode 0600).

Rows waiting to be written go to the store at least every `flush_s` seconds
(default 10), also in a slow run, so a live reader of the store (a notebook, a
timeline that flags runs without new data) sees an open run's progress: with a
responsive server, within about `flush_s` plus the poll period. Faster runs are
written in batches of 1,000 rows. A slow server delays rows (by its queue and
up to 45 s of retries per failed write), never the run.

Readers can read while a run is written: the store keeps its tables in SQLite
(`tables.sqlite`) with a write-ahead log. Keep `data_dir` on a local disk: the
log does not work on network shares (if SQLite refuses it, the session does not
open). To copy or
back up a store, stop its server first (close the session or `oe-tiled`), then
copy the whole `tiled` folder, including `tables.sqlite-wal` and `-shm` if
present. A store made by an earlier version also has `tables.duckdb`; its runs
stay readable, but parallel reads of those older runs can fail (DuckDB in
Tiled 0.2.21 answers one request at a time).

The local store has one API key, and it can write. Keep it to this computer's
own notebooks: do not hand it to dashboards or other shared readers. For those,
use a Tiled server with scoped (read-only) keys, with `uri` below; the session
then writes there, and readers get their own keys from that server.

To use an existing Tiled server instead, give its address and the name of the
environment variable that holds the API key (never the key itself: the session
file is stored in every run file):

```toml
[tiled]
uri = "https://tiled.example.org"
api_key_env = "TILED_API_KEY"
# timeout_s = 10.0
# flush_s = 10.0
```

If the store cannot be opened, the environment does not open (remove the
`[tiled]` table to measure without it). During a run, Tiled never delays,
stops or fails it: documents are written by a background thread, and what
cannot be written goes to `data_dir/tiled/backup` (JSON lines, for later
replay). When the copy can be incomplete: [Data, Tiled store](data.md#tiled-store).

## What the limits do

- **Before a run:** every row is checked against them (`oe-validate`,
  `oe-enqueue`, and again when the run starts). A row that asks for more current,
  a threshold outside the window or a longer run is refused.
- **During a run:** in current and rest steps the 2460 compares every voltage
  reading with the window and stops the run with the output OFF on the first
  reading outside it, independently of the row's own thresholds. Its voltage
  compliance is set 0.1 V outside the window, so the cutoff acts before
  compliance does.
- In a voltage hold (CV) the 2460 measures current, not voltage. The held
  voltage is checked against the window before the run; during the hold the
  current is limited to the row's CC current `|current_a|` (compliance; at
  least 11 % of the measure range, below `i_max_abs`) and the hold ends on its
  taper current (`cv_i_abs_min_a`) or its time limit. A CC step that ended on
  its time limit short of the hold voltage therefore continues at its own
  current until the voltage is reached, instead of jumping to it.
- Between readings the instrument does not watch the voltage; only the
  compliance limit applies there.
- The cell's rest voltage must be inside the window before a run.

## One session per window

Use a separate session for each voltage window and current range, for example:

- 0–3 V for a slow first discharge from the open-circuit voltage;
- −3 to 3 V and 30 mA for high-rate pulses whose loaded voltage goes below
  0 V;
- 0–0.5 V and 100 µA for slow cycling with a CV hold (the hold's current
  limit is `i_max_abs`).

A narrow window protects better: reversed leads or a wrong row stop at the first
reading outside it.

## Output-off mode

The 2460 switches on with output-off mode NORMAL, which pulls a connected cell
towards 0 V. Opening the environment sets **HIGH-Z** for both source functions
and reads it back; every run sets and checks it again. Connect a cell only after
the environment is open (see [At the instrument](operation.md)).

## Simulator sessions

`qserver/session-simulator.toml` has `backend = "simulator"` and `sim_speed`
(10 = ten times real time). Use it to try sheets and procedures; its data are
synthetic.
