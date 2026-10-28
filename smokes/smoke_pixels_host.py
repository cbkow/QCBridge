"""Pixel smoke, host half: a small Cycles scene in camera view, Shot Mode
on, the cube turning every half second so the replica's viewport keeps
producing frames. Dumps the viewer URL the runner probes with ffprobe."""
import json
import math
import os
import sys
import time
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from smokes._atomic import replace as _atomic_replace  # noqa: E402
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

from qcbridge.ring0 import session  # noqa: E402

ENGINE = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=True, srt_port=19998, srt_url="", srt_latency_ms=120,
    pixel_source="readback", stream_bitrate_mbps=20, stream_width=1280, stream_fps=30,
    ffmpeg_path="", replica_kiosk=False, path_mappings=[],
)

scene = bpy.context.scene
scene.render.engine = ENGINE
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
if ENGINE == "CYCLES":
    scene.cycles.preview_samples = 16
    scene.cycles.use_preview_denoising = False
scene.view_settings.view_transform = "AgX"
cube = bpy.data.objects["Cube"]
mat = bpy.data.materials.new("Orange")
mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1.0, 0.35, 0.05, 1.0)
cube.data.materials.append(mat)
cam = bpy.data.objects["Camera"]
scene.camera = cam

# The host looks through the camera in its largest 3D view, as a user would.
for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"

session.start(prefs)
sync = session.state["sync"]
results = {"done": False, "shot": False, "turns": 0}
state = {"t0": time.monotonic(), "last_turn": 0.0}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"seq": sync.seq, "note": session.state["note"],
                   "viewer_url": session.viewer_url(), "deep_link": session.qcview_deep_link(),
                       "summary": session.summary_line(), "status_rows": session.status_rows(),
                   "peer_stream": session.state.get("peer_stream"),
                   "peer_status": getattr(session.state["transport"], "peer_status", {}),
                   "cm_host": session.state.get("cm"), "cm_replica": session.state.get("peer_cm"),
                   "cm_mismatches": session.state.get("cm_mismatches"),
                   **results}, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "host.json"))


def _driver():
    now = time.monotonic()
    dump()
    if session.state["note"] != "connected":
        return 0.25
    if not results["shot"] and now - state["t0"] > 4.0:
        session.shot_mode_toggle()
        results["shot"] = True
    if results["shot"] and now - state["last_turn"] >= 0.5:
        state["last_turn"] = now
        cube.rotation_euler.z += math.radians(7)
        results["turns"] += 1
    if results["turns"] >= 90:
        results["done"] = True
        dump()
        return 1.0
    return 0.1


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
