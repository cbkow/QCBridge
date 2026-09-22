# QCBridge architecture

How the addon is put together, and — more usefully — *why* the awkward parts
are awkward. Much of this code is shaped by Blender behaviour that took a
while to pin down; those findings are recorded here and in the comments they
point at, so nobody re-derives them.

This describes `main`: the ZMQ transport, ffmpeg capture, no sidecar agent.
The Rust agent and the QUIC transport live on `spike/parity` — see
[Where the other half lives](#where-the-other-half-lives).

Line numbers are a reading aid, not a contract. Version-specific findings say
which Blender they were probed on.

---

## The ring model

Declared in `qcbridge/__init__.py:1-10`, enforced by `tests/test_ring_separation.py`.

- **ring0/** — the `bpy` adapter. Handlers, applies, capture supervision, UI
  glue. The only place Blender is imported.
- **ring1/** — sync logic. **No module here may import `bpy`** (nor `bmesh`,
  `mathutils`, `gpu`, `blf`, `bgl`, `aud`), and only `transport_zmq.py` may
  import `zmq`. Everything in ring1 imports and runs under plain CPython,
  which is why it can be unit-tested without Blender.
- **ring 2** is not a package: native heavy lifting lives in subprocesses
  (ffmpeg), not in this codebase.

Three tests enforce it: no Blender-only imports in ring1, zmq quarantined to
one module, and every ring1 module import-clean outside Blender.

## Module map

### ring0 — touches bpy

| File | Lines | Responsibility |
|---|---|---|
| `host_handlers.py` | 831 | The big one. Depsgraph handler, `HostSync`, snapshot builders, digests, flush timer, deletion and visibility sweep. |
| `replica_apply.py` | 626 | Replica tick, hot apply, cold processing, shot mode, self-healing camera view. |
| `session.py` | 498 | Lifecycle for both roles, handshake, transport construction, status text, QCView deep link. |
| `tier2_io.py` | 209 | Serialize/apply one datablock via `libraries.write` / append + `user_remap`. |
| `kiosk.py` | 200 | Kiosk state machine, serviced by the replica tick. |
| `bootstrap.py` | 137 | Tier 3: whole-file save, replica open + path localization. |
| `pixel_path.py` | 123 | ffmpeg capture/encode command build, spawn, supervision. |
| `overlay.py` | 69 | POST_PIXEL burn-in status overlay. |
| `host_hot.py` | 63 | 30 Hz view/frame sampler. |
| `identity.py` | 56 | UUID stamping as the `qcbridge_uuid` id property. |

### ring1 — pure Python

| File | Lines | Responsibility |
|---|---|---|
| `transport_zmq.py` | 334 | The pyzmq implementation. |
| `protocol.py` | 230 | Wire protocol: hot pack/unpack, control JSON, hello, cold chunking, reassembly, seq tracking. |
| `pathmap.py` | 154 | Windows ↔ macOS path translation, longest-prefix-first. |
| `shadow.py` | 143 | Tracked property tables + `ShadowStore.diff_and_update()`. |
| `dirtyset.py` | 93 | Last-value register at datablock granularity, one-way tier ratchet. |
| `toolbox.py` | 86 | ffmpeg resolution: prefs → QCView `toolbox.json` → PATH, each rung spawn-verified. |
| `transport.py` | 83 | The quarantine boundary. See below. |
| `registry.py` | 61 | uuid ↔ Blender `session_uid`. |
| `classifier.py` | 55 | Debouncer (0.15 s) + update classification. |

`prefs.py` (575) holds preferences, the path-mapping UIList, operators, the
N-panel and settings persistence. `qcbridge/__init__.py` delegates
`register()` to `prefs` only — **enabling the addon is inert**; a session only
ever starts from an explicit operator.

## Roles

**Role is a preference, never OS detection.** `QCBridgePreferences.role` is
`HOST` or `REPLICA`, default `HOST`; `session.start()` branches on it.

**Host** connects outward to `replica_address`, runs the 30 Hz hot sampler and
the depsgraph handlers plus a 50 ms flush timer, and is the **single writer**.
Sync is strictly one-way.

**Replica** binds `bind_address`, installs an IO-thread request handler, runs
a 15 ms apply tick, enables the burn-in overlay, enters kiosk, and starts the
pixel path. It never writes back and never touches the project tree — it works
from a temp copy, and forces `cycles.device = "GPU"` after each bootstrap.

The replica *keeps listening* after a session ends, so the next one bootstraps
it back with nobody touching that machine.

## What crosses, and on which lane

### Hot — camera view and frame, 30 Hz, ~85 bytes

`struct "<4sBi3f16f3f"`, magic `QCB2`, `PROTOCOL_VERSION = 2`: frame, 4×4 view
matrix, lens, clip start/end, flags (perspective, camera-view), camera zoom
and 2-float offset. Sampled from the **largest 3D viewport** on the host and
applied to the largest on the replica. Last-value throughout — never queued.
`scene.frame_set()` is rate-limited to 0.1 s.

### Cold — datablock state, sequence-numbered

Three tiers, escalating:

- **Tier 1 — property deltas.** A tracked-path table per datablock type
  (`shadow.py`) plus dynamic paths: node-socket defaults and link signatures,
  shape-key values, object custom properties, and `@`-prefixed non-RNA setters
  (`@hide`, `@scene_camera`, `@lc_exclude`, `@lc_hide`) for state that has no
  RNA path.
- **Tier 2 — whole datablock**, `libraries.write(compress=True)`, chunked at
  4 MiB. Scenes are deliberately unsupported and surface as "resync
  recommended".
- **Tier 3 — bootstrap**, the whole mainfile. Also the manual **Force Resync**.

`~`-prefixed *structure signatures* (`~anim`, `~modifiers`, `~parent`,
`~constraints`, `~pcache`, `~pose`, `~members`) are never sent — a change in
one escalates that datablock to tier 2.

Gaps are detected and **never replayed**: recovery is a manual Force Resync.
Backpressure requeues the datablock in the dirty set and rolls `seq` back —
the dirty set, not the socket, is where backlog belongs.

### Control — JSON request/reply

`hello`/`hello_reply` (token compared with `hmac.compare_digest`, protocol
version gate, addon version informational only), `goodbye`, `shot`, `zoom`,
and ping/pong heartbeats that carry a status dict the host uses to recommend a
resync.

### Video — out of band

Pixels do not ride the sync transport. The replica's ffmpeg sends SRT (mpegts,
HEVC 10-bit) straight to the viewer. The SRT passphrase is derived on both
ends from the session token and never crosses the wire.

### Not synchronised

Selection, active object, tool and modal state, UI/workspace state, render
output. Nothing flows replica → host except the pong status dict.

## The transport boundary

`ring1/transport.py` is the decision-#14 quarantine boundary: two
`typing.Protocol` classes and a frozen config dataclass, no implementation.
Its **threading contract** is load-bearing and stated in the module docstring:

> **Host** — `send_hot()`/`send_cold()` are called from ONE thread only
> (Blender's main thread); control requests and heartbeats run on the
> transport's internal IO thread.
> **Replica** — all sockets live on the IO thread; callers read completed
> state via `poll_hot()`/`poll_cold()`, which touch only thread-safe buffers.

`send_cold()` returning `False` means "not deliverable now" — the caller keeps
the datablock dirty and retries. `on_peer_state` fires from the IO thread and
**must not touch bpy**.

The blocking `request()` is for scripts and tests only; the session uses
`request_nowait()` / `poll_reply()` / `cancel_request()`, which are *not* in
the Protocol but are depended on — see [Known gaps](#known-gaps).

## Concurrency model

**There are no modal operators anywhere in the addon.** Everything is
`bpy.app.timers` plus `bpy.app.handlers`, and that is deliberate.

- `depsgraph_update_post` and `load_post`, both `@persistent`.
- Timers, all `persistent=True`: host flush 50 ms, host hot 1/30 s, handshake
  poll 0.25 s, replica apply 15 ms, prefs auto-restore once.
- `persistent=True` on the replica tick is load-bearing: the apply loop must
  survive the bootstrap's `open_mainfile`, and non-persistent timers are
  dropped on file load.
- Both long-lived timers wrap their body so **one exception costs one tick,
  not the session**. An unguarded raise unregisters the timer, and sync dies
  silently while the peer still looks connected.
- The handshake is a fire-and-poll timer, not a blocking request: a blocking
  3 s request froze Blender's UI whenever the replica was unreachable.
- Re-entrancy guard in the depsgraph handler, because uuid stamping dirties
  datablocks and re-triggers it.
- Nothing marks dirty until the first bootstrap is serialized, or the
  file-open flood replays as a tier-2 storm.

## Blender findings worth not re-deriving

All probed on 5.2 unless noted.

- **`is_updated_geometry` is useless as a structural signal** — it fires on a
  light-energy change and even a scene-exposure change. Deliberately ignored
  for recognized types (`classifier.py:46-52`).
- **Visibility changes produce no reliable event.** The eye toggle is
  view-layer state, and `hide_viewport` fires nothing for the object it
  disables. Handled by a 0.5 s polling sweep (`host_handlers.py:104-116`).
- **Renames fire no depsgraph event** — sweep-sampled, because name-keyed
  setters die on stale names (`host_handlers.py:617`).
- **Keyframe edits fire on the Action ID, not the object.** Without listing
  Action as syncable, a retimed rig animation never crosses.
- **Geometry Nodes modifier inputs are neither idprops nor RNA** — they live
  in `mod.properties.inputs[<socket identifier>]` as IDPropertyGroups.
  Modifiers don't support classic IDProperties at all on 5.2.
- **`libraries.load` has no `shape_keys` namespace.** Keys can't travel alone;
  they ride their owner's blob and are paired by a post-pass, or every resend
  of a keyed mesh leaks a duplicate Key datablock.
- **Kiosk owns no timers.** Tier-3 bootstrap calls `open_mainfile` from inside
  a timer callback; registering timers around that corrupted Blender's timer
  list — a C crash in `BLI_timer_execute`.
- **`screen.screen_full_area` silently no-ops when invoked from a timer**; the
  exit path uses `screen.back_to_previous()`.
- **A view3d RNA write restarts Cycles viewport sampling even when the value
  is identical** — hence compare-before-write everywhere in the apply path.
- **A converged viewport stops redrawing**, freezing the burned-in overlay; a
  10 Hz `tag_redraw()` poke fixes it without resetting sampling.
- **POST_PIXEL drawing survives `show_overlays=False`** (verified 2026-07-24),
  which is what lets status burn into the captured stream.
- **`relative_remap=False` is load-bearing** on the bootstrap save: the
  default rewrites `//` paths relative to the temp location.
- `bpy_prop_collection.get()` has raised `SystemError` on freshly-appended
  state; plain iteration is used instead. Blender also mutates the list
  assigned to `data_to` in place, so the code keeps `str()` copies.

## Caches, sims and geometry nodes

There is deliberate handling here, and it is honest about its limits.

`_pcache_signature` records, per sim on an object (cloth, soft body, particle
systems, dynamic paint surfaces): `[tag, is_baked, use_disk_cache,
frame_start, frame_end]`. The cache **info string is deliberately excluded** —
it changes on every frame of plain playback caching and would turn scrubbing
into a resend storm. `point_cache` is also skipped in the modifier digest.

Bake state is sampled on the 0.5 s sweep, because a bake finishing on a job
thread or a Delete Bake gives no dependable event for the owning object.

**The limitation is narrower than it looks.** A tier-2 resend *does* carry the
cache frames — re-probed on 5.2 on 2026-09-18, correcting an earlier belief
that partial blends dropped them. What it loses is the `is_baked` flag, so the
replica ends up with an unbaked cache holding the right frames, which will
re-simulate on the next edit rather than staying authoritative. Only tier 3
carries the bake intact. That is why a changed bake sets `bake_note` and the
host panel nags for a Force Resync, and why un-baked live sims are out of
scope: only a baked cache is state a resend can faithfully carry.

The probing also found: appending from a full save keeps `is_baked`; toggling
`use_disk_cache` converts memory↔disk (and writes `blendcache_<name>/` beside
the project); `.bphys` is zstd since 5.0, so every public third-party reader
predates the format; external caches scan their directory once on toggle, and
un-ticking External deletes files in the default location. Nobody else has
solved cache transfer either — Multiuser has it as an open blocked item and
Mixer does not support it.

Geometry nodes cross two ways: tree-side edits fire on the `GeometryNodeTree`
ID, which is a syncable tier-2 type; modifier-panel input tweaks fire only the
object's update, so their values are folded into the `~modifiers` digest.

`bpy.data.cache_files` (Alembic/USD) gets its filepath localized on the
replica, but no cache-file *content* handling. Transporting missing media or
caches over the wire is out of scope — both machines are assumed to have the
project.

## Known gaps

- **`request_nowait()` / `poll_reply()` / `cancel_request()` are not in the
  `HostTransport` Protocol** but the session depends on them. Any new
  transport must implement them; the Protocol doesn't say so.
- **`session.state["pixel_resolving"]` is never cleared by `stop()`.** Stop a
  session while ffmpeg resolution is in flight and the flag stays `True`, so
  every later `_start_pixel_path` early-returns. Sync works, the stream never
  starts again.
- **`srt_latency_ms` is missing from `prefs._SETTINGS_FIELDS`**, so it is the
  one stream setting that does not survive a reinstall — it silently reverts
  to the 300 ms default. This is the single biggest latency knob in the
  product: SRT's buffer is a ~1:1 fixed add to glass-to-glass (measured
  2026-09-22, four independent machine pairings), so a user who tuned it to
  120 and then updated the addon quietly loses 180 ms.
- `FLAG_HOLD` is reserved and never set; cold kind `"sync"` is declared but
  never produced or consumed.

## Where the other half lives

Branch `spike/parity` carries the sidecar work: a Rust tray agent
(`agent/`), a QUIC transport (`ring1/transport_kyber.py`), `agent_launch.py`
for a Blender the agent starts, and the motion-to-photon probe. Measurements
are in `spikes/parity/results/<date>-<slug>/notes.md`, and the roadmap plus
the ruled-out decisions are in `spikes/parity/PLAN.md`.

Two status notes for anyone reading this branch:

- **The ZMQ transport is frozen** — supported and working, but not receiving
  new features. Agent-era work (peer discovery, agent-owned connection
  settings) lands on the QUIC path only, which has no agent in ZMQ mode by
  construction.
- The addon's side of the agent link is framed in generic control/hot/cold
  message types and does not know which protocol the agent speaks, so
  replacing the QUIC implementation underneath is a change on the Rust side.
