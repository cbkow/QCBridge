# The sync, audited

*2026-09-23, branch `spike/quinn`, Blender 5.2.2 LTS, macOS, loopback.*

The question: now that the connection is one QUIC session with three lanes,
what in the host→replica control system could be synced better or faster,
and what is quietly wrong today? The answer comes from three code sweeps of
`qcbridge/` and `agent/src/` (every load-bearing claim re-read in source and
marked **confirmed** below; the rest are marked *reported*), a new
edit→visible latency bench (`smokes/bench_latency.sh`, numbers in §1), and the
cache probes already written up in `CACHES.md`. `ARCHITECTURE.md` on `main`
describes the design; this document is about the distance between the design
and the code.

**Short version.** The lanes bought parity, not speed: tier-1 and hot are
identical over quinn and zmq, and large blobs are *slower* on loopback. The
wins available now are structural — tier-1 on its own stream, the bidi control
lane carrying what the replica already knows, the reconnect that never
re-handshakes. The slowest thing in the system is not the network, it is the
150 ms debounce and the 0.5 s sweep. And a handful of edits a reviewer makes
every day never cross at all, with no warning.

---

## 1. Measured: edit → visible

`smokes/bench_latency.sh agent|zmq`. The host makes timestamped edits; the
replica samples the watched values on a 2 ms timer (actual cadence 6 ms
median, 13 ms p95 — the floor of the measurement). Heavy = a 640k-vertex
grid. Two runs per transport; the second is below, and the first is in the
results directory. The small-message rows repeat within 10 ms; the rows that
involve the Heavy blob move by 40–50 ms between runs, so read those as
bands, not points.

| phase | what crosses | agent p50 | agent p90 | zmq p50 | zmq p90 |
|---|---|---|---|---|---|
| t1 | `location.x` — tier-1 delta | 184 | 194 | 184 | 204 |
| t2 | modifier add on a cube — tier-2 blob | 216 | 231 | 223 | 231 |
| hot | `frame_set` — hot lane | 32 | 48 | 39 | 48 |
| sweep | custom prop — seen only by the 0.5 s sweep | **518** | 675 | **474** | 634 |
| hol0 | tier-1 in the same flush as the Heavy blob | 213 | 225 | 188 | 214 |
| hol150 | tier-1 150 ms *after* the Heavy blob | **256** (302) | 306 | **189** (217) | 194 |
| heavy | the Heavy blob itself, toggle→visible | **362** (349) | 392 | **252** (277) | 297 |

What the numbers say:

- **Tier-1 is 184 ms, and 150 of that is the debounce** (`classifier.py:14`,
  `DEBOUNCE_S = 0.15`) plus up to 50 ms of flush tick (`host_handlers.py:32`).
  The transport's share is under 10 ms. Halving the debounce would do more
  than any transport change.
- **Hot is 32 ms** — one 33 ms sampling interval plus a 15 ms apply tick.
  That is the lane doing exactly what it should.
- **The sweep path is half a second** — p50 ~500 ms, max 705 ms. Every eye
  toggle, rename, custom-prop slider and bone-idprop rig control takes this
  path (§3 C2). It is the slowest thing a reviewer does routinely.
- **Head-of-line blocking is real, and its cost is the blob's wire time.** A
  tier-1 edit made 150 ms after a big structural change lands after the
  blob: +70–120 ms over baseline on quinn loopback, +5–30 ms on zmq loopback,
  for ~30 MiB. The mechanism is the same on both (one ordered cold stream,
  §3 C3); the constant differs because quinn moves bytes more slowly on
  loopback (next point). On a 100 Mbps link the blob is ~3 s and the delta
  waits all of it, on either transport. `hol0` does not show it because both
  edits flush in one tick and the delta is serialized first.
