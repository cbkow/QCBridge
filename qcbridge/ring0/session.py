"""Session lifecycle for both roles.

Start/stop is always explicit (operators in prefs.py). The control-channel
request handler runs on the transport IO thread and must never touch bpy —
it works purely on strings captured at session start.
"""

from __future__ import annotations

import uuid as _uuid

import bpy

from ..ring1 import pathmap, protocol, toolbox
from ..ring1.transport import TransportConfig
from ..ring1.transport_zmq import HostTransportZmq, ReplicaTransportZmq
from . import host_handlers, host_hot, kiosk, overlay, pixel_path, replica_apply

_HANDSHAKE_TIMEOUT = 3.0
_HANDSHAKE_RETRY = 2.0


def _emergency_cleanup() -> None:
    """Blender quitting with a session running: kill the encoder child and
    close sockets so quit never hangs or orphans ffmpeg. No bpy in here —
    interpreter teardown territory."""
    try:
        pixel_path.stop()
    except Exception:
        pass
    transport = state.get("transport")
    if transport is not None:
        try:
            transport.stop()
        except Exception:
            pass


import atexit  # noqa: E402

atexit.register(_emergency_cleanup)

state = {
    "role": None,          # "HOST" | "REPLICA" | None
    "transport": None,
    "epoch": None,         # our session epoch
    "peer_epoch": None,
    "paused": False,
    "note": "",
    "sync": None,          # HostSync when role == HOST
    "prefs": None,         # live AddonPreferences for the running session
    "ffmpeg_note": "",     # which resolution rung provided ffmpeg (replica)
    "peer_stream": {},     # host: the replica's stream descriptor from hello
    "shot_mode": False,    # host: replica holds the fitted camera frame
}


def running() -> bool:
    return state["role"] is not None


def start(prefs) -> str:
    if running():
        return "already running"
    state["epoch"] = _uuid.uuid4().hex
    state["paused"] = False
    state["peer_epoch"] = None
    state["prefs"] = prefs
    if prefs.role == "HOST":
        _start_host(prefs)
    else:
        _start_replica(prefs)
    state["role"] = prefs.role
    try:
        # A session starting means this config works — snapshot it to the
        # survives-reinstall settings file (prefs.py owns the format).
        from .. import prefs as prefs_module

        prefs_module.save_settings(prefs)
    except Exception:
        pass
    return "ok"


def stop() -> None:
    transport = state["transport"]
    if state["role"] == "HOST" and transport is not None:
        # Clean goodbye so the replica idles its GPU — fire-and-forget (the
        # IO thread flushes it during shutdown); a dead replica must not
        # make Stop wait.
        transport.request_nowait({"kind": "goodbye"})
    host_hot.stop()
    host_handlers.stop()
    replica_apply.stop()
    overlay.disable()
    if state["role"] == "REPLICA":
        kiosk.exit_now()  # leave the machine with a usable UI
    else:
        kiosk.cancel()
    pixel_path.stop()
    if transport is not None:
        transport.stop()
    state.update(
        role=None, transport=None, peer_epoch=None, note="", sync=None,
        prefs=None, ffmpeg_note="", peer_stream={}, shot_mode=False,
    )


def _check_address(address: str) -> str:
    """zmq retries unresolvable hostnames silently forever — surface a bad
    replica address (e.g. a typo'd IP) at session start instead."""
    import socket

    try:
        socket.getaddrinfo(address, None)
        return ""
    except OSError:
        return f"replica address does not resolve: {address!r} — check preferences"


