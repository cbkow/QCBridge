"""Reconnect smoke, replica half: dump stats and the probe cube's location."""
import json
import os
import sys
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
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
TAG = os.environ.get("QCB_SMOKE_TAG", "r")


def _dump():
    d = {"tag": TAG, "pid": os.getpid(),
         "stats": {k: v for k, v in replica_apply.stats.items() if k != "applying"},
         "note": session.state["note"], "overlay": session._replica_overlay_text()}
    o = bpy.data.objects.get("Probe")
    d["probe"] = None if o is None else [round(v, 3) for v in o.location]
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.3


if os.environ.get("QCB_SMOKE_LOCAL_EDIT"):
    # 12 s in: edit the replica by hand (SYNC-AUDIT A9). The host must be told.
    def _local_edit():
        o = bpy.data.objects.get("Probe")
        if o is not None:
            o.location.z = 9.0
            bpy.context.view_layer.update()
        return None
    bpy.app.timers.register(_local_edit, first_interval=12.0, persistent=True)

bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