- *After P3 (2026-09-23, `agent-after-p3/`): `t1` **103** / `t2` 137 / `hot`
  29 / `sweep` **184** / `hol150` **152** / `heavy` 302 ms p50. The debounce
  and tick were the budget, as §1 said.*
- *After P2 (2026-09-23): with tier-1 on its own stream `hol150` reads
  240 ms against a 189 ms `t1` — the wire share is gone; the ~50 ms left is
  the replica's indivisible apply of the 640k-vertex blob on its main
  thread, which no lane can hide. Splitting or threading big applies is the
  remaining lever (P3+).* Results in `results/2026-09-23-sync-latency/agent-after-p2/`.
- **quinn is slower than zmq on large payloads** — +40 % on the Heavy blob,
  and the earlier bootstrap bench showed 3.02 s vs 1.21 s at 40 M verts
  (`spikes/parity/results/2026-09-22-quinn-port/notes.md`). Loopback QUIC
  pays crypto and a 1344-byte MTU that TCP loopback does not; the ≥4 copies
  per direction (§3 D4) are shared by both. This is the honest baseline for
  the lane work in §4: the lanes must earn their keep by *ordering*, because
  raw throughput went the other way.

---

## 2. The control system in one paragraph

Host: `depsgraph_update_post` (reads only `update.id.original`; ignores the
geometry/transform/shading flags — `host_handlers.py:210-224`,
`classifier.py:54`) and a 0.5 s sweep (`:33`) mark uuids dirty; a 50 ms flush
timer drains those untouched for 150 ms; the shadow diff decides tier 1
(property tuples), tier 2 (`libraries.write` of the datablock, chunked at
4 MiB) or, for `~` structural changes, tier 2 again. Tier 3 is the whole file,
manual. *(As audited:)* everything cold rode **one** ordered QUIC stream with
a 64-message credit window whose credits returned when the *host's own* agent
handed the frame to quinn — a local bound, not backpressure. Hot is a second
stream, conflated twice. Control is a bidi stream carrying
hello/shot/zoom/ping/pong/goodbye. Replica: a 15 ms timer applies ≤8 cold
messages under a 10 ms budget (`replica_apply.py:23-24`), with tier-2 applies
explicitly outside the budget. Nothing flowed replica→host except four
scalars on the pong.

*(Since 2026-09-23, P2:)* cold carries bootstraps and blobs; a **fast** lane
— its own QUIC stream and its own byte window — carries tier-1 deltas and
tombstones, so a delta is never behind a blob on the wire. Every fast
message names the cold seq it must follow (`after`: the last chunk of the
newest blob for its datablock or of a pointer target just shipped, or the
last bootstrap chunk); the replica's `ring1/merge.LaneMerger` parks it until
that seq has *applied*, and applies fast messages even while a blob apply is
pending. Sequence numbers are per lane; the hello carries the host's
counters so a re-handshake primes the trackers. Credits are bytes. The zmq
transport aliases the fast lane onto its single stream and still merges by
header. The pong carries epoch, both seqs, gaps, parked count, want_resync,
bootstraps, unmapped paths, frozen caches and the last error.

---

## 3. Findings, ranked by what a reviewer would suffer

**A. Silently wrong — the replica shows something else and nobody is told**