def _start_host(prefs) -> None:
    address_error = _check_address(prefs.replica_address or "127.0.0.1")
    cfg = TransportConfig(
        address=prefs.replica_address or "127.0.0.1",
        port_control=prefs.port_control,
        port_hot=prefs.port_hot,
        port_cold=prefs.port_cold,
    )
    transport = HostTransportZmq(cfg)
    transport.start()
    state["transport"] = transport
    state["note"] = address_error or "connecting"

    token = prefs.token
    hello = protocol.make_hello(token, state["epoch"], bpy.app.version_string)

    # Fire-and-poll — this timer runs on Blender's main thread, and a dead
    # peer must never freeze the UI (it did: a blocking 3 s request per
    # retry bogged Blender down whenever the replica was unreachable).
    pending = {"req": None, "sent_at": 0.0}

    def _handshake():
        import time as _time

        if state["transport"] is not transport:
            return None  # session stopped/replaced
        now = _time.monotonic()
        if pending["req"] is None:
            pending["req"] = transport.request_nowait(hello)
            pending["sent_at"] = now
            return 0.25
        reply = transport.poll_reply(pending["req"])
        if reply is not None:
            if reply.get("ok"):
                state["peer_epoch"] = reply.get("epoch")
                state["peer_stream"] = reply.get("stream") or {}
                state["note"] = "connected"
                # Fresh handshake = fresh epoch pairing → full bootstrap
                # (decision #16; same-epoch transport blips reconnect
                # without re-handshaking and just keep flowing).
                state["sync"].send_bootstrap()
                # Re-assert host-owned replica state a fresh session lost.
                transport.request_nowait(
                    {"kind": "shot", "on": state["shot_mode"]}
                )
                return None
            state["note"] = f"denied: {reply.get('reason', '')}"
            pending["req"] = None
            return _HANDSHAKE_RETRY  # denials can be fixed live
        if now - pending["sent_at"] > _HANDSHAKE_TIMEOUT:
            transport.cancel_request(pending["req"])
            pending["req"] = None
            state["note"] = address_error or "replica unreachable — retrying"
            return _HANDSHAKE_RETRY
        return 0.25

    bpy.app.timers.register(_handshake, first_interval=0.1, persistent=True)
    host_hot.start(transport)
    state["sync"] = host_handlers.start(
        transport,
        paused_fn=lambda: state["paused"],
        mappings=_prefs_mappings(prefs),
    )


def _start_replica(prefs) -> None:
    cfg = TransportConfig(
        address=prefs.bind_address or "0.0.0.0",
        port_control=prefs.port_control,
        port_hot=prefs.port_hot,
        port_cold=prefs.port_cold,
    )
    transport = ReplicaTransportZmq(cfg)
    token = prefs.token
    epoch = state["epoch"]

    # Captured now (main thread) so the IO-thread handler touches no bpy.
    # No passphrase here: both ends derive it from the shared token, so the
    # secret never crosses the wire.
    stream_info = {
        "enabled": bool(getattr(prefs, "enable_stream", True)),
        "port": prefs.srt_port,
        "latency_ms": getattr(prefs, "srt_latency_ms", 300),
    }

    def _handler(msg: dict) -> dict:  # IO thread: strings only, no bpy
        if msg.get("kind") == "hello":
            ok, reason = protocol.check_hello(msg, token)
            if ok:
                if state["peer_epoch"] != msg.get("epoch"):
                    replica_apply.notify_new_session()
                state["peer_epoch"] = msg.get("epoch")
                state["note"] = "host connected"
            return {
                "kind": "hello_reply", "ok": ok, "reason": reason,
                "epoch": epoch, "stream": stream_info,
            }
        if msg.get("kind") == "goodbye":
            replica_apply.notify_host_goodbye()  # flag only; handled on tick
            state["note"] = "host ended session"
            return {"kind": "goodbye_ack"}
        if msg.get("kind") == "shot":
            replica_apply.set_shot_mode(bool(msg.get("on")))
            return {"kind": "shot_ack"}
        if msg.get("kind") == "zoom":
            replica_apply.nudge_zoom(
                float(msg.get("delta", 0.0)), reset=bool(msg.get("reset"))
            )
            return {"kind": "zoom_ack"}
        return {"kind": "error", "error": f"unknown kind {msg.get('kind')!r}"}

    transport.set_request_handler(_handler)
    transport.set_status_provider(
        lambda: {
            "seq": replica_apply.stats["seq"],
            "gaps": replica_apply.stats["gaps"],
            "errors": replica_apply.stats["apply_errors"],
            "unknown": replica_apply.stats["unknown_uuid"],
            # Encoder state rides along so the HOST panel can say why a
            # viewer can't connect without anyone remoting to the replica.
            "pixel": pixel_path.status(),
            "ffmpeg": state.get("ffmpeg_note", ""),
        }
    )
    transport.start()
    state["transport"] = transport
    state["note"] = "listening"
    replica_apply.start(transport, _prefs_mappings(prefs))
    overlay.enable(_replica_overlay_text)
    if getattr(prefs, "replica_kiosk", False):
        kiosk.enter()
    else:
        kiosk.prepare_viewport()
    _start_pixel_path(prefs)


