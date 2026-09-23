"""Coverage replica: run every probe continuously and dump the observations."""
import json
import os
import sys
import time
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from smokes._atomic import replace as _atomic_replace  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

import catalog  # noqa: E402
from qcbridge.ring0 import replica_apply, session  # noqa: E402

prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="covtok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)
session.start(prefs)

history: dict = {}  # key -> [[value, t_first_seen], ...] on change only
blobs: list = []     # [[t, applied_t2, applied_t1]] whenever a count moves — the cost column


def _dump():
    now = time.time()
    for key, group, desc, act, probe in catalog.ACTIONS:
        try:
            v = probe()
        except Exception as exc:
            v = "ERR " + repr(exc)[:80]
        hist = history.setdefault(key, [])
        canon = json.dumps(v, sort_keys=True, default=str)
        if not hist or hist[-1][0] != canon:
            hist.append([canon, now])
    st = replica_apply.stats
    if not blobs or blobs[-1][1] != st["applied_t2"] or blobs[-1][2] != st["applied_t1"]:
        blobs.append([now, st["applied_t2"], st["applied_t1"]])
    d = {"history": history, "blobs": blobs,
         "stats": {k: v for k, v in st.items() if k != "applying"}}
    tmp = os.path.join(OUT, "replica.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f)
    _atomic_replace(tmp, os.path.join(OUT, "replica.json"))
    return 0.25


bpy.app.timers.register(_dump, first_interval=1.0, persistent=True)
