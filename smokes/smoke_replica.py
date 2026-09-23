"""Smoke replica: start the session, dump observed scene state as JSON on a
persistent timer (survives the bootstrap's open_mainfile)."""
import json
import os
import sys
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))  # vendored pyzmq wheel

from qcbridge.ring0 import replica_apply, session  # noqa: E402

KIOSK = bool(os.environ.get("QCB_SMOKE_KIOSK"))
prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=300,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=KIOSK,
    path_mappings=[],
)

if KIOSK:
    # Reproduce the field pre-bootstrap state: no camera to look through
    # when the first hot packets (camera=True) arrive.
    for obj in [o for o in bpy.data.objects if o.type == "CAMERA"]:
        bpy.data.objects.remove(obj)

session.start(prefs)

_persp_log = []  # (t, view_perspective) transitions — catches view pops
_disturb = {"armed": bool(os.environ.get("QCB_SMOKE_DISTURB")), "at": 0.0}


def _mat(obj):
    return [round(v, 6) for row in obj.matrix_world for v in row]


def _dump():
    d = {"stats": {k: v for k, v in replica_apply.stats.items()
                   if k not in ("applying", "last_error")},
         "last_error": replica_apply.stats["last_error"]}
    try:
        bpy.context.view_layer.update()
        scene = bpy.context.scene
        d["frame"] = scene.frame_current
        objs = bpy.data.objects
        cam = objs.get("Camera")
        if cam is not None:
            d["cam_parent"] = cam.parent.name if cam.parent else None
            d["cam_matrix"] = _mat(cam)
            d["cam_constraints"] = [
                [c.type,
                 c.target.name if getattr(c, "target", None) else None,
                 round(c.influence, 4)]
                for c in cam.constraints
            ]
        box = objs.get("ParentedBox")
        if box is not None:
            d["box_parent"] = box.parent.name if box.parent else None
            d["box_matrix"] = _mat(box)
        tc = objs.get("TargetCube")
        if tc is not None:
            deps = bpy.context.evaluated_depsgraph_get()
            me = tc.evaluated_get(deps).to_mesh()
            d["cube_mean_z"] = round(
                sum((tc.matrix_world @ v.co).z for v in me.vertices)
                / len(me.vertices), 6)
            d["cube_has_gn"] = any(m.type == "NODES" for m in tc.modifiers)
        rc = objs.get("RailCam")
        if rc is not None:
            d["railcam_matrix"] = _mat(rc)
        null = objs.get("RigNull")
        if null is not None:
            d["ctrl_val"] = null.get("CtrlVal")
            d["cam_mix"] = null.get("Cam.Mix")
        d["sk_count"] = len(bpy.data.shape_keys)
        if tc is not None and tc.data.shape_keys is not None:
            smile = tc.data.shape_keys.key_blocks.get("Smile")
            if smile is not None:
                d["smile_value"] = round(smile.value, 4)
        lat = objs.get("LatRig")
        if lat is not None:
            d["lat_pt"] = [round(v, 4) for v in lat.data.points[0].co_deform]
        rig = objs.get("PoseRig")
        if rig is not None and rig.pose is not None:
            pb = rig.pose.bones[0]
            d["arm_matrix"] = [round(v, 6) for row in pb.matrix for v in row]
        sheet = objs.get("ClothSheet")
        if sheet is not None and sheet.modifiers.get("Cloth"):
            pc = sheet.modifiers["Cloth"].point_cache
            d["cloth_baked"] = pc.is_baked
            deps = bpy.context.evaluated_depsgraph_get()
            me = sheet.evaluated_get(deps).to_mesh()
            d["cloth_mean_z"] = round(
                sum((sheet.matrix_world @ v.co).z for v in me.vertices)
                / len(me.vertices), 6)
        for name in ("ShotCam", "NestCam"):
            o = objs.get(name)
            if o is not None:
                d[name.lower() + "_matrix"] = _mat(o)
        area = next(
            (a for w in bpy.context.window_manager.windows
             for a in w.screen.areas if a.type == "VIEW_3D"), None)
        if area is not None:
            rv3d = area.spaces.active.region_3d
            d["view_persp"] = rv3d.view_perspective
            d["view_zoom"] = round(rv3d.view_camera_zoom, 2)
            import time as _t
            now = _t.monotonic()
            if not _persp_log or _persp_log[-1][1] != rv3d.view_perspective:
                _persp_log.append((round(now, 1), rv3d.view_perspective))
            d["persp_log"] = _persp_log[-8:]
            # Simulated "user pressed Num0/orbited on the replica": knock
            # the view out once, 4 s after camera view first lands — the
            # re-assert loop must restore CAMERA within ~1 s untouched.
            if _disturb["armed"] and rv3d.view_perspective == "CAMERA":
                if _disturb["at"] == 0.0:
                    _disturb["at"] = now + 4.0
                elif now >= _disturb["at"]:
                    _disturb["armed"] = False
                    rv3d.view_perspective = "PERSP"
                    area.tag_redraw()
        d["shot"] = {"on": replica_apply._shot["on"],
                     "fit_zoom": replica_apply._shot["fit_zoom"]}
        cam = bpy.context.scene.camera
        if cam is not None:
            d["scenecam_name"] = cam.name
            d["scenecam_matrix"] = _mat(cam)
            d["scenecam_parent"] = cam.parent.name if cam.parent else None
        if area is not None and cam is not None:
            rv3d = area.spaces.active.region_3d
            expect = cam.matrix_world.normalized().inverted()
            d["cam_bound"] = (
                rv3d.view_perspective == "CAMERA"
                and all(abs(a - b) <= 1e-3
                        for r1, r2 in zip(rv3d.view_matrix, expect)
                        for a, b in zip(r1, r2))
            )
        d["n_objects"] = len(bpy.data.objects)
        d["n_cameras"] = len(bpy.data.cameras)
    except Exception as exc:  # mid-apply states are fine to skip
        d["dump_error"] = repr(exc)
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.4


bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
