"""Cache smoke, host half: with a shared cache root set, bake a cloth and
never press Force Resync. The replica must end up reading the same frames
through its own external cache path — settings crossed, frames on disk."""
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

CACHE_ROOT = os.path.join(OUT, "cacheroot")
os.makedirs(CACHE_ROOT, exist_ok=True)
prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[], cache_root=CACHE_ROOT,
)

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 24
bpy.ops.mesh.primitive_plane_add(size=10, location=(0, 0, 0))
ground = bpy.context.active_object
ground.name = "Ground"
ground.modifiers.new("Collision", "COLLISION")
bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=12, size=2,
                                location=(0, 0, 3))
sheet = bpy.context.active_object
sheet.name = "ClothSheet"
cloth = sheet.modifiers.new("Cloth", "CLOTH")
cloth.point_cache.frame_start = 1
cloth.point_cache.frame_end = 24

# A real project is a saved file — and Blender ignores use_disk_cache on
# an unsaved one, which is the whole reason the cache root needs it.
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "host_project.blend"))

session.start(prefs)
sync = session.state["sync"]

results = {"done": False}
state = {"step": 0, "settle_until": 0.0}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq, "sent_boot": sync.sent_boot,
                   "cache_note": sync.cache_note, "externalized": sync.externalized,
                   **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def mean_z(obj, frame):
    scene.frame_set(frame)
    deps = bpy.context.evaluated_depsgraph_get()
    me = obj.evaluated_get(deps).to_mesh()
    return sum((obj.matrix_world @ v.co).z for v in me.vertices) / len(me.vertices)


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and sync.caught_up(ps)


def step_connect():
    pc = cloth.point_cache
    results["externalized_before_bake"] = bool(pc.use_external and pc.filepath.startswith(CACHE_ROOT))
    results["host_filepath"] = pc.filepath


def step_bake():
    pc = cloth.point_cache
    with bpy.context.temp_override(scene=scene, active_object=sheet,
                                   point_cache=pc):
        bpy.ops.ptcache.bake(bake=True)
    results["host_baked"] = pc.is_baked
    results["host_mean_z_f20"] = round(mean_z(sheet, 20), 6)
    results["host_cache_files"] = len(os.listdir(pc.filepath)) if os.path.isdir(pc.filepath) else 0
    scene.frame_set(20)  # the hot lane takes the replica there too


def step_finalize():
    results["done"] = True


STEPS = [step_connect, step_bake, step_finalize]
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
