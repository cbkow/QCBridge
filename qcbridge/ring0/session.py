"""Session lifecycle for both roles.

Start/stop is always explicit (operators in prefs.py). The control-channel
request handler runs on the transport IO thread and must never touch bpy —
it works purely on strings captured at session start.
"""

from __future__ import annotations

import os
import uuid as _uuid

import bpy

from ..ring1 import liveness, pathmap, protocol, toolbox
from ..ring1.transport import TransportConfig
from . import host_handlers, host_hot, kiosk, overlay, pixel_path, replica_apply



def _use_agent(prefs) -> bool:
    """Transport switch: QCB_TRANSPORT, then a `transport` pref, then the
    agent whenever one is registered for this role on the machine, else
    zmq (frozen but supported). Both ends must land on the same kind; the
    hello says which. See ring1.transport_agent.transport_kind."""
    from ..ring1 import transport_agent  # stdlib-only; no pyzmq behind it

    role = str(getattr(prefs, "role", "HOST") or "HOST")
    return transport_agent.transport_kind(role, getattr(prefs, "transport", "")) == "agent"


def _make_transport(prefs, cfg: TransportConfig, role: str):
    # Imported lazily: the agent path must not need pyzmq, nor zmq the agent.
    if _use_agent(prefs):
        from ..ring1 import transport_agent

        cls = (transport_agent.HostTransportAgent if role == "HOST"
               else transport_agent.ReplicaTransportAgent)
    else:
        from ..ring1 import transport_zmq

        cls = (transport_zmq.HostTransportZmq if role == "HOST"
               else transport_zmq.ReplicaTransportZmq)
    return cls(cfg)


def _agent_cert_dir() -> str:
    """Stable across sessions, or the host's pinned fingerprint would break."""
    return os.path.join(bpy.utils.user_resource("CONFIG"), "qcbridge-cert")

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
    "peer_addon": None,    # peer's addon version ("" = pre-0.1.5 peer)
}


def _addon_version() -> str:
    """Our version, from the shipped manifest (the one version site)."""
    global _ADDON_VERSION
    if _ADDON_VERSION is None:
        try:
            import tomllib
            from pathlib import Path

            manifest = Path(__file__).resolve().parent.parent / "blender_manifest.toml"
            _ADDON_VERSION = tomllib.loads(manifest.read_text())["version"]
        except Exception:
            _ADDON_VERSION = "?"
    return _ADDON_VERSION


_ADDON_VERSION: str | None = None


def _version_warning() -> str:
    # The agent installs separately from the extension; a stale one next
    # to a new addon would fail quietly on a lane it does not know.
    transport = state.get("transport")
    agent_v = getattr(transport, "agent_version", "") if transport else ""
    if getattr(transport, "agent_mode", False) and agent_v and agent_v != _addon_version():
        return f"⚠ agent {agent_v}, addon {_addon_version()} — update both"
    peer = state.get("peer_addon")
    if state.get("peer_epoch") is None or peer is None:
        return ""  # nothing paired yet
    if peer != _addon_version():
        return (
            f"⚠ peer runs {peer or 'pre-0.1.5'}, this end {_addon_version()}"
            " — update both ends"
        )
    return ""


def running() -> bool:
    return state["role"] is not None


def _start_agent_if_needed(prefs) -> None:
    """Agent mode is the default; with no autostart, the installed agent
    is started here when none is registered for this role. A zmq
    preference or QCB_TRANSPORT=zmq leaves it alone."""
    from ..ring1 import transport_agent

    env = os.environ.get("QCB_TRANSPORT", "").strip().lower()
    pref = str(getattr(prefs, "transport", "") or "").strip().lower()
    if env == "zmq" or pref == "zmq":
        return
    transport_agent.ensure_agent(str(getattr(prefs, "role", "HOST") or "HOST"))


def start(prefs) -> str:
    if running():
        return "already running"
    _start_agent_if_needed(prefs)
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
        peer_addon=None,
        # Cleared here or a session stopped mid-resolve leaves it True and
        # every later _start_pixel_path returns at the guard: sync works,
        # the stream never starts again.
        pixel_resolving=False,
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


