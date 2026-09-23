# 2026-09-23 — The agent line on Windows: build, suites, smokes, bench, probes

Machine: AMD Ryzen Threadripper PRO 7955WX (16 cores), 256 GB DDR5-4800,
NVIDIA GeForce RTX 5090, Windows 11 Pro 26200. Rust 1.96 (msvc), MSVC
19.44 for `zstd-sys`, Python 3.13 (CPython, for the suites), Blender 5.2.0
LTS. Windows Defender real-time protection on (default). `main` at
`1d6e8ec` plus the commits below. Everything here is loopback, one machine,
`--release`.

## Build and unit suite

- `cargo build --release` in `agent/`: 45 s cold, clean. `zstd-sys`
  compiled with the Build Tools' `cl.exe` without anything on PATH beyond
  what rustup's msvc target already needs (the `cc` crate finds it through
  vswhere).
- `python -m pytest -q`: **100 passed, 2 skipped**. The two skips are
  `test_pathmap.py`'s `skipif(sys.platform != "darwin")` cases (tilde
  expansion against a mac `$HOME`; the wire-form wrappers on the current
  OS) — platform marks, not a missing binary. The transport tests spawned
  the release agent; `test_discover_by_direct_probe_finds_the_replica`
  passed, which is the one-box evidence for the beacon socket binding
  UDP/4246 and answering a unicast probe.
- `pid_alive` returned `None` on Windows, so the single-instance guard was
  inert there. Ported (`a4e2c6c`): `OpenProcess(SYNCHRONIZE)` +
  `WaitForSingleObject(h, 0)`, access-denied counting as alive the way
  EPERM does on unix — the same answer as the QCBridgeAE ring's liveness
  check, written the same day. `windows-sys` 0.61 was already in the lock.
  The unit test asserts a nonsense pid is dead on Windows now.
- Per-instance config: `--config <dir>/agent.toml` puts `agent.json` and
  `cert/` beside it and creates nothing under `%APPDATA%\QCBridge`; without
  `--config` the agent resolves to `%APPDATA%\QCBridge\agent.toml`
  (Roaming). Confirmed by running both.
- The tray starts on Windows (a replica with `tray = true`, 6 s, no crash,
  UDP/4246 bound, `[discovery] answering direct probes on udp/4246`). The
  Off / Direct / Discoverable click-through and the addon panel following
  it are a hand check, still owed.

## The smoke runners, ported

`smokes/run_smokes.py` (`a4e2c6c`): the six zsh runners in plain CPython,
same launches, waits and checks; `BLENDER`, `QCB_TRANSPORT`, `QCB_AGENT`
and the suite switches pass through. Three things it does that the zsh
runners left to the operator or to the OS: it unpacks the pyzmq wheel
matching Blender's Python into `<work>/pysite` for the zmq transport; it
stops a Blender with its process tree (`taskkill /T`) so the agent it
spawned dies too — an orphaned replica agent would keep UDP/4246; and the
mapping suite's `ln -s` becomes a directory junction when a symlink is not
permitted. Two Windows console gotchas cost a run each: the host's notes
carry a bullet and the bench report an arrow, and a cp1252 console turns
either into a crash. The runner sets UTF-8 on its own stdout and on the
pollers' (`PYTHONIOENCODING`).

All runs over the agent transport, `QCB_TRANSPORT=agent QCB_AGENT=spawn`,
each a fresh pair of GUI Blenders:

| suite | Windows | Mac |
| --- | --- | --- |
| 21-check (`smoke`) | **21/21** | 21/21 |
| `reconnect` | **6/6** — host notes `connected → replica lost → reconnected → connected`; replica b: bootstraps 2, gaps 1, want_resync cleared, local edit `Object:Probe` reported | 6/6 |
| `cache` | **5/5** — the replica reads the host's bake at frame 20 with no resync (one bootstrap each side) | 5/5 |
| `mapping` | **4/5** — see below | 5/5 |

