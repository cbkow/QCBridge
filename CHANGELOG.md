# Changelog

Versions 0.1.1 through 0.1.6 came out of the first week of real production
use — every fix below started as field feedback from live sessions. Install
matching versions on **both machines**: the ends now tell each other their
version at connect, and the panel warns if they don't match. Mismatched ends
mostly work, but they degrade in confusing ways — update both.

## 0.4.0 — in progress (folds in the unpublished 0.3.0 of 2026-10-27)

**QCView listens; the replica calls; nothing launches QCView.** The host
add-on writes QCView a card (`Inputs/blender.json` in QCView's application
folder: `%ProgramData%\QCView\Inputs` on Windows, `~/Library/Application
Support/QCView/Inputs` on macOS) with the port, the passphrase, the colour
chain and the mismatch rows, rewrites it when any of that changes and
removes it when the session stops. QCView 2.7 watches the folder, opens an
SRT listener on the card's port and applies the chain; the replica's
encoder calls that listener (the host's address comes from the agent, the
port from the hello). A viewer that joins later gets a picture within a
second: the muxer's input probe no longer waits for 64 KB, 20 frames or
five seconds (`-analyzeduration 0` meant the default), the helpers send
keyframes at the idle floor for the first seconds and re-send idle frames
at 10 Hz, and start-up black is published rather than skipped. The legacy
`qcview://stream` link stays for the manual flow, launched without
Blender's own OCIO variable. Preferences: "Stream to QCView here" and the
QCView port.

**The stream follows the file's working space.** Blender 5.2's per-file
Working Space (Render › Color Management › Working Space) derives a new
OCIO config on every load; the colour state used to read the config once
and a Linear Rec.2020 file went out through the Rec.709 matrix. Both ends
re-read whenever the config's identity moves, the block says where the
working space comes from ("Linear Rec.2020 (file)"), the replica restarts
its encoder with the new matrix and re-describes the stream on every pong,
and configs are compared by content: a different config is an error only
when the working space, display, view or look in use differ.

**The replica window is the stream.** The agent's Resolution setting
(1920x1080 default, 2560x1440, 1280x720, fullscreen, or any WxH) opens
Blender's window at that size (points on a Retina Mac), so the viewport,
the ring and the encoder mean the same number; kiosk keeps the maximised
chrome-free viewport and only goes OS-fullscreen for the fullscreen preset.
Cycles no longer renders a 4K screen for a 2560 stream.

**Library files on demand.** A linked library the replica cannot place —
no mapping, or a mapped path that is not there — is asked for through the
pong; the host ships it once (up to 64 MB) as a cold blob, the replica
caches it under its config folder and relinks. A library the host cannot
read either is reported as "missing on the host too" and never asked for
again. The status names the files the replica could not place instead of
counting "unmapped paths".