def _agent_secrets(transport) -> tuple[str, str] | None:
    """(hello secret, SRT passphrase) as the agent reports them, or None
    before the attach reply has arrived. No bpy: safe on the IO thread. An
    older agent that still mirrored `token` is handled by deriving here."""
    cfg = getattr(transport, "agent_config", None) or {}
    if cfg.get("hello_secret") is not None and cfg.get("srt_passphrase") is not None:
        return cfg["hello_secret"], cfg["srt_passphrase"]
    if cfg.get("token"):
        return protocol.hello_secret(cfg["token"]), protocol.srt_passphrase(cfg["token"])
    return None


def _secrets(prefs, transport, fallback_token: str | None = None) -> tuple[str, str]:
    """(hello secret, SRT passphrase) in force. In agent mode the agent owns
    the token and reports only these derivations at attach (2026-09-24);
    the addon's own token pref is the zmq fallback, derived here the same
    way the agent does it. Read at the moment of use, not captured at
    start: the replica's attach reply lands after its session starts, and
    a secret captured before it is empty (the first paired run said
    "token mismatch" for exactly that). `fallback_token` stands in for
    `prefs.token` where bpy must not be touched."""
    from_agent = _agent_secrets(transport)
    if from_agent is not None:
        return from_agent
    if getattr(transport, "agent_mode", False):
        return "", ""  # attached but nothing mirrored: refuse rather than guess
    token = prefs.token if fallback_token is None else fallback_token
    return token, protocol.srt_passphrase(token)


def _effective_peer_host(prefs, transport) -> str:
    """The replica's address: the addon's, when given (an explicit override),
    else the agent's configured peer. Feeds the SRT viewer URL, which is
    the addon's business even though the connection is the agent's."""
    if prefs.replica_address:
        return prefs.replica_address
    cfg = getattr(transport, "agent_config", None) or {}
    return (cfg.get("peer") or "").rsplit(":", 1)[0]


