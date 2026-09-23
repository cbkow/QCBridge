"""Reconnect smoke, host half: connect, then keep editing while the harness
kills and restarts the replica. Records every note transition and how many
bootstraps were sent, so the runner can see the re-handshake happen. After
the second bootstrap it makes two tier-1 edits a second apart — the second
one is what reveals a dropped first one (QCB_TEST_DROP_FIRST_T1 on the
replica) and drives the want_resync → auto-bootstrap path."""
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

prefs = SimpleNamespace(
    role="HOST", replica_address="127.0.0.1", bind_address="0.0.0.0",
    port_control=19990, port_hot=19991, port_cold=19992, token="smoketok",
    enable_stream=False, srt_port=19998, srt_url="", srt_latency_ms=120,
    encoder_rung="hevc_10_420_100", ffmpeg_path="", replica_kiosk=False,
    path_mappings=[],
)

cube = bpy.data.objects["Cube"]
cube.name = "Probe"
session.start(prefs)
sync = session.state["sync"]

results = {"notes": [], "edits": [], "done": False}
state = {"last_note": None, "phase": "wait_second_boot", "at": 0.0}


def dump():
    tmp = os.path.join(OUT, "host.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"seq": sync.seq, "sent_boot": sync.sent_boot,
                   "auto_resyncs": session.state.get("auto_resyncs", 0),
                   "peer_status": getattr(session.state["transport"], "peer_status", {}),
                   "note": session.state["note"], **results}, f, indent=1)
    os.replace(tmp, os.path.join(OUT, "host.json"))


def _driver():
    now = time.monotonic()
    note = session.state["note"]
    if note != state["last_note"]:
        results["notes"].append([round(now, 2), note])
        state["last_note"] = note
    dump()
    if state["phase"] == "wait_second_boot":
        if sync.sent_boot >= 2 and not sync._boot_outbox and note == "connected":
            state["phase"] = "edit_a"
            state["at"] = now + 3.0
    elif state["phase"] == "edit_a" and now >= state["at"]:
        cube.location.x = 7.0
        results["edits"].append(["x", 7.0, time.time()])
        state["phase"] = "edit_b"
        state["at"] = now + 1.5
    elif state["phase"] == "edit_b" and now >= state["at"]:
        cube.location.y = 3.0
        results["edits"].append(["y", 3.0, time.time()])
        state["phase"] = "done"
        results["done"] = True
        dump()
    return 0.25


bpy.app.timers.register(_driver, first_interval=1.0, persistent=True)