**Smaller fixes from the audit.** A frame read while the producer was
still writing is retried instead of dropped (a still scene's only new
frame could be lost, leaving the previous picture on the stream); a frame
size change mid-stream respawns the encoder pair (ffmpeg kept the stream
parameters it opened with, so a joining viewer was told the old size); a
delta repair that plans nothing obeys the 10 s bootstrap rate limit; the
handshake waits 8 s for a replica still loading a file; the offscreen
buffer is freed at exit and before a load (Blender crashed on quit on
Windows); the agent logs a failing phonebook folder once a minute; a
per-stage timeline of every session is written on each end
(`qcbridge-timeline-<role>.log` in Blender's config folder). New icons:
the revised mark on a dark plate for the app, the grey mark with status
dots in the menu bar and tray.

### 0.3.0 (built 2026-10-27, not published)

**No screen capture.** The replica's viewport leaves Blender from the
inside: for Cycles the extension registers its own render engine, a
subclass of Blender's Cycles that reads the engine's scene-linear pixels
back right after it draws (bit-identical to stock Cycles on Metal), and
for EEVEE and Workbench it renders the camera frame into an offscreen
buffer at up to 15 Hz. Frames go through a shared-memory ring to a small
encode helper (`qcb-encode-mac`, `qcb-encode-win`) that converts on the
GPU and encodes HEVC Main10; ffmpeg only muxes to SRT. The display
capture helpers, the Screen Recording permission on macOS and the
"display = viewport" kiosk requirement are gone; `Pixel Source: screen`
keeps the old path as a fallback. Both ends must be 0.3.

**The stream is scene-referred.** 10-bit 4:2:0 PQ (scene 1.0 = 100 nit)
in a BT.2020 container, so QCView applies the view transform — the
`qcview://stream` link now carries the host scene's view, look, exposure
and gamma. Measured on a denoised AgX scene: the same quality as the 0.2
line's 100 Mbps 8-bit stream at a third of the bits, and 4 dB better than
PQ in a 709 container (DESIGN-NOTES pixels). Encoder rungs are replaced
by three preferences: bitrate, stream width and frame rate; the camera
frame is fitted to the stream width in Shot Mode, so the resolution no
longer depends on the replica's screen.

**Both ends know their colour setup and say when it differs.** Each
Blender reads its own colour management through PyOpenColorIO — the
config it runs (by identity, never by path), its working space and
matrix, display device, view transform, look, exposure, gamma, white
balance, curves, the HDR view, engine and viewport sampling — and tells
the other end. The host panel shows the two side by side with every
disagreement in red: different configs, a different working space, a
view or display the replica does not have, white balance or curves QCView
cannot reproduce, a non-sRGB display device Blender shows as raw codes.
The wire is converted from the replica's real working space, not an
assumed Rec.709; the encoder helpers take the matrix on their command
line and their selftest covers a Linear P3 working space too. The
`qcview://stream` link names the host's config, working space, display
device, view and look so QCView follows them by name (QCView 2.6.3).

**A viewer can join a stream at any time.** The SRT listener used to give
up every 10 s with nobody connected and relisten after a pause; a viewer
connecting into the pause failed with "Input/output error" and a session
could look dead. The listen now waits 30 s and the pair relistens with no
pause when a viewer leaves; the pixel smoke kills a reader hard and
expects the next one to connect.

**A dropped tier-1 frame no longer costs a bootstrap.** The replica
reports how far it has applied each lane and where the first hole is;
the host re-asserts the datablocks it sent from the hole on (full tier-1
snapshots) instead of shipping the file. A refused tier-1 send used to
lose its delta silently; it is kept now.

**A dropped blob no longer costs a bootstrap either.** The replica keeps
an inventory of what it holds; a hole on the cold lane is repaired by
difference — only the datablocks that differ are resent — and the file
goes again only when the scene's structure itself differs (DESIGN-NOTES
sync delta-resync).

**Local View follows.** Entering Local View on the host, and per-viewport
collection isolation, reach the replica's viewport (agent transport;
DESIGN-NOTES sync vis-signature).

**A burst of blobs is one apply.** Every blob that has completed by a tick
applies together, and the replica evaluates its scene once — six modifier
edits at once used to restart Cycles six times (DESIGN-NOTES sync
batch-apply). The pong carries the apply batches and evaluations.

**Fixes.** Leaving kiosk no longer races the bootstrap's file load (a
Blender crash on connect when the host's session flag blipped); the
local-edit detector is no longer silenced by the 1 s view re-assert.

## 0.2.1 — 2026-09-26

**The macOS agent no longer puts a tile in the Dock.** The bundle has always
asked to be a menu-bar app (`LSUIElement`), but the tray's event loop (tao)
sets its own activation policy when the app finishes launching, and its
default is a Regular app: a Dock tile and an app menu alongside the
menu-bar icon, and focus taken from whatever was in front. The tray loop now
asks for the Accessory policy and no longer activates on launch, the same
fix the settings window got on 2026-09-25 for winit. Extension and agent
both say 0.2.1; install both.

## 0.2.0 — 2026-09-25 (the agent line, merged to `main` 2026-09-23)

Extension and agent both say 0.2.0; released together once the Windows
pass was done. Everything below has run on macOS and Windows, both roles,
with the suites in `smokes/`.

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
