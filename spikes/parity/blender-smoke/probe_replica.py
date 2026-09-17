"""Probe smoke replica: stream on, strip on (QCB_PROBE=1 from the launcher)."""
import json, os, sys, time  # noqa: E401
from types import SimpleNamespace
import bpy

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT = sys.argv[sys.argv.index("--") + 1]  # work dir: pysite/ (unzipped pyzmq wheel) + json dumps
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(OUT, "pysite"))
from qcbridge.ring0 import pixel_path, replica_apply, session  # noqa: E402

prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=True, srt_port=19998, srt_url="", srt_latency_ms=60,
    encoder_rung="hevc_10_420_50", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)
session.start(prefs)

def _dump():
    region = None
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == "VIEW_3D":
                for r in a.regions:
                    if r.type == "WINDOW":
                        region = [r.x, r.y, r.width, r.height, w.x, w.y, w.width, w.height]
    d = {"t": time.time(), "pixel": pixel_path.status(),
         "stats": {k: v for k, v in replica_apply.stats.items()},
         "region_xywh_window_xywh": region,
         "session_note": session.state.get("note"),
         "ffmpeg_note": session.state.get("ffmpeg_note", "")}
    with open(os.path.join(OUT, "replica.json"), "w") as f:
        json.dump(d, f)
    return 1.0

bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
