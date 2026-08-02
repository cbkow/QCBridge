# Changelog

Versions 0.1.1 through 0.1.6 came out of the first week of real production
use — every fix below started as field feedback from live sessions. Install
matching versions on **both machines**: the ends now tell each other their
version at connect, and the panel warns if they don't match. Mismatched ends
mostly work, but they degrade in confusing ways — update both.

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
