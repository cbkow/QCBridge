"""Mapping smoke, host half. The host's project lives under one root; its
mapping table says that root is `Z:\\proj` on the wire. The project holds a
relative image (//tex.png), an absolute image under the same root, and an
absolute image nowhere near any mapping — the replica must resolve the
first two through its own table and count the third as unmapped."""
import json
import os
import sys
import time
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

from qcbridge.ring0 import session  # noqa: E402

HOST_ROOT = os.path.join(OUT, "hostside", "proj")
os.makedirs(HOST_ROOT, exist_ok=True)
STRAY = os.path.join(OUT, "stray")
os.makedirs(STRAY, exist_ok=True)


def make_png(path, color):
    img = bpy.data.images.new("tmp", 8, 8)
    img.generated_color = color
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


make_png(os.path.join(HOST_ROOT, "tex.png"), (1, 0, 0, 1))
make_png(os.path.join(HOST_ROOT, "abs.png"), (0, 1, 0, 1))
make_png(os.path.join(STRAY, "stray.png"), (0, 0, 1, 1))

mapping = SimpleNamespace(win="Z:\\proj", mac=HOST_ROOT, enabled=True, label="proj")
prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[mapping],
)

# Save first so '//' means something, then load the three images.
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(HOST_ROOT, "scene.blend"))
rel = bpy.data.images.load(os.path.join(HOST_ROOT, "tex.png")); rel.name = "RelTex"
rel.filepath = "//tex.png"
absimg = bpy.data.images.load(os.path.join(HOST_ROOT, "abs.png")); absimg.name = "AbsTex"
stray = bpy.data.images.load(os.path.join(STRAY, "stray.png")); stray.name = "StrayTex"
for img in (rel, absimg, stray):
    img.use_fake_user = True  # keep them in the file without a material
# Same machine: the stray file would be reachable by accident. Its pixels
# are loaded; the path must be one the replica cannot resolve.
stray.pixels[0]  # force load
os.remove(os.path.join(STRAY, "stray.png"))

session.start(prefs)
sync = session.state["sync"]
results = {"done": False, "host_root": HOST_ROOT}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"seq": sync.seq, "note": session.state["note"],
                   "peer_status": getattr(session.state["transport"], "peer_status", {}),
                   **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def _driver():
    dump()
    ps = getattr(session.state["transport"], "peer_status", {}) or {}
    if session.state["note"] == "connected" and sync.caught_up(ps) and not results["done"]:
        results["done"] = True
        dump()
    return 0.3


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