| # | finding | where | status |
|---|---|---|---|
| A1 | **Shader-node property edits are invisible to the material's own diff.** `_walk_sockets` reads socket `default_value` and links only. Measured (`COVERAGE.md`): a Math operation, mute, ColorRamp stop or `node.image` on a node **wired into the shader** does reach the replica — but only because the Mesh wearing the material is resent whole (D7); on a dangling node, nothing crosses. | `host_handlers.py:777-792` | confirmed + measured → **fixed 09-23**: `~nodes` signature (node settings, mute, ColorRamp stops, curves) escalates the material itself; D7 closed alongside |
| A2 | **Tier-1 edits are destroyed when they share a diff with a Scene structural change.** The shadow advances at `:307` before the structural test at `:318`; a Scene cannot be tier-2 serialized (`tier2_io.py:49-58` returns `None`), so `@scene_camera`, view transform, resolution in that window are consumed and never sent. | `host_handlers.py:300-327` | confirmed → **fixed 09-23**: the tier-1 half of a structural diff goes out via `_send_t1` when the blob is refused, and Scene structure triggers an automatic rate-limited bootstrap (empirical in `COVERAGE.md`) |
| A3 | **Image datablocks are not detected.** Not in `_is_syncable_id`; texture paint, reload, re-rendered bakes stay stale. | `host_handlers.py:446-449` | confirmed → **fixed 09-23** for file-backed images (Image is syncable; a reload/colourspace change resends it). Pixels of an unpacked generated image still cannot cross — pack them |
| A4 | **`object.data` re-link, material slots, `instance_collection` are untracked** — swap the mesh under an object, empty diff, nothing sent. | `shadow.py:24-34`, `build_snapshot` | confirmed → **fixed 09-23**: `~data`, `~materials`, `@instance_collection` |
| A5 | **View-layer state is lost on every tier-2 resend.** `@hide`/`@lc_exclude`/`@lc_hide` are not in the blob, and the shadow is refreshed before the blob goes so the host never resends them. A hidden object reappears after any structural edit. | `host_handlers.py:375-377` | confirmed → **fixed 09-23**: the replica keeps the last `@` values per uuid and re-applies them after `apply_blob` |
| A6 | **Cold frames dropped and credited.** When the agent's session is down or its queue full, the frame is discarded and `T_COLD_ACK`ed; on session start the stale queue is drained and acked. `send_cold` returns True, the dirty set clears, the replica counts a gap. | `link.rs:197-201`, `session.rs:458-466` | confirmed → **mitigated 09-23**: the agent now emits `cold_dropped` (panel counts it) and every reconnect re-handshakes with a fresh epoch and re-bootstraps, so nothing sent into an outage is relied on |
| A7 | **Disk point caches are frozen on the replica after every resync** — see `CACHES.md` §2. | `bootstrap.py:112-119` | confirmed (probed) |
| A8 | **`unmapped_paths` and `last_error` are counted and never displayed**, contradicting three docstrings; `//` paths are silently skipped when the project dir is unknown and there is no `isdir` check on the mapped dir. | `replica_apply.py:300`, `bootstrap.py:73-96` | confirmed → **fixed 09-23**: overlay + pong + host panel show unmapped/last_error; an unresolvable project dir counts its `//` paths as unmapped instead of skipping |
| A9 | **The replica is not read-only.** No handler on the replica role; a local edit diverges forever because the host diffs against its *own* previous value. | `session.py:273`, `shadow.py:113-117` | reported → **detected 09-23**: a replica-side depsgraph handler counts updates on stamped datablocks it did not write recently (outside a blob's or frame change's wake); count and name ride the pong, the burn-in says "edited here", the host panel says "Force Resync overrides". Still not read-only — by design. |
| A11 | **A mac host's absolute paths were never mapped, on any replica** (found by `smokes/run_smoke_mapping.sh`, 2026-09-23). `localize_paths` translated only Windows-form ("wire") paths, but a bootstrap or blob carries the host's *native* paths untouched; a POSIX absolute path was neither translated nor counted as unmapped (not "foreign form" on either OS). | `bootstrap.py`, `pathmap.py` | **fixed 09-23**: `pathmap.localize_any` tries every source form; "unmapped" is now judged by existence on the replica, not by the shape of the string |
| A10 | Particle settings, rigid-body world/constraints, force fields; grease pencil, volumes, metaballs, point clouds, hair curves; NLA beyond a track count; extra view layers; scene frame range/fps/markers/world pointer — none detected. | `host_handlers.py:63-67, 446-449, 648`, `shadow.py:61-76` | reported → **fixed 09-23**: particles/textures/force fields/rigid-body world/NLA strips/scene scalars all detected (`COVERAGE.md`: 116 of 122 cross); linked libraries remain |

