# Design notes

The settled findings the code cites. Each label below is referenced from a
comment somewhere in `qcbridge/`, `agent/` or `smokes/` as `DESIGN-NOTES
<section> <label>`; the measurements and the reasoning behind them were
working documents and are not part of the repository.

## Sync

How an edit travels: tier 1 is a small delta for a tracked property
(location, a shader socket, a view-layer flag), tier 2 is the whole datablock
written with `bpy.data.libraries.write`, and the hot lane carries the frame
number and the camera. A 0.25 s sweep catches what the depsgraph does not
report.

- **§1 latency.** On loopback, one machine, agent transport: a tier-1 delta
  reaches the replica in ~180 ms p50, a tier-2 blob in ~220 ms, a frame
  change in ~35 ms, a sweep-only edit in ~180 ms since C2 (was ~500 ms).
  `smokes/bench_latency.sh` reproduces the numbers.
- **§3.** Findings are ranked by what a reviewer would suffer: A = silently
  wrong, B = stalls and drops, C = slow, D = wasteful.
- **§4.2 honesty.** The panel and the burn-in say who we are, what we want
  and what went wrong; an unmapped path or a dropped frame is counted and
  shown, never swallowed.
- **A1.** Shader-node property edits are invisible to the material's own
  diff (only socket values and links are walked); wired nodes still reach
  the replica because the mesh wearing the material is resent (D7).
- **A2.** A tier-1 edit that shares a diff with a Scene structural change
  must still go out: the Scene has no tier 2, so its tier-1 half is sent
  even when the structural blob is refused.
- **A4.** `object.data` re-links, material slots and `instance_collection`
  are tracked (`~data`, `~materials`, `@instance_collection`); swapping the
  mesh under an object is not an empty diff.
- **A5.** View-layer state (`@hide`, `@lc_exclude`, `@lc_hide`) is not in a
  tier-2 blob; the replica keeps the last `@` values per uuid and re-applies
  them after `apply_blob`, so a hidden object stays hidden after a resend.
- **A6.** A cold frame the agent cannot deliver is dropped and credited; the
  agent reports `cold_dropped`, the panel counts it, and every reconnect
  re-bootstraps, because the replica may be a new process.
- **A9.** The replica is not read-only by construction: a replica-side
  depsgraph handler counts updates on stamped datablocks it did not write,
  and the count rides the pong so the host can say so.
- **B3.** Tier-2 transfer resumes from the refused chunk; credits are bytes
  (a 32 MiB window), so chunk count does not matter.
- **B4.** Control, acks and events have their own queue in the agent's link,
  drained ahead of cold frames, so a big transfer cannot trip the 3 s
  liveness window.
- **C2.** The sweep runs every 250 ms and sweep-marked datablocks skip the
  debounce.
- **D1.** The pose digest ignores `matrix_basis` on bones the action or NLA
  animates; scrubbing a rig sends no rig blobs.
- **D2.** `serialize` flushes the edit-mesh first, and a blob byte-identical
  to the last one sent for that datablock (plus its point-cache signature)
  is skipped.
- **D4.** Big blobs are not concatenated on the main thread: the link writes
  buffers in turn and the reassembler returns a view of its buffer; chunks
  are 4 MiB, lanes accept 256 MiB.
- **D6.** `build_snapshot`'s layer-collection walk is memoised for 50 ms.
- **D7.** A shading-only update on a geometry type does not resend the
  mesh; `is_updated_geometry` is honoured.
- **fast-ack.** The pong carries how far the fast lane has been applied
  (not just seen) and where its first hole is. A hole on that lane alone
  is repaired from the host's log of what left (`ring1/fastlog`): a full
  tier-1 snapshot of each datablock named from the hole on, then a
  `resync_end` — no bootstrap. The shadow used to advance at diff time and
  a refused send silently lost the delta; it is restored on refusal. From
  the Octane add-on's "advance the camera shadow only on echo".