def _start_host(prefs) -> None:
    agent = _use_agent(prefs)
    # Agent mode: a blank address means the agent's own peer stands, so there
    # is nothing to pre-check; the old "127.0.0.1" fallback would have
    # overridden that peer with the loopback.
    address_error = "" if (agent and not prefs.replica_address) else _check_address(prefs.replica_address or "127.0.0.1")
    cfg = TransportConfig(
        address=prefs.replica_address if agent else (prefs.replica_address or "127.0.0.1"),
        port_control=prefs.port_control,
        port_hot=prefs.port_hot,
        port_cold=prefs.port_cold,
        token=prefs.token,
        helper_path=getattr(prefs, "helper_path", ""),
        fingerprint=getattr(prefs, "replica_fingerprint", ""),
    )
    transport = _make_transport(prefs, cfg, "HOST")
    transport.start()
    state["transport"] = transport
    state["note"] = address_error or "connecting"
    if agent and hasattr(transport, "wait_attached"):
        transport.wait_attached(3.0)  # agent_config — secrets, peer — arrives with `attached`
        _push_paths_to_agent(prefs, transport)
        _mirror_paths_from_agent(prefs, transport)
    # Read once for this session: the sync's canonicalizer and the hello
    # must agree on the same rows. (An agent that had none takes the
    # addon's, pushed just above.)
    mappings = _effective_mappings(prefs, transport)
    cache_root = _effective_cache_root(prefs, transport)

    # Fire-and-poll — this timer runs on Blender's main thread, and a dead
    # peer must never freeze the UI (it did: a blocking 3 s request per
    # retry bogged Blender down whenever the replica was unreachable).
    # Re-armable: a reconnect or a restarted replica pairs afresh (below).
    pending = {"req": None, "sent_at": 0.0, "armed": False}

    def _handshake():
        import time as _time

        if state["transport"] is not transport:
            return None  # session stopped/replaced
        now = _time.monotonic()
        if pending["req"] is None:
            sync = state.get("sync")
            # Main thread (a timer): the secret as of now, so a slow attach
            # or a token set meanwhile is picked up by the next hello.
            hello_secret, _ = _secrets(prefs, transport)
            hello = protocol.make_hello(
                hello_secret, state["epoch"], bpy.app.version_string,
                addon_version=_addon_version(),
                seq=sync.seq if sync else 0,
                seq_fast=getattr(sync, "seq_fast", 0) if sync else 0,
                mappings=pathmap.rows_to_wire(mappings),
                shared_root=_shared_root_card(transport),
            )
            pending["req"] = transport.request_nowait(hello)
            pending["sent_at"] = now
            return 0.25
        reply = transport.poll_reply(pending["req"])
        if reply is not None:
            if reply.get("ok"):
                state["peer_epoch"] = reply.get("epoch")
                state["peer_stream"] = reply.get("stream") or {}
                _adopt_pair_row(transport, _shared_root_card(transport), reply.get("shared_root") or {})
                state["peer_addon"] = reply.get("addon", "")
                state["note"] = "connected"
                state["resync_policy"].reset()
                # Fresh handshake = fresh epoch pairing → full bootstrap
                # (decision #16). Transport blips re-handshake too, with a
                # fresh host epoch, because frames sent into the outage are
                # gone (SYNC-AUDIT A6) and the replica may be a new process.
                state["sync"].send_bootstrap()
                # Re-assert host-owned replica state a fresh session lost.
                transport.request_nowait(
                    {"kind": "shot", "on": state["shot_mode"]}
                )
                pending["req"] = None
                pending["armed"] = False
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

    def _arm_handshake(note: str, new_epoch: bool) -> None:
        if pending["armed"]:
            return
        if new_epoch:
            import uuid as _uuid
            state["epoch"] = _uuid.uuid4().hex  # the replica resets its seq tracker on a new epoch
        pending["armed"] = True
        pending["req"] = None
        state["note"] = note
        bpy.app.timers.register(_handshake, first_interval=0.1, persistent=True)

    # Peer transitions arrive on the IO thread: flag them, act on the tick.
    flags = {"lost": False, "reconnect": False}

    def _on_peer(alive: bool) -> None:
        if not alive:
            flags["lost"] = True
        elif flags["lost"]:
            flags["reconnect"] = True

    transport.on_peer_state(_on_peer)
    state["resync_policy"] = liveness.ResyncPolicy()

    def _watch():
        """Main-thread watcher: reconnects, replica restarts, resync requests."""
        import time as _time

        if state["transport"] is not transport:
            return None
        if flags["lost"] and not flags["reconnect"] and not pending["armed"]:
            state["note"] = "replica lost — waiting"
        if flags["reconnect"]:
            flags["reconnect"] = False
            flags["lost"] = False
            _arm_handshake("reconnected — re-syncing", new_epoch=True)
            return 0.25
        status = getattr(transport, "peer_status", {}) or {}
        if not pending["armed"] and liveness.replica_restarted(status, state["peer_epoch"]):
            _arm_handshake("replica restarted — re-syncing", new_epoch=True)
            return 0.25
        _mirror_paths_from_agent(prefs, transport)  # a tray/window edit shows up in the panel
        sync = state.get("sync")
        if sync is not None and not pending["armed"] and state["resync_policy"].should_resync(
            status, _time.monotonic()
        ):
            sync.send_bootstrap()
            state["auto_resyncs"] = state.get("auto_resyncs", 0) + 1
        return 0.25

    _arm_handshake(address_error or "connecting", new_epoch=False)
    bpy.app.timers.register(_watch, first_interval=0.5, persistent=True)
    host_hot.start(transport)
    state["sync"] = host_handlers.start(
        transport,
        paused_fn=lambda: state["paused"],
        mappings=mappings,
        cache_root=cache_root,
    )


