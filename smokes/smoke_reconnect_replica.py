"""Reconnect smoke, replica half: dump stats and the probe cube's location."""
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
    pixel_source="readback", stream_bitrate_mbps=50, stream_width=2560, stream_fps=30, ffmpeg_path="", replica_kiosk=False,
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
    d["probe_mods"] = None if o is None else [m.name for m in o.modifiers]
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.3


if os.environ.get("QCB_SMOKE_LOCAL_EDIT"):
    # Edit the replica by hand (DESIGN-NOTES sync A9); the host must be told.
    # Timed from the fast re-assert the dropped frame provokes, not from
    # launch: the detector ignores a datablock the host touched in the last
    # 1.5 s, and the re-assert touches Probe — a fixed 12 s edit landed
    # inside that window on some runs.
    _edit_at = {"t": None}

    def _local_edit():
        import time as _t
        if _edit_at["t"] is None:
            # after BOTH repairs (the fast re-assert and the delta), so the
            # edit never lands inside a repair's quiet window
            if replica_apply.stats.get("resyncs", 0) >= 2 or replica_apply.stats["bootstraps"] >= 2:
                _edit_at["t"] = _t.monotonic() + 4.0
            return 0.5
        if _t.monotonic() < _edit_at["t"]:
            return 0.5
        o = bpy.data.objects.get("Probe")
        if o is not None:
            print(f"qcb smoke: local edit at t={_t.monotonic():.2f} (resyncs={replica_apply.stats.get('resyncs')})", flush=True)
            o.location.z = 9.0
            bpy.context.view_layer.update()
        return None
    bpy.app.timers.register(_local_edit, first_interval=1.0, persistent=True)

bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