**B. Stuck — visible, but only a human at the host can recover**

| # | finding | where | status |
|---|---|---|---|
| B1 | **A replica restart is never re-bootstrapped.** The handshake timer returns `None` on first success and is never re-armed; `on_peer_state` is implemented in the transport and registered by nobody. Host says connected, replica says listening, deltas hit unknown uuids. | `session.py:260`, `transport_agent.py:478` | confirmed → **fixed 09-23**: `on_peer_state` registered; down→up or a new replica epoch on the pong re-arms the handshake with a fresh host epoch and bootstraps (`smokes/run_smoke_reconnect.sh`) |
| B2 | **Gaps are counted, never acted on.** `SeqTracker.observe` increments; nothing escalates; Force Resync is host-only. An unattended replica with gaps stays wrong. | `replica_apply.py:344-348`, `session.py:489` | confirmed → **fixed 09-23**: a gap or unknown uuid raises `want_resync` on the pong; `ring1/liveness.ResyncPolicy` sends one bootstrap per replica state, rate-limited |
| B3 | **Tier-2 restarts from chunk 0 on any credit refusal** with a new `blob_id`. Over 64 chunks (256 MiB) it depends on credits returning mid-loop; otherwise it re-serializes every 50 ms. | `host_handlers.py:380-387` | confirmed → **fixed 09-23**: `_t2_outbox` resumes from the refused chunk; credits are bytes (32 MiB window) so chunk count no longer matters |
| B4 | Replica pongs share the agent's 256-slot link queue with inbound cold frames (`link.frame` for both), so a big transfer can trip the 3 s liveness window. | `main.rs:192-193`, `session.rs:489, 541` | confirmed → **fixed 09-23**: the link has a priority queue for control/acks/events, drained ahead of cold frames |

**C. Slow — correct, later than it should be**

| # | finding | where | status |
|---|---|---|---|
| C1 | **150 ms debounce + 50 ms tick = 80 % of tier-1 latency** (§1). | `classifier.py:14`, `host_handlers.py:32` | measured → **fixed 09-23**: debounce 80 ms, tick 25 ms — `t1` 189 → **103 ms** p50; the 21-check, storm counter and survey unchanged |
| C2 | **Sweep-only edits** (eye toggle, rename, custom props, bone idprops, collection exclude) wait for the 0.5 s sweep *and then* a full debounce cycle, since the sweep runs before the drain in the same tick. | `host_handlers.py:280-291` | measured → **fixed 09-23**: sweep every 250 ms and sweep-marked datablocks skip the debounce — `sweep` 429 → **184 ms** p50 |
| C3 | **One cold stream, no priority.** No `set_priority` in `agent/src`; tier-1 waits behind every blob and the bootstrap (§1 hol150). | `session.rs:371-392` | confirmed → **fixed 09-23**: tier-1 and tombstones on their own stream with the `after` merge rule; contract test `test_fast_lane_is_not_behind_a_cold_blob` |
| C4 | Replica `frame_set` is clamped to 10 Hz, so host playback never plays back. | `replica_apply.py:25` | reported |

**D. Wasteful — work that produces nothing**

