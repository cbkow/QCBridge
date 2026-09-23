"""Smoke 4: chris's exact field setup — kiosk replica starting CAMERA-LESS,
host already in camera view through a camera PARENTED to a scaled circle
spline. The replica must land in bound camera view with zero manual input."""
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

curve = bpy.data.curves.new("Rail", "CURVE")
curve.dimensions = "3D"
spline = curve.splines.new("BEZIER")
spline.bezier_points.add(3)
for i, co in enumerate([(4, 0, 2), (0, 4, 2), (-4, 0, 2), (0, -4, 2)]):
    p = spline.bezier_points[i]
    p.co = co
    p.handle_left_type = p.handle_right_type = "AUTO"
spline.use_cyclic_u = True
rail = bpy.data.objects.new("Rail", curve)
scene.collection.objects.link(rail)
rail.scale = (2.5, 2.5, 2.5)

shotcam = bpy.data.objects.new("ShotCam", bpy.data.cameras.new("ShotCamD"))
scene.collection.objects.link(shotcam)
shotcam.parent = rail  # chris: "my camera is parented to a circle spline"
shotcam.location = (0, -3, 1)
shotcam.rotation_euler = (1.35, 0, 0)
scene.camera = shotcam

# Host sits IN camera view before the session even starts.
for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"

session.start(prefs)
sync = session.state["sync"]

results = {"done": False}
state = {"step": 0, "settle_until": 0.0}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq,
                   "sent_t2": sync.sent_t2, **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def mat(obj):
    bpy.context.view_layer.update()
    return [round(v, 6) for row in obj.matrix_world for v in row]


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and sync.caught_up(ps)


def step_connect():
    pass


def step_record_initial():
    results["shotcam_m1"] = mat(shotcam)


def step_orbit_rail():
    rail.rotation_euler = (0, 0, 0.8)  # camera rides the parent


def step_record_orbit():
    results["shotcam_m2"] = mat(shotcam)


def step_finalize():
    results["done"] = True


STEPS = [
    step_connect, step_record_initial, step_orbit_rail, step_record_orbit,
    step_finalize,
]
SETTLE = 12.0  # kiosk launch stagger runs to ~8.5 s + verify retries


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


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
