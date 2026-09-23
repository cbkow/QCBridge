"""Latency bench, host half: make timestamped edits and record when each was
made. The replica half records when each value became visible; the report
joins the two on the value. Same-machine clocks, so time.time() is shared.

Phases (each value is an integer the replica can match exactly):
  t1    Probe.location.x = k           tier-1 property delta
  t2    modifier "Bench" strength = k  structural change → tier-2 blob
  hot   scene.frame_set(100 + k)       hot lane
  hol0  Heavy modifier toggled, then Probe.location.x = 100 + k in the same
        flush — does a large tier-2 blob delay a small tier-1 delta?
  hol150 same, with the Probe edit 150 ms after the Heavy toggle
  sweep  Probe["Knob"] = k              custom prop: seen only by the 0.5 s sweep
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

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="benchtok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

HEAVY = int(os.environ.get("QCB_BENCH_HEAVY", "800"))  # grid subdivisions
N_T1 = int(os.environ.get("QCB_BENCH_N", "20"))

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 250
probe = bpy.data.objects["Cube"]
probe.name = "Probe"
bpy.ops.mesh.primitive_grid_add(x_subdivisions=HEAVY, y_subdivisions=HEAVY,
                                size=4, location=(0, 0, -2))
heavy = bpy.context.active_object
heavy.name = "Heavy"

session.start(prefs)
sync = session.state["sync"]

sent: dict = {"t1": {}, "t2": {}, "hot": {}, "hol0": {}, "hol150": {},
              "sweep": {}, "heavy": {}}
results = {"done": False, "heavy_verts": len(heavy.data.vertices),
           "transport": os.environ.get("QCB_TRANSPORT", "zmq")}
state = {"phase": 0, "k": 0, "next": 0.0, "pending": None}


def dump():
    tmp = os.path.join(OUT, "host_lat.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"sent": sent, **results}, f)
    _atomic_replace(tmp, os.path.join(OUT, "host_lat.json"))


def peer_caught_up():
    transport = session.state["transport"]
    ps = getattr(transport, "peer_status", {}) or {}
    return session.state["note"] == "connected" and sync.caught_up(ps)


def edit_probe(value):
    probe.location.x = float(value)
    bpy.context.view_layer.update()  # what a UI edit does: evaluate now
    return time.time()


def toggle_heavy():
    if heavy.modifiers:
        heavy.modifiers.remove(heavy.modifiers[0])
    else:
        heavy.modifiers.new("Blk", "DISPLACE")
    bpy.context.view_layer.update()
    return time.time()


def do_t1(k):
    sent["t1"][k] = edit_probe(k)


def do_t2(k):
    if "Bench" in probe.modifiers:
        probe.modifiers.remove(probe.modifiers["Bench"])
    m = probe.modifiers.new("Bench", "DISPLACE")
    m.strength = float(k)
    bpy.context.view_layer.update()
    sent["t2"][k] = time.time()


def do_hot(k):
    scene.frame_set(100 + k)
    sent["hot"][k] = time.time()


def do_hol0(k):
    sent["heavy"][f"hol0-{k}"] = toggle_heavy()
    sent["hol0"][k] = edit_probe(100 + k)


def do_hol150(k):
    sent["heavy"][f"hol150-{k}"] = toggle_heavy()
    state["pending"] = (time.monotonic() + 0.15, lambda: sent["hol150"].__setitem__(k, edit_probe(200 + k)))


def do_sweep(k):
    probe["Knob"] = k  # no depsgraph event: only the sweep notices
    sent["sweep"][k] = time.time()


# (name, fn, trials, spacing seconds)
PHASES = [
    ("t1", do_t1, N_T1, 0.5),
    ("t2", do_t2, 10, 1.5),
    ("hot", do_hot, N_T1, 0.5),
    ("sweep", do_sweep, N_T1, 1.0),
    ("hol0", do_hol0, 8, 4.0),
    ("hol150", do_hol150, 8, 4.0),
]


def _driver():
    now = time.monotonic()
    if state["pending"] is not None:
        at, fn = state["pending"]
        if now >= at:
            fn()
            state["pending"] = None
        return 0.01
    if state["phase"] >= len(PHASES):
        if not results["done"]:
            results["done"] = True
            dump()
        return 0.5
    if state["phase"] == 0 and state["k"] == 0 and not peer_caught_up():
        return 0.25
    if now < state["next"]:
        return 0.01
    name, fn, trials, spacing = PHASES[state["phase"]]
    if state["k"] == 0:
        results.setdefault("phase_start", {})[name] = time.time()
    state["k"] += 1
    fn(state["k"])
    state["next"] = now + spacing
    if state["k"] >= trials:
        state["phase"] += 1
        state["k"] = 0
        state["next"] = now + 3.0  # let the previous phase drain
    dump()
    return 0.01


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
