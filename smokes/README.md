# Two-instance smoke suites

Two GUI Blenders on one machine: the replica listens, the host drives a
scripted scenario, and a plain-CPython poller judges the result. This is the
acceptance gate for anything that touches sync — run `run_smoke.sh` after ANY
change to `ring0/host_handlers.py` or `ring0/replica_apply.py`, and after any
transport change.

Built during the 0.1.1–0.1.6 field-feedback series and kept outside the repo
until 2026-09-22. It is here now because it is what proves a transport change,
and that should not live in one person's notes.

## Running

```zsh
smokes/run_smoke.sh                 # work dir defaults to a fresh /tmp dir
smokes/run_smoke.sh /tmp/my-run     # or pick one
QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_smoke.sh   # over QUIC
BLENDER=/path/to/Blender smokes/run_smoke.sh
```

Exit code 0 means pass; `verdict.json` in the work dir has the per-check
detail, and `host.log` / `replica.log` sit beside it. Each suite takes about
60–90 seconds and two Blender windows appear on screen — the kiosk one goes
fullscreen briefly. That is expected.

**Ports are 19990-92 and the token is `smoketok`, deliberately not the
defaults**, so a smoke run cannot collide with a live session on the same
machine.

The scripts run in place and write only into the work dir. They find the repo
from their own location, so nothing needs configuring.

`QCB_TRANSPORT=agent` needs `agent/` built (`cargo build`). `QCB_AGENT=spawn`
goes with it here: outside a real session there is no agent running to attach
to, so the transport starts a private one per role, isolated under each
instance's Blender config dir.

The default zmq path additionally needs pyzmq importable by Blender's Python:
unzip the matching wheel from `qcbridge/wheels/` into `<work-dir>/pysite/`.
The agent path does not — it is stdlib-only, which is one of the things the
transport boundary bought.

## The suites

| Script | What it covers | Pass condition |
|---|---|---|
| `run_smoke.sh` | **The 21-check regression.** Parenting, constraints (add + tweak), unparent-keep-transform, cloth bake → resync → free including the bake-note lifecycle, geometry-nodes add + input tweak, rail rig + keyframe retime, custom properties (sweep and dotted-name tier 1), shape keys (values, and no `.001` leak on re-pairing), lattice, armature pose, and `replica_clean` | `verdict.json` `"pass": true`, exit 0 |
| `run_smoke3.sh` | Scaled-spline camera rig repro: startup-storm counter (tier 2 must be 0 before the first edit), constraint poke and autokey resends mid-camera-view, disturbance recovery | No verdict block — read the printed JSON and the t2 histogram by eye |
| `run_smoke4.sh` | The field setup: kiosk replica starting **camera-less**, host pre-connected in camera view through a camera parented to a 2.5× scaled bezier circle | 5 checks including `camera_view_BOUND` and `follows_rail_orbit` |
| `run_smoke5.sh` | Production-file validation. Opens a real `.blend` read-only as host and never saves it. **Set `QCB_SMOKE_FILE`** — no path is committed here | 11 checks including `object_count_parity` and `startup_storm_free`. Written, never yet run |

## Things that are load-bearing

- **Separate `BLENDER_USER_RESOURCES` per instance.** `save_settings` writes
  there; without it a smoke run would edit real preferences.
- **Steps advance on the replica's pong sequence catching up, plus a settle
  window** — polled, never on a fixed timeline. That is why the suites are
  reliable rather than flaky.
- **API-driven edits must `obj.update_tag()`** where the UI would tag, because
  raw idprop writes fire nothing — *except* when the point of the check is the
  sweep path, where tagging would hide the bug.
- Ops inside timer steps need `bpy.context.temp_override(...)`: a window for
  `armature_add`, and scene/active-object/point-cache for `ptcache.bake`.
- The replica dump carries `persp_log` and `cam_bound` (the view matrix
  compared against the camera's inverted world matrix, tolerance 1e-3), which
  is how the camera-view checks avoid trusting the enum alone.
