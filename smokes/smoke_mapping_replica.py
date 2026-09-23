"""Mapping smoke, replica half: our table says `Z:\\proj` is a different
local directory (a symlink to the host's, so the files are shared but the
path is not). Dump every image's resolved path and whether it loads."""
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

REPLICA_ROOT = os.path.join(OUT, "replicaside", "proj")
mapping = SimpleNamespace(win="Z:\\proj", mac=REPLICA_ROOT, enabled=True, label="proj")
prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[mapping],
)
session.start(prefs)


def _dump():
    d = {"stats": {k: v for k, v in replica_apply.stats.items() if k != "applying"},
         "replica_root": REPLICA_ROOT, "images": {}}
    try:
        for name in ("RelTex", "AbsTex", "StrayTex"):
            img = bpy.data.images.get(name)
            if img is None:
                continue
            ok = False
            try:
                ok = bool(img.has_data) or (img.size[0] > 0)
            except Exception:
                pass
            d["images"][name] = {"filepath": img.filepath, "loads": ok, "size": list(img.size)}
    except Exception as exc:
        d["dump_error"] = repr(exc)
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    _atomic_replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.4


bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
