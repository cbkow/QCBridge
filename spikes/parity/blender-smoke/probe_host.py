"""Probe smoke host: default scene, stamped hot packets, constant orbit."""
import json, os, sys, time  # noqa: E401
from types import SimpleNamespace
import bpy

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT = sys.argv[sys.argv.index("--") + 1]  # work dir: pysite/ (unzipped pyzmq wheel) + json dumps
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(OUT, "pysite"))
from qcbridge.ring0 import session  # noqa: E402

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=True, srt_port=19998, srt_url="", srt_latency_ms=60,
    encoder_rung="hevc_10_420_50", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)
session.start(prefs)

def _dump():
    d = {"t": time.time(), "note": session.state.get("note"),
         "peer": session.state.get("peer_status"),
         "sync_seq": getattr(session.state.get("sync"), "seq", None)}
    with open(os.path.join(OUT, "host.json"), "w") as f:
        json.dump(d, f, default=str)
    return 1.0

bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
