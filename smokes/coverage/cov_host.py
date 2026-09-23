"""Coverage host: run the catalogue one action at a time, recording what
each should leave behind. Steps advance when the replica's pong seq has
caught up and a settle window has passed, like the smokes."""
import json
import os
import sys
import time
import traceback
from types import SimpleNamespace

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.join(OUT, "pysite"))

import catalog  # noqa: E402
from qcbridge.ring0 import session  # noqa: E402

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="covtok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

only = os.environ.get("QCB_COV_ONLY", "")
ACTIONS = [a for a in catalog.ACTIONS if not only or a[0] in only.split(",")]
SETTLE = float(os.environ.get("QCB_COV_SETTLE", "2.5"))

catalog.setup()
bpy.context.view_layer.update()

session.start(prefs)
sync = session.state["sync"]

results = {"done": False, "expected": {}, "t_done": {}, "errors": {},
           "groups": {k: g for k, g, *_ in ACTIONS}, "desc": {k: d for k, _, d, *_ in ACTIONS},
           "order": [k for k, *_ in ACTIONS]}
state = {"step": 0, "settle_until": 0.0, "started": time.monotonic()}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"step": state["step"], "seq": sync.seq, **results}, f)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and ps.get("seq", -1) >= sync.seq


def _driver():
    now = time.monotonic()
    dump()
    if state["step"] >= len(ACTIONS):
        if not results["done"]:
            results["done"] = True
            dump()
        return 0.5
    if now < state["settle_until"] or not peer_caught_up():
        if now - state["started"] > 60 and state["step"] == 0 and not peer_caught_up():
            results["errors"]["_connect"] = "never caught up"
        return 0.2
    key, group, desc, act, probe = ACTIONS[state["step"]]
    try:
        act()
        bpy.context.view_layer.update()
    except Exception:
        results["errors"][key] = traceback.format_exc().strip().splitlines()[-1]
    try:
        results["expected"][key] = probe()
    except Exception:
        results["errors"][key] = "probe: " + traceback.format_exc().strip().splitlines()[-1]
        results["expected"][key] = None
    results["t_done"][key] = time.time()
    state["step"] += 1
    state["settle_until"] = now + SETTLE
    dump()
    return 0.2


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
