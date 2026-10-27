"""Colour harness, host half (smokes/README.md, harness_colour): the pixel
smoke's host with the STILL harness scene (harness_colour_scene.py) so the
same scene can be rendered to a reference and streamed, then compared at
every stage. Dumps what the smoke host dumps, plus the colour blocks.
Environment: QCB_HARNESS_DISPLAY / _VIEW / _LOOK, QCB_SMOKE_ENGINE,
QCB_HARNESS_REPLICA (address; default 127.0.0.1).
"""
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

ENGINE = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")

prefs = SimpleNamespace(
    role="HOST", replica_address=os.environ.get("QCB_HARNESS_REPLICA", "127.0.0.1"), bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=True, srt_port=19998, srt_url="", srt_latency_ms=120,
    pixel_source="readback", stream_bitrate_mbps=50, stream_width=1280, stream_fps=30,
    ffmpeg_path="", replica_kiosk=False, path_mappings=[],
)


from smokes.harness_colour_scene import build_scene  # noqa: E402

if True:  # Blender runs this file as the script; the scene lives in harness_colour_scene
    scene = bpy.context.scene
    build_scene(scene)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"

    session.start(prefs)
    sync = session.state["sync"]
    results = {"done": False, "shot": False}
    state = {"t0": time.monotonic()}

    def dump():
        tmp = os.path.join(OUT, "host.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"seq": sync.seq, "note": session.state["note"],
                       "viewer_url": session.viewer_url(), "deep_link": session.qcview_deep_link(),
                       "peer_stream": session.state.get("peer_stream"),
                       "peer_status": getattr(session.state["transport"], "peer_status", {}),
                       "cm_host": session.state.get("cm"), "cm_replica": session.state.get("peer_cm"),
                       "cm_mismatches": session.state.get("cm_mismatches"),
                       "display": scene.display_settings.display_device,
                       "view": scene.view_settings.view_transform,
                       "look": scene.view_settings.look,
                       **results}, f, indent=1)
        _atomic_replace(tmp, os.path.join(OUT, "host.json"))

    def _driver():
        now = time.monotonic()
        dump()
        if session.state["note"] != "connected":
            return 0.25
        if not results["shot"] and now - state["t0"] > 4.0:
            session.shot_mode_toggle()
            results["shot"] = True
        return 0.5

    bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
