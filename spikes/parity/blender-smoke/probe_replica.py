"""Probe smoke replica: strip on (QCB_PROBE=1 from the launcher).

Env: QCB_SMOKE_BIND (127.0.0.1), QCB_SMOKE_TOKEN (smoketok), QCB_SMOKE_STREAM
(1 = addon's own SRT stream, 0 = none, e.g. when an external kyber pipeline
captures), QCB_SMOKE_SRT_LATENCY (60), QCB_SMOKE_RUNG (hevc_10_420_50),
QCB_SMOKE_FFMPEG, QCB_SMOKE_KIOSK (0), QCB_SMOKE_CYCLES_DEVICE (OPTIX/CUDA/METAL). The replica viewport is always
Rendered shading (kiosk.prepare_viewport); the engine comes from the host.
Ports 19990-19992 control/hot/cold, 19998 SRT."""
import json, os, sys, time  # noqa: E401
from types import SimpleNamespace
import bpy

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT = sys.argv[sys.argv.index("--") + 1]  # work dir: pysite/ (unzipped pyzmq wheel) + json dumps
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(OUT, "pysite"))
from qcbridge.ring0 import pixel_path, replica_apply, session  # noqa: E402
from qcbridge.ring1 import protocol  # noqa: E402

E = os.environ.get
prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address=E("QCB_SMOKE_BIND", "127.0.0.1"),
    port_control=19990, port_hot=19991, port_cold=19992, token=E("QCB_SMOKE_TOKEN", "smoketok"),
    enable_stream=E("QCB_SMOKE_STREAM", "1") == "1", srt_port=19998, srt_url="",
    srt_latency_ms=int(E("QCB_SMOKE_SRT_LATENCY", "60")),
    encoder_rung=E("QCB_SMOKE_RUNG", "hevc_10_420_50"), ffmpeg_path=E("QCB_SMOKE_FFMPEG", ""),
    replica_kiosk=E("QCB_SMOKE_KIOSK", "0") == "1",
    path_mappings=[],
)
# Factory startup = Cycles on CPU. QCB_SMOKE_CYCLES_DEVICE=OPTIX|CUDA|METAL
# enables the GPU backend here; the host sets scene.cycles.device = GPU.
if E("QCB_SMOKE_CYCLES_DEVICE"):
    cprefs = bpy.context.preferences.addons["cycles"].preferences
    cprefs.compute_device_type = E("QCB_SMOKE_CYCLES_DEVICE")
    cprefs.refresh_devices()
    for dev in cprefs.devices:
        dev.use = dev.type == E("QCB_SMOKE_CYCLES_DEVICE")
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
         "hot_seq": (lambda h: protocol.unpack_hot(h).probe_seq if h else None)(
             session.state["transport"].poll_hot()),
         "transport_stats": getattr(session.state["transport"], "stats", None),
         "ffmpeg_note": session.state.get("ffmpeg_note", "")}
    with open(os.path.join(OUT, "replica.json"), "w") as f:
        json.dump(d, f)
    return float(E("QCB_SMOKE_DUMP", "1.0"))

bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