| # | finding | where | status |
|---|---|---|---|
| D1 | **`~pose` hashes animated `matrix_basis`** in both the flush and the sweep digest: scrubbing a rigged shot escalates a full tier-2 Object blob per rig each time the pose is sampled changed, for zero information. | `host_handlers.py:578-590` | confirmed by code → **fixed 09-23**: the digest ignores `matrix_basis` on bones the action (or NLA) animates; the survey's `anim_rig_scrub` row sends **0** rig blobs across a 12-frame scrub (no pre-fix measurement of that row exists — the baseline is the code) |
| D2 | **Edit-mode and sculpt strokes resend the whole Mesh, and the blob is the pre-edit datablock** — no `update_from_editmode()` anywhere. Correctness arrives on mode exit; the host stalls on every ≥150 ms pause. | `tier2_io.py:56-66` | confirmed → **fixed 09-23**: `serialize` flushes the edit-mesh first, and a blob byte-identical to the last one sent for that datablock (plus its point-cache signature) is skipped — `libraries.write` is deterministic for unchanged data (probed). A pause with no edit now sends nothing; a pause with one sends the current mesh. The map is primed on idle ticks after a bootstrap (two datablocks per tick), which is what turns an undo's 34 blobs into 6 |
| D3 | Tier-2 `libraries.write` is synchronous on the host main thread with no per-tick byte budget; a multi-object structural edit is a serialize storm. | `host_handlers.py:289-296` | reported → **capped 09-23**: at most two serializations per flush tick, the rest requeued (and re-touched as ready — a plain requeue after `debounce.ready()` had consumed the entry left 33 blobs unsent, caught the same hour); still synchronous |
| D4 | A 50 MiB blob is copied ≥4 times per direction (`pack_cold` join, `_read_exact` join, agent `vec!` per frame, `Reassembler` join); 4 MiB chunking is a zmq-era limit — lanes accept 256 MiB. | `transport_agent.py:78-80, 251-262`, `link.rs:179`, `protocol.py:217` | reported |
| D5 | `stats` (rtt, loss, cwnd, mtu, mbps) is emitted at 1 Hz and read by nobody. `"listening"` event branch the agent never emits; `FLAG_HOLD`, cold kind `"sync"`, the fixed-rate pacer — declared, unused. | `transport_agent.py:525, 687`, `protocol.py:25, 144` | reported → **done 09-23**: stats on the panel and burn-in; `"listening"` branch and the `"sync"` kind removed. `FLAG_HOLD` stays as a reserved protocol bit; the pacer stays in lib.rs, documented as unused |
| D6 | `build_snapshot` walks `_layer_collections()` once per collection — O(n²) on the main thread. | `host_handlers.py:767` | reported → **fixed 09-23**: memoised for 50 ms |
| D7 | **Every shader-affecting material edit resends every Mesh that wears it, whole.** The depsgraph flags the mesh (shading), the handler discards `is_updated_geometry`, and Mesh is unconditional tier 2 — a 64 KB blob per slider tick on a four-vertex plane, a full mesh in production. | `classifier.py:54-55`, measured in `COVERAGE.md` | confirmed + measured → **fixed 09-23**: shading-only updates on geometry types are skipped (by type — an Action's keyframe edit also arrives as shade=True and must not be) |

---

## 4. What the new layer makes possible

Three things exist in the agent today and are unused by the addon:

1. **Streams are cheap.** A second cold stream for tier-1 (or quinn's
   `set_priority` on the existing ones) is one line each side of
   `dial`/`serve_one` plus a lane tag. It needs a merge rule — a per-lane seq,
   or the replica applying a tier-1 delta for a uuid whose blob is in flight
   *after* the blob — and then the hol150 row collapses to the t1 row on any
   link. This is the one place where QUIC beats what zmq could do.
2. **The control lane is bidirectional** and already carries replica→host
   replies. The replica knows `apply_errors`, `last_error`, `unmapped_paths`,
   `applying`, its own frame, whether it was restarted, and (after
   `CACHES.md` §4) whether a cache is frozen. Four scalars ride the pong.
   Sending the offending uuid lets the host re-mark *that* datablock dirty
   instead of recommending a full resync; a `want_resync` flag closes B2; a
   session epoch closes B1.
3. **Telemetry is free.** `rtt_ms`/`quic_lost`/`tx_mbps` on the panel and the
   burn-in make every latency claim in this document checkable in the field,
   and make (1) measurable.

Not on the table: datagrams for hot (no measured need — 32 ms is the sampling
interval, not the wire) and frames-over-the-wire for caches.

---

## 5. Proposed order

Each step is independently shippable and testable with the bench or a smoke.

**P0 — tell the truth and recover (a day or two). Done 2026-09-23.** A8 surface
`unmapped_paths`/`last_error` on the overlay and pong; B1 register
`on_peer_state` and re-arm the handshake on a peer epoch change; B2 replica
`want_resync` on gap, host honours it; A6 refuse instead of ack when the
session is down (the host requeues, which is the path that already works);
`CACHES.md` step A (frozen-cache warning). None of this changes what
crosses; all of it changes whether anyone knows.

**P1 — close the silent holes (two to three days). Done 2026-09-23 — survey 72 → 116 of 122.** A2 test `structural`
before advancing the shadow, and give Scene a tier-2 path or a tier-1-only
fallback; A1 add node property digests to `_walk_sockets` (mute, operation,
color-ramp elements, image pointer) as `~node.*` signatures; A3 stamp Image
and treat a reload/paint as a tier-2 dependency resend; A4 `~data`,
`~materials`, `~instance` signatures; A5 replica-side cache of the last `@`
values per uuid, re-applied after `apply_blob`.

**P2 — use the lanes (two to three days, agent + addon). Done 2026-09-23: part 1 (byte credits, resume-from-chunk, pong priority, stats) and part 2 (fast stream, `LaneMerger`, per-lane seqs, hello-primed trackers).** C3 tier-1 on its
own stream with the merge rule; B3 resume from the failed chunk like
`_boot_outbox`; a byte-based window (32 MiB) replacing the 64-message one, which
also retires the 4 MiB chunk as a transport limit; B4 pongs on their own
queue; D5 stats on the panel. Verify with `hol150` → ≈ `t1` and the bootstrap
bench unchanged.

**P3 — faster where it counts (a day, mostly tuning with the bench). Done 2026-09-23 — see the after-P3 row below §1.** C1 try
`DEBOUNCE_S` 0.15 → 0.08 and the flush tick 50 → 25 ms and watch the bench
and the smoke's startup-storm counter; C2 sweep at 0.25 s with the drain
*after* the sweep in the same tick; D1 drop `matrix_basis` from the sweep's
light pose digest and take it from the depsgraph path only; D2
`update_from_editmode()` before serializing a Mesh in edit mode, and rate-limit
per-Mesh tier-2 to one in flight.

**P4 — cover it (ongoing).** `smokes/coverage/` now surveys 122 user
actions (`COVERAGE.md`: 72 cross, 50 don't) and is the acceptance test for
P1 and P3 — every fix should turn a row. Still missing from any harness:
replica restart, gap → resync, path mapping on a real mapping table, undo.
`replica_clean` should include `unmapped_paths` and a datablock-parity count.

---

## 6. Not measured, not resolved

- The `~pose` over-trigger (D1) is read from code; a scrub-and-count run on a
  rigged file would size it.
- Tier-2 serialize time vs vertex count on the host main thread — the bench
  measures toggle→visible, not the host stall alone.
- All of the above on a real link. The bench is loopback; the head-of-line
  cost scales with blob ÷ bandwidth, the quinn-vs-zmq gap may invert once
  crypto is not the bottleneck, and the reconnect behaviour (B1) is exactly
  what a VPN drop exercises.
- Everything in `CACHES.md` §5.

## 7. Reproducing

```
cargo build --release --manifest-path agent/Cargo.toml
smokes/bench_latency.sh agent            # spawns the sidecar
mkdir -p <work>/pysite && unzip -q qcbridge/wheels/pyzmq-*-cp313-*macosx*.whl -d <work>/pysite
smokes/bench_latency.sh zmq <work>
```
Results land in `<work>/latency.json`; the ones behind §1 are in
`spikes/parity/results/2026-09-23-sync-latency/`.
