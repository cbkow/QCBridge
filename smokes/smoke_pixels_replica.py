"""Pixel smoke, replica half: the readback pixel path ON (frame ring +
encode helper + SRT mux). Dumps stats and the readback status so the
runner can attach to the ring and probe the stream."""
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

from qcbridge.ring0 import engine_qcb, pixel_path, replica_apply, session  # noqa: E402

prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=True, srt_port=19998, srt_url="", srt_latency_ms=120,
    pixel_source=os.environ.get("QCB_SMOKE_PIXEL_SOURCE", "readback"),
    stream_bitrate_mbps=20, stream_width=int(os.environ.get("QCB_SMOKE_STREAM_WIDTH", "1280")),
    stream_fps=30, ffmpeg_path="", replica_kiosk=False, path_mappings=[],
)
session.start(prefs)


def _dump():
    d = {"pid": os.getpid(),
         "stats": {k: v for k, v in replica_apply.stats.items() if k != "applying"},
         "note": session.state["note"],
         "readback": engine_qcb.status(),
         "pixel": pixel_path.status(),
         "ffmpeg": session.state.get("ffmpeg_note", ""),
         "engine": bpy.context.scene.render.engine}
    try:
        area = replica_apply._target_view()
        if area is not None:
            d["view_persp"] = area.spaces.active.region_3d.view_perspective
            region = next(r for r in area.regions if r.type == "WINDOW")
            d["region"] = [region.width, region.height]
    except Exception as exc:
        d["view_err"] = repr(exc)
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.3


bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
