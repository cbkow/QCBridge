"""Smoke 5: HOST on chris's real production file (opened via CLI before this
script runs; NEVER saved — all edits stay in-memory and are reverted).
Validates storm-free startup, bootstrap integrity, camera binding and a
nudge/revert round-trip against real data."""
import json
import os
import sys
import time
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

from qcbridge.ring0 import session  # noqa: E402

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=300,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

scene = bpy.context.scene
cam = scene.camera

results = {
    "done": False,
    "file": bpy.path.basename(bpy.data.filepath),
    "n_objects": len(bpy.data.objects),
    "n_meshes": len(bpy.data.meshes),
    "n_actions": len(bpy.data.actions),
    "scenecam_name": cam.name if cam else None,
    "scenecam_parent": (cam.parent.name if cam and cam.parent else None),
    "scenecam_parent_type": (cam.parent.type if cam and cam.parent else None),
    "frame": scene.frame_current,
    "prestamped": sum(
        1 for o in bpy.data.objects if o.get("qcbridge_uuid")
    ),
}

for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"

session.start(prefs)
sync = session.state["sync"]

state = {"step": 0, "settle_until": 0.0, "saved": None}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq,
                   "sent_t2": sync.sent_t2,
                   "sync_errors": sync.sync_errors,
                   "t2_unsupported": sync.t2_unsupported,
                   **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def mat(obj):
    bpy.context.view_layer.update()
    return [round(v, 6) for row in obj.matrix_world for v in row]


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and ps.get("seq", -1) >= sync.seq


def step_connect():
    pass


def step_baseline():
    results["t2_baseline"] = sync.sent_t2  # 0 = storm-free on real data
    results["scenecam_m_base"] = mat(cam) if cam else None


def step_nudge():
    target = cam.parent if (cam and cam.parent) else cam
    if target is None:
        return
    state["saved"] = (target.name, tuple(target.rotation_euler))
    target.rotation_euler.z += 0.05


def step_record_nudge():
    results["scenecam_m_nudged"] = mat(cam) if cam else None


def step_revert():
    if state["saved"]:
        name, rot = state["saved"]
        bpy.data.objects[name].rotation_euler = rot


def step_finalize():
    results["scenecam_m_final"] = mat(cam) if cam else None
    results["t2_final"] = sync.sent_t2
    results["done"] = True


STEPS = [
    step_connect, step_baseline, step_nudge, step_record_nudge,
    step_revert, step_finalize,
]
SETTLE = 12.0


def _driver():
    now = time.monotonic()
    dump()
    if state["step"] >= len(STEPS):
        return 0.5
    if now < state["settle_until"] or not peer_caught_up():
        return 0.25
    STEPS[state["step"]]()
    state["step"] += 1
    state["settle_until"] = now + SETTLE
    dump()
    return 0.25


bpy.app.timers.register(_driver, first_interval=2.0, persistent=True)