def _start_replica(prefs) -> None:
    cfg = TransportConfig(
        address=prefs.bind_address or "0.0.0.0",
        port_control=prefs.port_control,
        port_hot=prefs.port_hot,
        port_cold=prefs.port_cold,
        token=prefs.token,
        helper_path=getattr(prefs, "helper_path", ""),
        cert_dir=_agent_cert_dir() if _use_agent(prefs) else "",
    )
    transport = _make_transport(prefs, cfg, "REPLICA")
    # The capture child is ours in both transports now: video leaves over SRT
    # and never touches the connection.
    pixel_path.set_external()
    fallback_token = prefs.token  # main thread; the handler must not touch bpy
    epoch = state["epoch"]
    own_rows = {"rows": []}  # filled after attach (below); read by the handler

    # Captured now (main thread) so the IO-thread handler touches no bpy.
    # No passphrase here: both ends derive it from the shared token, so the
    # secret never crosses the wire.
    stream_info = {
        "enabled": bool(getattr(prefs, "enable_stream", True)),
        "port": prefs.srt_port,
        "latency_ms": getattr(prefs, "srt_latency_ms", 120),
    }

    our_version = _addon_version()  # captured: the handler runs on IO thread

    def _handler(msg: dict) -> dict:  # IO thread: strings only, no bpy
        if msg.get("kind") == "hello":
            hello_secret, _ = _secrets(None, transport, fallback_token)
            ok, reason = protocol.check_hello(msg, hello_secret)
            if ok:
                # The host's table rides in the hello: this machine's rows
                # first, the host's rows it lacks after them.
                received = pathmap.rows_from_wire(msg.get("mappings"))
                replica_apply.set_mappings(pathmap.merge_tables(own_rows["rows"], received))
                _adopt_pair_row(transport, _shared_root_card(transport), msg.get("shared_root") or {})
                state["host_mappings"] = len(received)
                if state["peer_epoch"] != msg.get("epoch"):
                    replica_apply.notify_new_session(msg.get("seq", 0), msg.get("seq_fast", 0))
                state["peer_epoch"] = msg.get("epoch")
                state["peer_addon"] = msg.get("addon", "")
                state["note"] = "host connected"
            return {
                "kind": "hello_reply", "ok": ok, "reason": reason,
                "epoch": epoch, "stream": stream_info,
                "shared_root": _shared_root_card(transport),
                "addon": our_version,
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
            # Everything else the replica knows and the host could not see
            # (SYNC-AUDIT §4.2): who we are, what we want, what went wrong.
            "epoch": epoch,
            "want_resync": replica_apply.stats["want_resync"],
            "seq_fast": replica_apply.stats["seq_fast"],
            "parked": replica_apply.stats["parked"],
            "bootstraps": replica_apply.stats["bootstraps"],
            "unmapped": replica_apply.stats["unmapped_paths"],
            "frozen": replica_apply.stats["frozen_caches"],
            "last_error": replica_apply.stats["last_error"][:120],
            "local_edits": replica_apply.stats["local_edits"],
            "last_local_edit": replica_apply.stats["last_local_edit"][:60],
            # Encoder state rides along so the HOST panel can say why a
            # viewer can't connect without anyone remoting to the replica.
            "pixel": pixel_path.status(),
            "ffmpeg": state.get("ffmpeg_note", ""),
        }
    )
    transport.start()  # agent mode: returns once attached, so the mirror is in
    state["transport"] = transport
    state["note"] = "listening"
    _push_paths_to_agent(prefs, transport)
    _mirror_paths_from_agent(prefs, transport)
    own_rows["rows"] = _effective_mappings(prefs, transport)
    replica_apply.start(transport, own_rows["rows"])
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
    latency_us = getattr(prefs, "srt_latency_ms", 120) * 1000
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
        prefs.ffmpeg_path, prefs.encoder_rung,
        _replica_srt_url(prefs),
        _secrets(prefs, state.get("transport"))[1],
    )
    transport = state.get("transport")

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


def _effective_mappings(prefs, transport) -> list[pathmap.PathMapping]:
    """The table in force. In agent mode the agent owns it (2026-09-24) and
    mirrors it at attach; the addon's own rows serve zmq mode, and an agent
    that holds none yet (they are pushed up at session start, below)."""
    cfg = getattr(transport, "agent_config", None) or {}
    if getattr(transport, "agent_mode", False) and cfg.get("path_mappings"):
        return pathmap.rows_from_wire(cfg["path_mappings"])
    return _prefs_mappings(prefs)


def _effective_cache_root(prefs, transport) -> str:
    cfg = getattr(transport, "agent_config", None) or {}
    if getattr(transport, "agent_mode", False) and cfg.get("cache_root"):
        return str(cfg["cache_root"])
    return getattr(prefs, "cache_root", "") or ""