- **batch-apply.** Every blob that has completed by the tick applies in
  that tick, and the per-batch work (uuid map, `@` re-apply, frozen-cache
  count, parked releases) runs once; Blender evaluates once when the
  timer returns. Six blobs in a burst used to cost six evaluations and six
  Cycles restarts; they cost one (`smokes/run_smoke.sh` check 22). The
  host marks a blob `more` when more tier-2 work is ready than its tick
  budget, and the replica holds its batch 100 ms for the rest; the apply
  budget per tick is a quarter second, because a burst split across ticks
  costs an evaluation per piece (six 143 KB blobs took over 40 ms to apply
  on the Windows box). From Octane's "evaluate once per push" flag.
- **A9 quiet.** The local-edit detector's quiet window belongs to a frame
  change, not to every hot apply: the 1 s view re-assert on a static host
  used to silence it half of the time.
- **delta-resync.** The replica keeps an inventory {uuid: digest} of the
  blobs it applied; every t2 carries its digest, and what the host hashes
  after a bootstrap (`_prime_hashes`) goes over in `manifest` batches on
  the control lane, so the two inventories converge within the prime
  window. A hole on the cold lane is then answered by difference
  (`ring1/resync.plan_delta`): tombstones for what only the replica has,
  blobs for what differs or it lacks, a verify pass for what the host has
  not hashed, a full Scene tier-1 snapshot, "@" re-asserts, then a cold
  `resync_end`. Scene STRUCTURE crosses only in the file, so a mismatch of
  the structural scene digest (boot meta / Scene t1 `sd`) still means a
  bootstrap. From Octane's engine-data cache keyed by name and hash.
- **vis-signature.** Local View and per-viewport collection isolation live
  on the SpaceView3D: no depsgraph event names them and no blob carries
  them. The host samples a signature of its largest 3D view on the sweep
  (`ring1/visview`) and sends it on the keyed hot lane, re-asserted every
  second; the replica turns the difference between what it shows and what
  the host shows into the operator calls (localview, local_view_set,
  hide_collection). zmq has no keyed lane: not available there. From
  Octane's visible-set signature for empty depsgraph batches.

## Pixels

How the viewport leaves the replica since 0.3 (the 2026-10-26/27 probe and
overnight campaign; screen capture is the `pixel_source = screen` fallback
only). Cycles: a Python RenderEngine subclassing the bundled
`cycles.CyclesRender` under its own id (`QCB_CYCLES`) reads the engine's
scene-linear pixels back from the active framebuffer inside `view_draw`.
EEVEE and Workbench: a draw handler renders the camera frame into an
offscreen buffer, linear, at most 15 times a second. Either way the frame
goes to a shared-memory ring the encode helper reads; the helper converts
on the GPU and encodes HEVC Main10; ffmpeg only muxes to SRT.

- **readback.** The subclass IS Cycles: on Metal its viewport is
  bit-identical to stock (PSNR > 100 dB, light linking included); on
  Windows/GL it differs at the noise level only. `read_color` after
  `super().view_draw()` returns the engine image alone — no overlays, no
  view transform — so the stream is scene-referred and the view transform
  is QCView's.
- **rect.** In Shot Mode only the camera rectangle is read and published,
  and the camera frame is fitted to at most `stream_width` px wide: the
  stream's resolution is a preference, not the replica's screen.
- **hash skip.** 70–90 % of viewport draws carry no new pixels (Cycles
  re-blits its display texture); a blake2b over a strided centre window
  skips them for 0.1 ms. When nothing changed for eight draws the readback
  thins to one draw in four — Windows reads synchronously (18 ms at 1440p,
  48 ms at 4K through GL), the Mac in 2–5 ms.
- **startup black.** A fresh Cycles session reads back black for a few
  seconds while kernels compile; black-and-unchanged at session start is
  "not yet", never a frame.
- **pixel size.** `render.preview_pixel_size` AUTOMATIC is the UI pixel
  size: on a Retina Mac every Cycles viewport frame is rendered at half
  resolution as 2x2 blocks. The replica forces 1 on every scene it adopts.
