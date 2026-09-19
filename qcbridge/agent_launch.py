"""Entry point for a Blender launched by the QCBridge Agent (replica role).

The agent runs Blender with `--python-expr` that imports this module and
calls run(): fill the addon preferences from the agent's environment, start
the replica session (which attaches to the agent's local socket), and poll
for the agent's quit request so the machine returns to idle without anyone
touching the keyboard. Nothing here reads scene data.
"""

from __future__ import annotations

import os

import bpy


def _prefs():
    for name, addon in bpy.context.preferences.addons.items():
        if name.endswith(".qcbridge") or name == "qcbridge":
            return addon.preferences
    return None


def _dev_prefs():
    """Repo checkout on sys.path, extension not installed: a prefs-shaped
    object is enough for session.start (the smoke harness does the same)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        role="REPLICA", replica_address="", bind_address="0.0.0.0",
        port_control=19990, port_hot=19991, port_cold=19992,
        token=os.environ.get("QCB_AGENT_TOKEN", ""),
        enable_stream=os.environ.get("QCB_AGENT_STREAM", "1") == "1",
        srt_port=9998, srt_url="", srt_latency_ms=60,
        encoder_rung="hevc_10_420_50", ffmpeg_path="",
        replica_kiosk=os.environ.get("QCB_AGENT_KIOSK", "1") == "1",
        path_mappings=[], transport="kyber",
    )


def run() -> None:
    os.environ["QCB_TRANSPORT"] = "kyber"
    prefs = _prefs()
    if prefs is not None:
        prefs.role = "REPLICA"
        if os.environ.get("QCB_AGENT_TOKEN"):
            prefs.token = os.environ["QCB_AGENT_TOKEN"]
        prefs.replica_kiosk = os.environ.get("QCB_AGENT_KIOSK", "1") == "1"
        if hasattr(prefs, "transport"):
            prefs.transport = "KYBER"

    def _start():
        if prefs is not None:
            bpy.ops.qcbridge.session_start()
        else:
            from .ring0 import session

            print("qcbridge agent_launch: dev mode (extension not installed)", flush=True)
            session.start(_dev_prefs())
        bpy.app.timers.register(_watch_quit, first_interval=1.0, persistent=True)
        return None

    # Let the window and extension finish loading before starting the session.
    bpy.app.timers.register(_start, first_interval=1.0, persistent=True)


def _watch_quit():
    from .ring0 import session

    transport = session.state.get("transport")
    if getattr(transport, "quit_requested", False):
        print("qcbridge agent_launch: agent asked us to quit", flush=True)
        session.stop()
        bpy.ops.wm.quit_blender()
        return None
    return 1.0
