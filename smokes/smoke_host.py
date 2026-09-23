"""Smoke host: scripted look-dev session exercising parent/constraint/pcache
sync. Steps advance only when the replica's pong seq has caught up AND a
settle window has passed (poll, never fixed timelines)."""
import json
import os
import sys
import time
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))  # vendored pyzmq wheel

from qcbridge.ring0 import session  # noqa: E402

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=300,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

# ── scene: everything the bootstrap should carry ─────────────────────────────
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 24

cube = bpy.data.objects["Cube"]
cube.name = "TargetCube"

null = bpy.data.objects.new("RigNull", None)
scene.collection.objects.link(null)

bpy.ops.mesh.primitive_cube_add(size=1, location=(1, 0, 1))
box = bpy.context.active_object
box.name = "ParentedBox"
box.parent = null  # pre-bootstrap parenting: baseline the replica gets free

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

cam = bpy.data.objects["Camera"]

session.start(prefs)
sync = session.state["sync"]

results = {"done": False}
state = {"step": 0, "settle_until": 0.0}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq, **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def mat(obj):
    bpy.context.view_layer.update()
    return [round(v, 6) for row in obj.matrix_world for v in row]


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
    pass  # gate alone does the work


def step_parent_camera():
    cam.parent = null  # mid-session parenting — the reported gap
    null.location = (2, -3, 4)
    null.rotation_euler = (0.2, 0.0, 0.5)


def step_add_constraint():
    c = cam.constraints.new("TRACK_TO")
    c.target = cube
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    c.influence = 0.75


def step_unparent_box():
    bpy.context.view_layer.update()
    world = box.matrix_world.copy()
    box.parent = None  # keep transform: local gets the old world
    box.matrix_world = world


def step_bake():
    pc = cloth.point_cache
    with bpy.context.temp_override(scene=scene, active_object=sheet,
                                   point_cache=pc):
        bpy.ops.ptcache.bake(bake=True)
    results["host_baked"] = pc.is_baked
    results["host_mean_z_f20"] = round(mean_z(sheet, 20), 6)


def step_resync():
    results["bake_note_seen"] = bool(sync.bake_note)  # sweep had ≥2 s
    session.force_resync()
    # Queued is not shipped: the note clears when the last chunk leaves
    # (SYNC-AUDIT §3, 2026-09-23). Judged in the next step, after settle.
    results["bake_note_cleared_at_queue"] = not sync.bake_note
    scene.frame_set(20)


def step_influence():
    results["bake_note_cleared"] = not sync.bake_note  # bootstrap drained by now
    cam.constraints[0].influence = 0.3


def obj_mean_z(obj):
    deps = bpy.context.evaluated_depsgraph_get()
    me = obj.evaluated_get(deps).to_mesh()
    return sum((obj.matrix_world @ v.co).z for v in me.vertices) / len(me.vertices)


def step_gn_add():
    mod = cube.modifiers.new("GN", "NODES")
    tree = bpy.data.node_groups.new("SmokeGN", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT",
                              socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT",
                              socket_type="NodeSocketGeometry")
    zoff = tree.interface.new_socket("ZOff", in_out="INPUT",
                                     socket_type="NodeSocketFloat")
    n_in = tree.nodes.new("NodeGroupInput")
    n_out = tree.nodes.new("NodeGroupOutput")
    n_set = tree.nodes.new("GeometryNodeSetPosition")
    n_comb = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(n_in.outputs[0], n_set.inputs["Geometry"])
    tree.links.new(n_in.outputs["ZOff"], n_comb.inputs["Z"])
    tree.links.new(n_comb.outputs[0], n_set.inputs["Offset"])
    tree.links.new(n_set.outputs[0], n_out.inputs[0])
    mod.node_group = tree
    results["zoff_ident"] = zoff.identifier
    mod.properties.inputs[zoff.identifier]["value"] = 1.5
    cube.update_tag()  # what a UI edit does; the raw idprop write doesn't
    results["host_gn_z1"] = round(obj_mean_z(cube), 6)


def step_gn_tweak():
    mod = cube.modifiers["GN"]
    mod.properties.inputs[results["zoff_ident"]]["value"] = 4.0
    cube.update_tag()
    results["host_gn_z2"] = round(obj_mean_z(cube), 6)


def step_free_bake():
    pc = cloth.point_cache
    with bpy.context.temp_override(scene=scene, active_object=sheet,
                                   point_cache=pc):
        bpy.ops.ptcache.free_bake()


