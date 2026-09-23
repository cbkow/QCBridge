"""Cache smoke, replica half: dump the cloth's cache state and its mean z at
whatever frame the hot lane put us on."""
import json
import os
import sys
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from smokes._atomic import replace as _atomic_replace  # noqa: E402
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

from qcbridge.ring0 import replica_apply, session  # noqa: E402

prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)
session.start(prefs)


def _dump():
    d = {"stats": {k: v for k, v in replica_apply.stats.items() if k != "applying"}}
    try:
        scene = bpy.context.scene
        d["frame"] = scene.frame_current
        sheet = bpy.data.objects.get("ClothSheet")
        if sheet is not None and sheet.modifiers:
            pc = sheet.modifiers[0].point_cache
            d["cloth_baked"] = pc.is_baked
            d["cloth_external"] = pc.use_external
            d["cloth_filepath"] = pc.filepath
            d["cloth_info"] = pc.info
            deps = bpy.context.evaluated_depsgraph_get()
            me = sheet.evaluated_get(deps).to_mesh()
            d["mean_z"] = round(sum((sheet.matrix_world @ v.co).z for v in me.vertices) / len(me.vertices), 6)
    except Exception as exc:
        d["dump_error"] = repr(exc)
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.4


bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