def _replica_srt_url(prefs) -> str:
    """The listen URL is generated — nobody types SRT URLs. srt_url stays as
    an override for unusual topologies."""
    if prefs.srt_url:
        return prefs.srt_url
    bind = prefs.bind_address or "0.0.0.0"
    latency_us = getattr(prefs, "srt_latency_ms", 300) * 1000
    return f"srt://{bind}:{prefs.srt_port}?mode=listener&latency={latency_us}"


def _start_pixel_path(prefs) -> None:
    """Sync-only mode (decision #17): no stream, the user views the replica
    through their own remote-desktop tool. Otherwise resolve ffmpeg through
    the chain (prefs → QCView's toolbox.json → PATH) and surface the
    outcome either way. Resolution spawn-verifies each rung (~100 ms+), so
    it runs off the main thread — house rule: the main thread never waits."""
    if not getattr(prefs, "enable_stream", True):
        return
    if state.get("pixel_resolving"):
        return
    state["pixel_resolving"] = True
    # bpy reads happen HERE, main thread; the worker gets plain strings.
    args = (
        prefs.ffmpeg_path, prefs.encoder_rung, _replica_srt_url(prefs),
        protocol.srt_passphrase(prefs.token),
    )

    def _resolve_and_start():
        try:
            ffmpeg, source = toolbox.resolve_ffmpeg(args[0])
            if ffmpeg is None:
                state["ffmpeg_note"] = f"stream OFF — ffmpeg {source}"
                return
            state["ffmpeg_note"] = f"ffmpeg: {source}"
            pixel_path.start(ffmpeg, args[1], args[2], args[3])
        finally:
            state["pixel_resolving"] = False

    import threading

    threading.Thread(
        target=_resolve_and_start, name="qcb-ffresolve", daemon=True
    ).start()


def _prefs_mappings(prefs) -> list[pathmap.PathMapping]:
    return [
        pathmap.PathMapping(win=m.win, mac=m.mac, enabled=m.enabled, label=m.label)
        for m in prefs.path_mappings
    ]


def on_project_loaded() -> None:
    """Replica, after every bootstrap apply: a project just landed, so this
    machine's job is rendering — re-engage kiosk (Kiosk Mode pref is the
    master switch; a manual exit lasts until the next project) and make sure
    the encoder is running again after a goodbye stopped it."""
    prefs = state.get("prefs")
    if prefs is None or state["role"] != "REPLICA":
        return
    if getattr(prefs, "replica_kiosk", False):
        kiosk.enter(first=False)
    _start_pixel_path(prefs)


def viewer_url() -> str:
    """Host side: the complete caller URL for any receiver — assembled from
    the host's replica_address and the stream descriptor the replica
    reported at handshake. Empty when not applicable."""
    if state["role"] != "HOST":
        return ""
    stream = state.get("peer_stream") or {}
    prefs = state.get("prefs")
    if not stream.get("enabled") or prefs is None:
        return ""
    latency_us = int(stream.get("latency_ms", 300)) * 1000
    url = (
        f"srt://{prefs.replica_address}:{stream.get('port', 9998)}"
        f"?mode=caller&latency={latency_us}"
    )
    passphrase = protocol.srt_passphrase(prefs.token)
    if passphrase:
        url += f"&passphrase={passphrase}&pbkeylen=16"
    return url