def _shared_root_card(transport) -> dict:
    """This machine's shared folder for the hello / hello reply."""
    cfg = getattr(transport, "agent_config", None) or {}
    root = str(cfg.get("shared_root") or "")
    return {"path": root, "os": pathmap.current_os_tag()} if root else {}


def _adopt_pair_row(transport, ours: dict, theirs: dict) -> None:
    """Both machines named their shared folder; on a mixed pair that is a
    mapping row, and the agent that lacks it gets it. No bpy: safe on the
    IO thread. Same-OS pairs need no row."""
    if not ours or not theirs:
        return
    if ours.get("os") == theirs.get("os") or not getattr(transport, "agent_mode", False):
        return
    win = ours["path"] if ours["os"] == "win" else theirs["path"]
    mac = ours["path"] if ours["os"] == "mac" else theirs["path"]
    if not win or not mac:
        return
    cfg = getattr(transport, "agent_config", None) or {}
    rows = pathmap.rows_from_wire(cfg.get("path_mappings"))
    new = pathmap.PathMapping(win=win, mac=mac, enabled=True, label="shared")
    merged = pathmap.merge_tables(rows, [new])
    if len(merged) != len(rows):
        transport.set_config(path_mappings=pathmap.rows_to_wire(merged))
        print("qcbridge: mapping row for the shared folder formed at pairing", flush=True)


def _push_paths_to_agent(prefs, transport) -> None:
    """First session against an agent that holds no table: the addon's
    rows and cache root move up, once, so an existing setup keeps working
    and the agent is the owner from then on."""
    cfg = getattr(transport, "agent_config", None) or {}
    if not getattr(transport, "agent_mode", False) or not cfg:
        return
    fields = {}
    if not cfg.get("path_mappings") and len(prefs.path_mappings):
        fields["path_mappings"] = pathmap.rows_to_wire(_prefs_mappings(prefs))
    if not cfg.get("cache_root") and (getattr(prefs, "cache_root", "") or ""):
        fields["cache_root"] = prefs.cache_root
    if fields:
        transport.set_config(**fields)
        print(f"qcbridge: moved {', '.join(fields)} into the agent", flush=True)


def _mirror_paths_from_agent(prefs, transport) -> None:
    """Main thread only. The preferences show the agent's table and cache
    root so the panel reads what is in force; edits go back through
    qcbridge.agent_save_paths."""
    cfg = getattr(transport, "agent_config", None) or {}
    if not getattr(transport, "agent_mode", False) or not cfg:
        return
    if state.get("mirrored_paths_from") is cfg:
        return
    state["mirrored_paths_from"] = cfg
    if "path_mappings" in cfg and not isinstance(prefs, dict) and hasattr(prefs, "path_mappings") \
            and hasattr(prefs.path_mappings, "add"):
        rows = pathmap.rows_from_wire(cfg["path_mappings"])
        if rows != _prefs_mappings(prefs) and rows:
            while prefs.path_mappings:
                prefs.path_mappings.remove(0)
            for r in rows:
                m = prefs.path_mappings.add()
                m.win, m.mac, m.enabled, m.label = r.win, r.mac, r.enabled, r.label
    if cfg.get("cache_root") and hasattr(prefs, "cache_root") and prefs.cache_root != cfg["cache_root"]:
        try:
            prefs.cache_root = cfg["cache_root"]
        except (AttributeError, TypeError):
            pass


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
    transport = state.get("transport")
    host_addr = _effective_peer_host(prefs, transport)
    if not host_addr:
        return ""
    latency_us = int(stream.get("latency_ms", 120)) * 1000
    url = (
        f"srt://{host_addr}:{stream.get('port', 9998)}"
        f"?mode=caller&latency={latency_us}"
    )
    _, passphrase = _secrets(prefs, transport)
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
    st = getattr(transport, "stats", None) or {}
    if st.get("rtt_ms") is not None:
        bits.append(f"rtt {st['rtt_ms']:.0f} ms")
    if _version_warning():
        bits.append("⚠ version mismatch")
    if stats["gaps"]:
        bits.append(f"⚠ {stats['gaps']} gaps")
    if stats["apply_errors"]:
        bits.append(f"⚠ {stats['apply_errors']} apply errors")
    if stats["unknown_uuid"]:
        bits.append(f"⚠ {stats['unknown_uuid']} unknown")
    if stats["want_resync"]:
        bits.append("⟳ resync requested")
    if stats["unmapped_paths"]:
        bits.append(f"⚠ {stats['unmapped_paths']} unmapped paths")
    if stats["frozen_caches"]:
        bits.append(f"⚠ {stats['frozen_caches']} frozen disk caches")
    if stats["last_error"]:
        bits.append(f"⚠ {stats['last_error'][:48]}")
    if stats["local_edits"]:
        bits.append(f"⚠ edited here: {stats['last_local_edit'][:32]} ({stats['local_edits']})")
    return " · ".join(bits)