def _rail_fcurve():
    act = bpy.data.objects["RailCircle"].data.animation_data.action
    if getattr(act, "fcurves", None):
        return act.fcurves[0]
    for layer in act.layers:  # 5.x layered actions
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fc in bag.fcurves:
                    return fc
    return None


def step_rail_add():
    curve = bpy.data.curves.new("RailCircle", "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(3)
    for i, co in enumerate([(4, 0, 2), (0, 4, 2), (-4, 0, 2), (0, -4, 2)]):
        p = spline.bezier_points[i]
        p.co = co
        p.handle_left_type = p.handle_right_type = "AUTO"
    spline.use_cyclic_u = True
    rail = bpy.data.objects.new("RailCircle", curve)
    scene.collection.objects.link(rail)
    curve.use_path = True
    curve.path_duration = 24
    curve.eval_time = 0.0
    curve.keyframe_insert("eval_time", frame=1)
    curve.eval_time = 24.0
    curve.keyframe_insert("eval_time", frame=24)
    railcam = bpy.data.objects.new("RailCam", bpy.data.cameras.new("RailCamD"))
    scene.collection.objects.link(railcam)
    con = railcam.constraints.new("FOLLOW_PATH")
    con.target = rail
    con.use_curve_follow = True
    results["rail_m1"] = mat(railcam)  # frame is 20 since step_resync


def step_rail_retime():
    fc = _rail_fcurve()
    fc.keyframe_points[1].co = (24.0, 12.0)  # half speed along the rail
    fc.update()
    results["rail_m2"] = mat(bpy.data.objects["RailCam"])


def step_ctrl_props():
    # No update_tag on purpose: raw idprop writes fire no depsgraph event —
    # this proves the sweep catches them. Dotted name proves json paths.
    null["CtrlVal"] = 0.25
    null["Cam.Mix"] = 0.5
    results["ctrl_v1"] = [0.25, 0.5]


def step_ctrl_tweak():
    null["CtrlVal"] = 0.85  # value-only change → tier-1 bracket-path write
    results["ctrl_v2"] = 0.85


def step_shapekeys():
    cube.shape_key_add(name="Basis")
    cube.shape_key_add(name="Smile")
    cube.data.shape_keys.key_blocks["Smile"].value = 0.6
    results["smile_v1"] = 0.6


def step_shapekeys2():
    cube.data.shape_keys.key_blocks["Smile"].value = 0.9
    results["smile_v2"] = 0.9
    results["host_sk_count"] = len(bpy.data.shape_keys)


def step_lattice():
    lat_data = bpy.data.lattices.new("LatRigD")
    lat = bpy.data.objects.new("LatRig", lat_data)
    scene.collection.objects.link(lat)
    lat_data.points[0].co_deform = (-0.75, -0.6, -0.55)
    results["lat_pt"] = [-0.75, -0.6, -0.55]


def step_armature():
    with bpy.context.temp_override(
        window=bpy.context.window_manager.windows[0]
    ):
        bpy.ops.object.armature_add(location=(3, 0, 0))
    rig = bpy.context.active_object
    rig.name = "PoseRig"
    pb = rig.pose.bones[0]
    pb.location = (0.0, 0.3, 0.2)
    pb.rotation_quaternion = (0.95, 0.2, 0.0, 0.1)
    con = pb.constraints.new("LIMIT_LOCATION")
    con.use_min_z = True
    con.min_z = 0.1
    con.influence = 0.5
    bpy.context.view_layer.update()
    results["arm_matrix"] = [round(v, 6) for row in pb.matrix for v in row]


def step_finalize():
    results["bake_note_seen_again"] = bool(sync.bake_note)
    results["cam_matrix"] = mat(cam)
    results["box_matrix"] = mat(box)
    results["cam_influence"] = round(cam.constraints[0].influence, 4)
    results["host_unbaked"] = not cloth.point_cache.is_baked
    results["done"] = True


STEPS = [
    step_connect, step_parent_camera, step_add_constraint, step_unparent_box,
    step_bake, step_resync, step_influence, step_free_bake,
    step_gn_add, step_gn_tweak, step_rail_add, step_rail_retime,
    step_ctrl_props, step_ctrl_tweak, step_shapekeys, step_shapekeys2,
    step_lattice, step_armature, step_finalize,
]
SETTLE = 3.0  # sweep (0.5 s) + debounce + tier-2 flight, with margin


def _driver():
    now = time.monotonic()
    dump()
    if state["step"] >= len(STEPS):
        return 0.5  # idle until the harness kills us
    if now < state["settle_until"] or not peer_caught_up():
        return 0.25
    STEPS[state["step"]]()
    state["step"] += 1
    state["settle_until"] = now + SETTLE
    dump()
    return 0.25


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
