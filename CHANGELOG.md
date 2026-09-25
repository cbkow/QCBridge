# Changelog

Versions 0.1.1 through 0.1.6 came out of the first week of real production
use — every fix below started as field feedback from live sessions. Install
matching versions on **both machines**: the ends now tell each other their
version at connect, and the panel warns if they don't match. Mismatched ends
mostly work, but they degrade in confusing ways — update both.

## 0.2.0 — unreleased (the agent line, merged to `main` 2026-09-23)

The number is set — extension and agent both say 0.2.0 — but it ships only
with the coordinated release once Windows is verified. Everything below has
run on macOS, both roles on one machine, with the suites in `smokes/`; the
Windows pass is in progress.

**Agent mode is the default whenever an agent is registered** on the
machine for the session's role (2026-09-23): a plain launch of Blender lands
in the shipped mode, and the panel shows the agent's connection. Without an
agent the frozen zmq transport still runs end to end; `QCB_TRANSPORT`
and the `transport` preference override either way.

**The connection lives in an agent.** A small tray app (Rust) on each
machine owns the QUIC session, so a Blender restart or a dropped link no
longer means a lost connection. It finds replicas three ways — a direct
address (the VPN path), a phonebook on shared storage, or multicast on the
LAN (opt-in, and never colliding with the sister tools' beacons) — and
takes settings at runtime from a tray menu. Blender's own preferences panel
mirrors the agent's settings rather than owning a second copy. The zmq
transport from 0.1.6 stays as a fallback.

**Quiet on Windows, and it takes its children with it.** The agent is a
windowless program there: no console to close by mistake (that ended the
first logon-task runs), a message box when it cannot start, and its
diagnostics in `agent.log` beside `agent.toml`, Blender's output in
`blender.log` next to it, and a capture helper the agent itself runs in
`capture.log`.
Killing the agent, however it happens, now kills the Blender, helper and
ffmpeg it started, so the next agent never finds its ports taken. One
agent per role and directory, exactly. Autostart is a Run-key value
(`agent/windows/autostart.ps1`) instead of a scheduled task.

**The token stays in the agent.** It lives in the OS credential store
(macOS Keychain, Windows Credential Manager; an owner-only file where
there is neither, or `token_store` says so), `agent.toml` never carries
it, and a token found there from an older install is moved out on the
first start. Blender never sees it: the agent hands the addon the SRT
passphrase and a hello secret derived from it, and both ends show an
eight-character fingerprint so two people can tell they typed different
tokens without reading theirs out. The token is set from the panel's
lock icon (the agent's window, later); the addon's own token field
remains only for the zmq fallback.

**One mapping table, on the host.** The path-mapping rows and the shared
cache root belong to the agent now, and the host's rows travel to the
replica when the two pair, so a row entered on the host machine is
enough — the replica no longer needs its own copy (it may still have
one; the two are merged, same roots not doubled). An existing table in
Blender's preferences moves into the agent on the first session; from
then on the table is edited in the agent's window and the preferences
hold no copy of it.

**A settings window, and nothing else to set in Blender.** The agent's
tray has a Settings… item, and Blender's preferences and QC Bridge panel
an Open Agent Settings button, both opening one native window for
everything the agent owns. In agent mode Blender's preferences are a
read-out of the agent — role, name, receiver or listen address, token
fingerprint, shared folder — plus the Pixel Path, which is Blender's own;
the role, address, port, token, cache-root and path-mapping fields, the
Save to Agent and Set Token buttons and the panel's discover box are gone
(2026-09-25), and the session's role is the agent's: the addon adopts
whichever role the installed agent is set to when a session starts, and
starts the installed agent if none is registered. The window holds: the machine's name, the
Send scene / Receive scene switch (which takes effect at once — no
restart, one agent per machine), the Blender to run, the token with its
fingerprint, the receiver to dial with a Find receivers list and a Pair
button, the pinned certificate, the network mode and the phonebook
folder, the shared storage folder as this machine sees it — simulation
caches and the receiver phonebook are its `cache` and `phonebook`
subfolders, made for you, every subfolder maps on its own, and when a
Windows machine pairs with a Mac the mapping row for that folder forms
by itself from what each side named (the table stays visible to check
or correct; custom folders under Advanced) — the stream cap and scale,
and the diagnostics. The window is its own process attached
to the agent beside Blender, so nothing it does disturbs a session. On
macOS it stays out of the Dock and the menu bar while open, like the tray
it belongs to (2026-09-25: winit had been making it a regular app for as
long as the window was up).

**Blender on the replica follows the host's session, not the link
(2026-09-25).** Two paired agents used to be reason enough for the
replica to launch Blender and keep it, in kiosk, for as long as the link
stood. Now the host agent tells the replica on the control lane whether
its own Blender is attached — the host is in a session — and the replica
launches Blender for that alone. When the session ends (Stop Session,
the host's Blender quitting, a goodbye, or the link dropping) the
replica's Blender leaves kiosk and is closed after the grace in *Close
Blender after* (default 20 s, 0 = at once; it was 300 s and "0 = never");
a host that restarts its session inside the grace finds it warm. The
status line and the `status` event say which it is ("host connected, no
session" / "host in session").

**Installers.** The agent ships as *QCBridge Agent.app* in a signed,
notarized package on macOS and as an Inno Setup installer on Windows;
the extension zip carries no binaries and starts the installed agent
itself when a session needs one. No autostart, by decision.

**Faster, and honest about it.** An edit reaches the replica in about
105 ms (it was ~185), a visibility toggle or rename in about 200 ms (it
was ~500), and a small edit no longer waits behind a large mesh: deltas
ride their own lane and the replica applies them in the right order
relative to the blob they follow. Blobs the replica already holds are not
resent (an undo costs 6 transfers where it cost 34). Link stats — round
trip, loss, throughput — show on the host panel and in the burn-in.

**More of what you do reaches the replica.** 120 of the 122 user actions in
the coverage survey cross now, from 72. New: object display and instancing
settings, ray visibility and holdout, delta transforms, every light and
camera setting that shows in a render, world colour and world switching,
scene frame range, fps, units, gravity, render region, Cycles/EEVEE render
settings, view layers, passes and the compositor (through an automatic
bootstrap), custom properties on any datablock, shader-node settings and
ColorRamps, node groups, NLA strips, particle settings, force fields,
rigid-body world, legacy texture settings, fluid and geometry-nodes bake
directories, metaballs, grease pencil, volumes, hair, point clouds, light
probes, Alembic/USD caches, and linked libraries. A whole-mesh resend that
used to follow every material slider is gone.

**It recovers on its own.** A replica that restarts is re-bootstrapped
without anyone pressing anything; a dropped frame is detected as a gap and
the replica asks for — and gets — a resync; the host says when frames were
lost. Edits made on the replica are reported to the host ("edited here")
rather than diverging silently.

**Simulation caches.** An opt-in **shared cache root** in the host's
preferences: point caches are externalized there before they are baked, so
a bake on the host is a bake on the replica with no Force Resync, and disk
caches no longer freeze the replica after a resync. The replica says when
a disk cache is frozen and the host panel says what to do. Geometry-nodes
simulation and bake nodes cross and are used.

**Paths.** A mac host's absolute paths are now mapped on a Windows replica
(they never were); whatever cannot be resolved is counted and shown on
both panels instead of silently skipped. Libraries the replica had to
repoint are reloaded.

**Known limits.** Windows is unverified for all of the above. Native
capture on the replica is macOS only; the ffmpeg path works everywhere. An
unused datablock has nothing to cross until it is used; pixels painted on
an unpacked generated image do not cross (pack it). The replica is still
not read-only — it tells you, it does not stop you.

## 0.1.6 — 2026-08-02

- The replica now lands in camera view on its own at session start, kiosk
  included. Previously, if sync connected before the scene arrived, the
  viewport could get stuck *claiming* to be in camera view without actually
  looking through the camera — the fix was toggling Num0 out and back in by
  hand. The replica now detects that state and does the toggle itself.
- The replica's apply loop survives errors instead of silently dying: one bad
  apply now costs a fraction of a second, not the session.

## 0.1.5 — 2026-08-02

- No more startup burst: starting a session used to re-send a pile of heavy
  data the bootstrap already carried, which made the first half-minute of a
  session feel unsettled. The host now sends nothing that the initial scene
  transfer already covers.
- The replica's view self-heals: if anything disturbs it — a kiosk
  transition, a stray click on the replica, a camera swap — it converges back
  to following the host within a second.
- Version handshake: both panels (and the burn-in overlay) warn when the two
  ends run different versions.
- Renames now sync, undo on the host no longer confuses the replica, and one
  problem object can no longer silently stop sync on the host.
- Multi-viewport replicas follow in the largest 3D viewport, matching how the
  host picks the one you're working in.

## 0.1.4 — 2026-08-02

- The replica's camera view survives edits that re-send the camera itself
  (constraint tweaks, autokey). Previously these could knock the replica out
  of camera view and leave it stuck there; Shot Mode re-fits its framing
  after such an edit too.

## 0.1.3 — 2026-08-02

Rig controls sync. Everything here escalates to a normal re-send under the
hood — nothing new to configure:

- **Custom properties** on objects — rig-control sliders on nulls — sync
  live, including properties added mid-session.
- **Shape key** values and mutes sync as they're dragged. Also fixes a bug
  where every re-send of a mesh with shape keys quietly leaked a duplicate
  Key datablock on the replica.
- **Lattices** sync, point edits included.
- **Armature pose** syncs — bone transforms, per-bone constraints and
  custom properties, bendy-bone settings. Edit-mode bone changes sync too.

## 0.1.2 — 2026-08-02

- **Animation syncs.** Keyframe edits, retiming, new actions, "Animate Path"
  on a rail curve — all of it now crosses. Before this, editing existing
  keys never reached the replica, and could leave it snapping between the
  current pose and stale animation.
- New objects created mid-session sync no matter which collection they land
  in (previously, objects added straight to the Scene Collection were
  silently skipped).

## 0.1.1 — 2026-08-01

- **Parenting syncs** — Ctrl+P, Alt+P, keep-transform variants. This was the
  big one: parent relationships made mid-session never crossed at all, which
  broke camera rigs (cameras under nulls or spline rails) in ways that only
  showed up once you moved the rig.
- **Constraints sync** — adding, removing, and every property tweak
  (targets, influence, axes).
- **Modifier property edits sync** (previously only adding/removing a
  modifier crossed) — including Geometry Nodes modifier inputs on
  Blender 5.x.
- **Physics caches:** baking or deleting a sim cache (cloth etc.) is now
  detected. Baked caches only travel in a full re-send, so the host panel
  tells you when a Force Resync is needed to ship one. Un-baked live sims
  can't be mirrored faithfully — bake to sync.

## 0.1.0 — 2026-07-27

Initial release.
