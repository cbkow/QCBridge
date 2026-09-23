"""Latency bench, replica half: sample the watched values on a fast timer and
record the wall-clock time each new value first appeared. Also records the
timer's own cadence, which bounds the measurement's resolution."""
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

from qcbridge.ring0 import replica_apply, session  # noqa: E402

prefs = SimpleNamespace(
    role="REPLICA", replica_address="", bind_address="127.0.0.1",
    port_control=19990, port_hot=19991, port_cold=19992, token="benchtok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)
session.start(prefs)

seen: dict = {"probe_x": [], "bench": [], "frame": [], "heavy_mods": [],
              "knob": []}
last: dict = {}
ticks: list = []
_state = {"last_dump": 0.0, "last_tick": 0.0}


def _watch():
    objs = bpy.data.objects
    probe = objs.get("Probe")
    heavy = objs.get("Heavy")
    vals = {"frame": bpy.context.scene.frame_current}
    if probe is not None:
        vals["probe_x"] = int(round(probe.location.x))
        m = probe.modifiers.get("Bench")
        vals["bench"] = int(round(m.strength)) if m is not None else None
        vals["knob"] = probe.get("Knob")
    if heavy is not None:
        vals["heavy_mods"] = len(heavy.modifiers)
    return vals


def _tick():
    now = time.time()
    if _state["last_tick"]:
        ticks.append(now - _state["last_tick"])
    _state["last_tick"] = now
    try:
        vals = _watch()
    except Exception:  # mid-apply states are fine to skip
        return 0.002
    for key, value in vals.items():
        if last.get(key) != value:
            last[key] = value
            seen[key].append([value, now])
    if now - _state["last_dump"] > 0.5:
        _state["last_dump"] = now
        d = {"seen": seen,
             "tick_ms_median": sorted(ticks)[len(ticks) // 2] * 1000 if ticks else None,
             "tick_ms_p95": sorted(ticks)[int(len(ticks) * 0.95)] * 1000 if ticks else None,
             "stats": {k: v for k, v in replica_apply.stats.items()
                       if k not in ("applying",)}}
        tmp = os.path.join(OUT, "replica_lat.json.tmp")
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, os.path.join(OUT, "replica_lat.json"))
        if len(ticks) > 20000:
            del ticks[:10000]
    return 0.002


bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)