- **offscreen.** `GPUOffScreen.draw_view3d` for EEVEE is a second,
  single-sample render per call: 48–80 ms at 1440p on both machines,
  within dE ~0.5 (p99 ~4) of the converged viewport. Workbench 0.3–1.4 ms.
  Every offscreen is freed before quit or Blender crashes on exit.
- **ring.** One block per Blender: a header page and three slots of
  descriptor page + page-aligned pixels, rows padded to 256 bytes, a
  seqlock per slot, `latest = (seq << 8) | slot`, `reader_claim` so the
  writer steps over the reader's slot. Python has no fence: every control
  word is one aligned 8-byte store and the helper re-checks the seqlock
  after its copy, so weak ordering costs a retry, never a torn frame. A
  taller frame retires the block and a new one is created under a new
  name (a Windows section still held by the helper hands back the old
  object). Blender-side cost at 1440p: 2.3 ms read + 1 ms publish (Metal),
  16 + 2.5 ms (GL); 10-minute soaks flat on both machines.
- **wire.** HEVC Main10 4:2:0, PQ with scene 1.0 = 100 nit, BT.2020
  container (working space → 2020 before the curve, by the matrix below;
  tags bt2020 / smpte2084 / bt2020nc, limited range, pinned in the mux
  with `hevc_metadata`), view transform in QCView. On a denoised AgX scene at equal NVENC bitrate: PQ
  in a 2020 container 43.2 dB / dE 0.77 at 30 Mbps against 38.9 for the
  same curve in a 709 container; a 10-bit display-referred AgX stream
  43.4 dB at 53 Mbps; the 0.2 line's 8-bit stream 39.0 dB / dE 1.37 at
  97 Mbps. 4:4:4 and 4:2:2 add nothing on AgX content; noisy (256-sample)
  viewports score the same whatever the curve; saturated gamut-edge noise
  (a spectral laser scene) is the known 4:2:0 corner at 22–25 dB on every
  option. 10-bit PQ before any codec sits at 53–59 dB.
- **idle floor.** A converged viewport stops producing; the helper re-sends
  the last frame a few times a second so an SRT viewer joining a silent
  stream is not left in its demuxer probe.
- **probe.** The parity strip is stamped into the published float frame
  (black 0.0, ones 10.0 scene-linear: above the reader's threshold after
  PQ) at the overlay's old geometry; the host reader is unchanged.
- **working space.** The framebuffer is scene-linear in the OCIO config's
  `scene_linear` role — Linear Rec.709 in Blender's own config, but a fact
  of whichever config this Blender loaded (a studio config may say
  otherwise), never an assumption (2026-10-27). The replica reads the
  role through PyOpenColorIO, which ships inside Blender's Python, takes
  its matrix to CIE XYZ D65 off the three primaries, and hands the helper
  working → BT.2020 as nine numbers (`--matrix`); the descriptor says
  which space and whether it was read or, without PyOpenColorIO, assumed.
  Two selftest fixtures: Rec.709 and Linear P3 — the second fails a helper
  that ignores or transposes the matrix, which the near-identity Rec.709
  rows let through.
- **colour state.** Each end reads its own colour management — config
  identity (cache ID, file, kind; never the path), working space and
  matrix, display device and its primaries, emulation, view, look,
  exposure, gamma, white balance, curves, HDR view, engine and viewport
  sampling — into one block (`ring1/colour_state.py`) that rides the hello,
  the hello reply, the pong and a `cm` message on change. The host panel
  shows both columns and every disagreement in red: the two ends may run
  different configs, and then the synced scene's numbers mean different
  colours. The `qcview://stream` link carries the host's block by name so
  QCView follows the host's view for the host's display device or says
  what it cannot (QCView docs: qcbridge-blender). Measured with the studio
  config: Blender 5.2.2 on macOS writes a Rec.2020 or Display P3 device's
  codes straight into its sRGB-tagged window, so the artist on such a
  device sees a muted picture (red 0.72/0.36/0.23 for AgX-sRGB's
  0.86/0.22/0.13) and the panel says so.