def qcview_deep_link() -> str:
    """qcview://stream?url=<pct-encoded>[&name=…] — QCView's shipped
    deep-link format (v2.2.3, openProjectLink)."""
    import urllib.parse

    url = viewer_url()
    if not url:
        return ""
    name = bpy.path.basename(bpy.data.filepath) or "Blender"
    return (
        "qcview://stream?url=" + urllib.parse.quote(url, safe="")
        + "&name=" + urllib.parse.quote(f"Live · {name}")
    )


def shot_mode_toggle() -> str:
    """Host: toggle shot mode — replica locks to the fitted camera frame
    (passepartout opaque, navigation ignored, time still follows)."""
    transport = state.get("transport")
    if state["role"] != "HOST" or transport is None:
        return "not a running host session"
    state["shot_mode"] = not state["shot_mode"]
    transport.request_nowait({"kind": "shot", "on": state["shot_mode"]})
    return "ok"


def replica_zoom(delta: float = 0.0, reset: bool = False) -> str:
    """Host: nudge the replica's camera-view zoom (an offset on top of the
    synced zoom). Fire-and-forget on the control channel."""
    transport = state.get("transport")
    if state["role"] != "HOST" or transport is None:
        return "not a running host session"
    transport.request_nowait({"kind": "zoom", "delta": delta, "reset": reset})
    return "ok"


def force_resync() -> str:
    """Manual tier-3 reship (host role only)."""
    sync = state.get("sync")
    if state["role"] != "HOST" or sync is None:
        return "not a running host session"
    sync.send_bootstrap()
    return "ok"


def _replica_overlay_text() -> str:
    """Burned into the stream (honesty principle). Called per redraw."""
    transport = state["transport"]
    stats = replica_apply.stats
    if transport is None:
        return ""
    if stats["host_ended"]:
        return "◦ session ended by host"
    if not transport.peer_alive:
        return "○ host lost"
    if stats["applying"]:
        return f"⟳ applying {stats['applying']}"
    # Wall clock in-band: glass-to-glass latency = burned-in time vs. the
    # viewer machine's clock (NTP keeps them within ms).
    import time as _time

    now = _time.time()
    clock = _time.strftime("%H:%M:%S", _time.localtime(now)) + f".{int(now * 10) % 10}"
    bits = [f"● live · seq {stats['seq']} · {clock}"]
    if stats["gaps"]:
        bits.append(f"⚠ {stats['gaps']} gaps")
    if stats["apply_errors"]:
        bits.append(f"⚠ {stats['apply_errors']} apply errors")
    if stats["unknown_uuid"]:
        bits.append(f"⚠ {stats['unknown_uuid']} unknown")
    return " · ".join(bits)


def status_text() -> str:
    if not running():
        return "idle"
    transport = state["transport"]
    alive = "●" if (transport and transport.peer_alive) else "○"
    bits = [f"{state['role'].lower()} {alive}", state["note"]]
    if state["paused"]:
        bits.append("paused")
    sync = state.get("sync")
    if sync is not None and len(sync.dirty):
        t1, t2, tomb = sync.dirty.counts()
        bits.append(f"pending {t1}t1/{t2}t2/{tomb}del")
    if sync is not None:
        peer = getattr(transport, "peer_status", {}) or {}
        if peer.get("pixel") and peer["pixel"] != "off":
            bits.append(f"replica stream: {peer['pixel']}")
        elif peer.get("ffmpeg", "").startswith("stream OFF"):
            bits.append(f"replica {peer['ffmpeg']}")
        troubled = (
            peer.get("gaps", 0) or peer.get("unknown", 0) or peer.get("errors", 0)
            or sync.t2_unsupported
        )
        if troubled:
            bits.append(
                "⚠ resync recommended"
                f" (gaps {peer.get('gaps', 0)}, unknown {peer.get('unknown', 0)},"
                f" errors {peer.get('errors', 0)}, unsent {sync.t2_unsupported})"
            )
    if state["role"] == "REPLICA":
        bits.append(_replica_overlay_text())
        bits.append(state.get("ffmpeg_note", ""))
        if pixel_path.status() != "off":
            bits.append(pixel_path.status())
    return " · ".join(b for b in bits if b)