def status_text() -> str:
    if not running():
        return "idle"
    transport = state["transport"]
    alive = "●" if (transport and transport.peer_alive) else "○"
    bits = [f"{state['role'].lower()} {alive}", state["note"], _version_warning()]
    sync_ = state.get("sync")
    if sync_ is not None and getattr(sync_, "sync_errors", 0):
        bits.append(
            f"⚠ {sync_.sync_errors} sync errors ({sync_.last_sync_error})"
        )
    if state["paused"]:
        bits.append("paused")
    # Agent link state: where the connection actually lives.
    if getattr(transport, "agent_mode", False):
        bits.append(f"agent {getattr(transport, 'agent_version', '') or '?'}")
        ac = getattr(transport, "agent_config", None) or {}
        if ac.get("name"):
            bits.append(f"as {ac['name']}")
        if state["role"] == "HOST" and ac.get("peer"):
            bits.append(f"→ {ac['peer']}")
        if state["role"] == "REPLICA" and ac.get("discovery"):
            bits.append({"off": "not reachable", "direct": "reachable by address",
                         "discoverable": "discoverable on the LAN"}.get(ac["discovery"], ac["discovery"]))
    note = getattr(transport, "link_note", "")
    if note:
        bits.append(f"link: {note}")
    st = getattr(transport, "stats", None) or {}
    if st.get("rtt_ms") is not None:
        bits.append(
            f"rtt {st['rtt_ms']:.0f} ms · lost {st.get('quic_lost', 0)}"
            f" · ↑{st.get('tx_mbps', 0):.1f} ↓{st.get('rx_mbps', 0):.1f} Mb/s"
        )
    fp = getattr(transport, "peer_fingerprint", "")
    if fp and state["role"] == "HOST":
        pinned = getattr(transport, "peer_pinned", True)
        bits.append(f"replica cert {fp[:8]}…{'' if pinned else ' (new — pinned on first use)'}")
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
        if peer.get("want_resync"):
            bits.append("⟳ replica asked for a resync — sending")
        if state.get("auto_resyncs"):
            bits.append(f"auto-resyncs {state['auto_resyncs']}")
        if getattr(sync, "t2_skipped", 0):
            bits.append(f"unchanged blobs skipped {sync.t2_skipped}")
        if peer.get("unmapped"):
            bits.append(f"⚠ replica: {peer['unmapped']} unmapped paths — check path mappings")
        if peer.get("frozen"):
            bits.append(f"⚠ replica: {peer['frozen']} disk caches frozen — use an external cache path")
        if peer.get("last_error"):
            bits.append(f"⚠ replica error: {peer['last_error'][:60]}")
        if peer.get("local_edits"):
            bits.append(f"⚠ replica edited locally: {peer.get('last_local_edit', '')[:40]} ({peer['local_edits']}) — Force Resync overrides")
        dropped = getattr(transport, "cold_dropped", 0)
        if dropped:
            bits.append(f"⚠ {dropped} frames dropped by the agent")
        if getattr(sync, "bake_note", ""):
            bits.append(f"⚠ {sync.bake_note} — Force Resync ships it")
        if getattr(sync, "cache_note", ""):
            bits.append(sync.cache_note)
    if state["role"] == "REPLICA":
        bits.append(_replica_overlay_text())
        bits.append(state.get("ffmpeg_note", ""))
        if pixel_path.status() != "off":
            bits.append(pixel_path.status())
    return " · ".join(b for b in bits if b)