- **viewer rejoin.** The mux's SRT listener takes one viewer; when that
  viewer leaves the mux exits and the pair is respawned at once, no
  backoff (the backoff is for a dying helper). The listen itself gives up
  after 30 s with nobody there so an orphaned pair dies on its own — at
  10 s the relistens came every 17 s and a viewer landing in a gap failed
  with "Input/output error" (found 2026-10-27). The pixel smoke kills a
  reader hard and expects the next within 45 s, then one more after an
  idle. `smokes/run_harness_colour.sh` measures the whole path on a still
  scene: wire vs Blender's linear render (PSNR > 30 dB, dE mean < 1 —
  viewport vs final render bottoms near 34 dB), QCView's picture vs the
  host's view for the host's display (dE mean < 1.5, p99 < 4).
- **the file's working space (0.4).** Blender 5.2 lets a FILE choose its
  Working Space (`bpy.data.colorspace`, Render › Color Management); on
  load Blender derives a new OCIO config (new cache ID, `scene_linear`
  moved). The colour state is therefore re-read whenever the current
  config's cache ID moves — `GetCurrentConfig()` + `getCacheID()` cost a
  microsecond — and on `load_post` / after every bootstrap; the block says
  `working.source` ("file:lin_rec2020_scene" or "config"). The replica
  rebuilds its stream parameters and restarts the encoder with the new
  matrix, and re-describes the stream on every pong. Found 2026-10-27: a
  Linear Rec.2020 file streamed through the Rec.709 matrix read from the
  default scene, "desaturated like 709 vs 2020". Configs are compared by
  CONTENT (working space, display, view, look in use), never by cache ID:
  two Blenders on the same file never share one.
- **the probe (0.4).** ffmpeg opens its OUTPUT only after its input probe:
  `probesize` bytes, 20 frames (HEVC's rate probe) or `analyzeduration`
  — and `-analyzeduration 0` means the 5 s default (demux.c). An idle
  encoder (one small keyframe, tiny P-frames a few times a second) kept
  the old 64 KB probe waiting a minute; viewers calling meanwhile got
  "Input/output error". Now `-probesize 32 -analyzeduration 1
  -fpsprobesize 0 -use_wallclock_as_timestamps 1`, the helpers send
  KEYframes at the idle floor for the first 3 s of a session and re-send
  idle frames at 10 Hz, start-up black is published at 2 fps (`F_WARMUP`)
  instead of skipped, and QCView's decoder opens with `fpsprobesize 0`
  (the same 20-frame wait cost it 5 s a join). Pixel smoke:
  `viewer_connects_fast` (a retrying caller has a frame within 5 s).
- **stale stream parameters (0.4).** `ffmpeg -c copy` keeps the stream
  parameters it opened with: after a mid-stream frame-size change
  (Shot Mode) a joining viewer was told the old size while the frames
  were the new one. The helper exits (8) on a size change and the pair
  respawns, one reconnect for the viewer. And a frame read while the
  producer is still writing is retried, never dropped with the cursor
  advanced: a still scene publishes its new frame once, and losing it
  left the previous picture on the stream.
- **the replica window IS the stream (0.4).** The ring takes the viewport's
  size, so a fullscreen kiosk had Cycles rendering 3840x2160 for a 2560
  stream. The agent's `replica_resolution` (1920x1080 default, 2560x1440,
  1280x720, fullscreen, WxH) launches Blender with `--window-geometry`
  (POINTS: pixels / the backing scale the add-on reports once), kiosk
  maximises the area and strips chrome, and OS fullscreen is only the
  `fullscreen` preset. The add-on's stream width mirrors the agent's
  setting. Measured: a 960x540-point window on the Retina Mac gives a
  1920x1080 framebuffer after `screen_full_area`; 1:1 on the box.
