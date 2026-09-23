"""Smoke 3 (regression repro): scaled-spline camera rig + camera view +
shot mode. Measures t2 churn and whether the replica's camera view survives
camera-chain resends."""
import json
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

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=300,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 24, 24
scene.frame_start = 1

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
rail.scale = (2.5, 2.5, 2.5)  # the "scale?" ingredient, pre-bootstrap
curve.use_path = True
curve.path_duration = 24
curve.eval_time = 0.0
curve.keyframe_insert("eval_time", frame=1)
curve.eval_time = 24.0
curve.keyframe_insert("eval_time", frame=24)

shotcam = bpy.data.objects.new("ShotCam", bpy.data.cameras.new("ShotCamD"))
scene.collection.objects.link(shotcam)
con = shotcam.constraints.new("FOLLOW_PATH")
con.target = rail
con.use_curve_follow = True
scene.camera = shotcam

nestcam = bpy.data.objects.new("NestCam", bpy.data.cameras.new("NestCamD"))
scene.collection.objects.link(nestcam)
nestcam.parent = rail  # plain "nested under the spline"
nestcam.location = (0, 0, 1)

session.start(prefs)
sync = session.state["sync"]

results = {"done": False}
state = {"step": 0, "settle_until": 0.0, "scrub": 1}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq,
                   "sent_t2": sync.sent_t2, **results}, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "host.json"))


def mat(obj):
    bpy.context.view_layer.update()
    return [round(v, 6) for row in obj.matrix_world for v in row]


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and sync.caught_up(ps)


def _view3d():
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == "VIEW_3D":
                return a
    return None


def step_connect():
    pass


def step_cam_view():
    area = _view3d()
    area.spaces.active.region_3d.view_perspective = "CAMERA"
    area.tag_redraw()
    results["t2_before_scrub"] = sync.sent_t2


def step_scrub():
    # advance 4 frames per driver tick until 24 (scrub-ish), then move on
    if state["scrub"] < 24:
        state["scrub"] = min(24, state["scrub"] + 4)
        scene.frame_set(state["scrub"])
        state["step"] -= 1  # stay on this step
        state["settle_until"] = time.monotonic() + 0.3
        return
    results["t2_after_scrub"] = sync.sent_t2
    results["shotcam_m_f24"] = mat(shotcam)
    results["nestcam_m_f24"] = mat(nestcam)


def step_scale_rail():
    rail.scale = (3.5, 3.5, 3.5)
    results["t2_before_poke"] = sync.sent_t2


def step_record_scale():
    results["shotcam_m_scaled"] = mat(shotcam)
    results["nestcam_m_scaled"] = mat(nestcam)


def step_poke_camera():
    # Force a camera-object T2 resend while the replica sits in camera view
    # (constraint digest change) — the view-pop reproduction.
    con.influence = 0.999
    results["t2_at_poke"] = sync.sent_t2


def step_after_poke():
    results["t2_after_poke"] = sync.sent_t2
    results["shotcam_m_poked"] = mat(shotcam)


def step_autokey():
    # Animation edit on the viewed camera → ~anim change → another camera
    # T2 resend mid-camera-view (the autokey workflow).
    shotcam.keyframe_insert("location", frame=24)
    results["t2_at_autokey"] = sync.sent_t2


def step_after_autokey():
    results["t2_after_autokey"] = sync.sent_t2


def step_shot_on():
    session.shot_mode_toggle()


def step_shot_check():
    results["shot_on_host"] = session.state["shot_mode"]


def step_shot_off():
    session.shot_mode_toggle()


def step_finalize():
    results["t2_final"] = sync.sent_t2
    results["done"] = True


STEPS = [
    step_connect, step_cam_view, step_scrub, step_scale_rail,
    step_record_scale, step_poke_camera, step_after_poke,
    step_autokey, step_after_autokey,
    step_shot_on, step_shot_check, step_shot_off, step_finalize,
]
SETTLE = 3.0


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