**The mapping suite's fifth check on a Windows pair.** `relative_image_
mapped_to_replica_root` fails here, and cannot pass on one Windows box as
the suite is written — not because the code is wrong. The table has one
column per OS and the wire form *is* the `win` column. On the Mac pair,
host and replica each keep their own native root in the `mac` column under
one label (`hostside/proj` and `replicaside/proj`) and meet on `Z:\proj` in
the wire form; the relative image therefore lands under the replica's
root. On a Windows pair both sides *are* the win column, so there is
nothing to translate between: the smoke puts the two roots in the `mac`
column, `current_os_tag()` says `win`, and the image resolves to the
host's real path — which exists on the same machine and loads. The
tracker says this already ("a same-OS remap is not expressible by the
two-column table"). The Windows evidence for that check is the
two-machine run: a mac host and this replica, `/Volumes/...` on the wire.
The other four checks (absolute path untranslated and loads, stray image
left alone, unmapped counted and shown on the host's panel, replica clean)
pass, and the junction stood in for the symlink without incident.

Smokes killed nothing they should not have: no stray `blender.exe` or
`qcbridge-agent.exe` after any run.

## Blender behaviour the code relies on — re-probed here

`probes/caches/shared_dir_*.py`, headless, with `$S` and the Blender path
substituted (the scripts carry them as zsh-style literals; a Windows path
must go in with forward slashes, since a backslashed path pasted into a
`--python-expr` source is a unicode escape).

- **Finding 7 holds.** `shared_dir_poisoning -- bug`: the replica
  evaluates two frames into the empty shared directory, the host then
  bakes and ends with **2 files**, z at frame 20 = 2.994205 (poisoned).
  `-- fix`: replica keeps the cache in memory until frames exist, host
  bakes **24**, replica reads **1.540838** — the host's value exactly.
- **Finding 8 holds.** `shared_dir_append`: `seeks`, `toggle`,
  `same_path`, `alias_path` all keep 24 files and read 1.540838 at frame
  20 (alias too); `wipe` — re-setting the disk/external flags on an
  evaluated external cache and seeking — takes the directory 24 → 2 → 1 →
  0 and z to 2.99 / 3.0 / 1.93. Identical to the Mac's sequence.
- **`use_disk_cache` is ignored on an unsaved file.** Set to `True` on a
  new, unsaved file it reads back `False`; a bake with `use_external` and
  a path writes 0 files and reports "No valid data to read!". The host's
  refusal note is not a false alarm on Windows.

## Latency bench (`bench_latency`, loopback, 641,601-vertex Heavy)

Replica sampling tick 5.5 ms median / 9.1 ms p95 (Mac: 6.4 / 11–13). ms,
p50 / p90; the Mac column is `2026-09-23-sync-latency/agent-after-pipeline`;
the zmq column is this box, same day, the frozen transport as control.

| phase | Windows agent | Mac agent (after pipeline) | Windows zmq |
| --- | ---: | ---: | ---: |
| t1 | **107 / 115** | 105 / 120 | 100 / 108 |
| t2 | 145 / 156 | 137 / 150 | 144 / 154 |
| hot | 29 / 44 | 33 / 42 | 33 / 50 |
| sweep | 187 / 273 | 201 / 266 | 192 / 288 |
| hol0 | 176 / 194 | 125 / 144 | 170 / 174 |
| hol150 | 112 / 115 | 132 / 137 | 120 / 122 |
| heavy blob | **613 / 673** | 327 / 377 (max) | **369 / 389** |

Against the tracker's thresholds: tier-1 well under 150, sweep well under
300, the delta 150 ms behind the blob (hol150) *below* tier-1 — the fast
lane and the merge rule do their job here. Two rows differ:

- **The heavy blob is ~1.9× slower over the agent (613 vs 327 ms) — and
  the zmq control run on the same box does it in 369.** Both transports
  write the same `libraries.write` partial to `%TEMP%` and load it the same
  way on the replica, so the extra ~250 ms is in what differs: the agent
  path ships the partial *uncompressed* over the local TCP link twice
  (host → agent, agent → replica's Blender, ~61 MB each way) and compresses
  on the agent's threads, while zmq compresses in Blender and sends ~a
  tenth of the bytes. On the Mac the same comparison costs the agent 75 ms
  (327 vs 252); here 245. That is the tracker's `recv_into` / socket-buffer
  question answered with a number rather than a stall: nothing stalls (16
  of 16 crossed, 0 lost), but Windows' loopback TCP moves 122 MB
  noticeably slower than macOS's, and the bigger partial is scanned by
  Defender on write as well. Next measurement, when someone has elevation:
  the same run with `%TEMP%` excluded, and the agent's own log timestamps
  for receive-vs-compress-vs-send on the heavy blob.
- **hol0 is higher on both transports** (176 agent, 170 zmq, vs 125 Mac):
  a Probe edit in the *same* flush as the Heavy toggle waits for the host's
  write of Heavy before its own delta leaves, so it inherits the write
  cost, which is a Blender-on-Windows number, not a transport one. hol150
  (112 / 120), where the edit arrives after the write, is *under* the
  Mac's, so the fast lane and the merge rule work.

zmq control: `zmq/latency.json`; agent: `agent/latency.json`, both with
the printed tables beside them. (The first zmq run was cut short by
someone closing the Blender windows; the numbers above are the clean
re-run.)



## Coverage survey (`coverage/run_coverage`, 125 rows, agent transport)

| | Windows | Mac (`full-agent-final.json`) |
| --- | ---: | ---: |
| crossed | **120** | 120 |
| NOT crossed | 2 — `image_new_generated`, `image_pixels_edit` | 2 — the same two |
| piggybacked | 2 — `viewlayer_add`, `compositor_node_add` | 2 — the same two |
| host-error | 1 — `undo_after_move` (excluded from the survey; its own run below) | 1 — same |
| median edit→visible | **249 ms** (p90 364) | 249 ms |
| replica | gaps 0, apply errors 0, unmapped 0 | same |

Row for row the same survey as the Mac's, to the median millisecond.
`coverage/full-agent.json` and the printed table sit beside this file.

One number differs and is flagged for the Mac side: **the replica counted
8 bootstraps here against 1 on the Mac.** The host log shows why: seven
times a Scene-level action (the scene camera switch, view layer add,
compositor node add, the rigid-body world, keyframes on `SceneAction`)
escalated `Scene` to tier 2, which is unsupported as a blob, so the host
sent a fresh bootstrap (`qcb t2 UNSUPPORTED Scene:Scene → auto bootstrap`).
The rows crossed either way — the bootstrap carries them — but each one
is a full-file resend where the Mac apparently sent none. Whether the
Mac's host log shows the same lines and its counter simply did not count
them, or whether Windows Blender reports Scene changes the Mac's does not,
is a question for whoever has the Mac's `host.log`. Cost, not
correctness.

The first full run on this box died at step 102 and reported 19 rows
inconclusive: the survey host's own `host.json` rewrite (`os.replace` of a
temp file) hit `PermissionError` on Windows because the poller had the
file open — CPython's `open()` shares read and write but not delete, and a
rename over such a file is refused. `smokes/_atomic.py` retries the
replace for up to a second; every half uses it (`3a38b52`). The run above
is with the fix. **The same class of thing can bite the agent's phonebook
on a share** (`<dir>/qcbridge/<name>.json` by temp-file-then-rename): a
reader that holds the file open at the wrong instant makes the writer's
rename fail on Windows, where on macOS it always succeeds. The agent's
read is open-read-close, so the window is tiny; worth a retry on the
writer side regardless. Noted in the tracker.

*Undo (`QCB_COV_UNDO=1 QCB_COV_ONLY=obj_color,mesh_vertex_move,undo_after_move`):*
**the host Blender crashes.** At the `undo_after_move` step — `bpy.ops.ed.undo()`
called from the survey's timer, after `mesh.primitive_cube_add` and
`object.armature_add` — Blender 5.2.0 dies with an access violation in
`deg_update_eval_copy_datablock` (via `scene_graph_update_tagged` from
`bpy_op_fn_call` inside `py_timer_execute`; `blender.crash.txt`). The Mac's
GUI Blender survives the same call and merely rewinds the session
(`COVERAGE.md`). Twice on this box, the second time with nothing else
running. So the undo *cost* measurement — 6 blobs and 29 skipped for one
Ctrl-Z on the Mac — cannot be taken by the survey here; it is a hand
check: Ctrl-Z in a `QCB_DEBUG=1` host after a vertex move and count the
`t2 skip … (unchanged blob)` lines. The gate itself is proven by the full
survey above: **27 of 102 tier-2 sends were skipped as unchanged blobs**,
so `libraries.write` is deterministic enough on Windows for the digest to
match. A user pressing Ctrl-Z is not a timer calling `ed.undo()`, so this
is a limit of the instrument, not (yet) a product finding — but it is a
Blender-on-Windows behaviour that differs from the Mac, and the Mac side
should know before relying on operator calls from timers anywhere else.

## Real Windows work, done as scripts (`c1f1e56`)

- `agent/windows/logon-task.ps1` — a Scheduled Task at the user's log on,
  Interactive, Limited, no time limit, `--role` on the command line;
  `-Remove` unregisters. Verified: registered, read back (logon trigger,
  Interactive, Limited, `PT0S`), removed.
- `agent/windows/firewall-rule.ps1` — inbound UDP/4246 (and optionally the
  QUIC control port), unscoped for MinRender's reasons, `-Remove` to
  delete. Needs elevation; parsed, not run here.

## Two-machine items — not attempted on one box

Cross-OS path mapping (a mac host's absolute paths on this replica),
discovery over the real LAN and by direct IP over the VPN, the shared
cache root on the SMB share (UNC and mapped drive), linked libraries by
mapped path. Everything above is loopback. Pair with the Mac.

## The decision that is not this session's

`qcbridge/blender_manifest` still says 0.1.6; the agent line is on `main`.
Which version the release carries, and whether the agent is signed, are the
Mac owner's calls (WINDOWS-SESSION.md). Nothing here bumps or signs.