- **QCView listens; the card (0.4).** QCView is never launched by Blender
  (the launch inherited Blender's own `OCIO` variable, and QCView loaded
  Blender's bundled config under the name "$OCIO"). The host add-on
  writes `blender.json` into QCView's Inputs folder on the host machine
  (`%ProgramData%\QCView\Inputs` on Windows — ProgramData is never
  virtualized, the Store build reads its output manifests there;
  `~/Library/Application Support/QCView/Inputs` on macOS): port,
  passphrase (the same derivation as the link), the colour block as the
  link's keys, the panel rows and mismatch rows; rewritten on change (1 Hz
  digest), removed on stop. QCView watches the folder and opens an SRT
  LISTENER on the port; the replica's mux CALLS it — the hello carries the
  viewer port, the agent tells the add-on the peer's address (`peer_addr`),
  the stream descriptor carries `push`. The replica listens as before when
  the host has no viewer block (an older host) or the preference is off,
  and the legacy `qcview://stream` launch scrubs `OCIO`. The AE/Premiere
  bridges hand off through shared memory on one machine; this is the
  file-shaped equivalent for a stream that crosses the network.
- **library files on demand (0.4).** A linked library the replica cannot
  place — no mapping matched, or a mapped path that is not there — goes
  into its pong (`need_libs`, host-native paths from the link headers and
  the bootstrap's unplaced list). The host ships each readable one once
  (`libfile` cold blob, ≤ 64 MB, meta lib/sha1/name) or answers
  `lib_missing` (not on the host either, too big); the replica stores it
  under `<config>/qcbridge-libcache/<sha1>-<name>` and relinks. Textures
  are NOT shipped: the path mapping serves them from the share (1.3 GB in
  the audited project). The status names the files it could not place
  and the libraries missing on the host too, instead of "N unmapped
  paths" (the audited project's four were broken on the host).
- **the timeline (0.4).** Every session writes one line per stage on each
  end (`ring1/timeline.py`, `qcbridge-timeline-<role>.log` in Blender's
  config folder): agent, handshake, every bootstrap with its reason and
  bytes, deltas, gaps, apply errors, engine adoption, first signal and
  first published frame, encoder spawns and exits with why, the card,
  Shot Mode. The audit of 2026-10-27 took hours without it; with it the
  second run took one read.

## Caches

- **§2 finding 5.** Reassigning `pc.filepath = pc.filepath` (or `frame_end`,
  or `name`) makes Blender rescan a point cache and set `is_baked` from the
  files. No `use_external` toggle is needed; toggling it can delete files.
- **§2 finding 6.** Converting an already-baked cache to external does not
  migrate its frames: the cache reads as baked with zero files at the new
  path, a frozen sim. The external path must be set before the bake.
- **§2.** A memory cache that crosses tier 2 is inert; a disk cache is frozen
  on the replica after a resend; a geometry-nodes bake is invisible to RNA
  and crosses only as the packed bake on the object.
- **§4 B.** A shared cache root, set before baking: the host externalises
  unbaked point caches under `<root>/<file>/<uuid>/<sim>` at session start
  and on the sweep, refuses on an unsaved file, and counts baked caches it
  leaves alone. The replica maps the path and reassigns it (finding 5).
- **§4 D.** `NodesModifier.bake_directory`, custom bake directories,
  `FluidDomainSettings.cache_directory` and external `PointCache.filepath`
  are mapped through the mapping table at bootstrap and after every tier-2
  arrival.

## Coverage

A survey of 122 user actions on loopback: 120 reach the replica. The
catalog is `smokes/coverage/catalog.py`; adding an action is one function
with a `@probe`. The misses shared six causes: a short tracked-path table,
custom props swept on Objects only, no Scene tier 2, node trees walked for
sockets and links only, pointer-only digests, and datablock types outside
every set.

- **Cause 5, pointer-only digests.** `texture`, `particle settings`,
  `focus_object` were recorded as pointers, so a change inside the pointee
  was invisible; the owner is re-shipped when the pointee changes.
- **The inventory.** Types that cross only as a side effect of their owner
  now have their own tier-2 path.

## Agent

Decisions of 2026-09-24, with the owner:

- **§8.** One agent per machine with a Send scene / Receive scene switch,
  live without a restart. The settings window is the tray's UI: this
  machine, pairing, storage, stream, diagnostics. Blender's preferences are
  a read-out of the agent. Every path field has a folder picker that
  proposes the other platform's form from the mount table. The token lives
  in the OS keychain and only its fingerprint is shown.
