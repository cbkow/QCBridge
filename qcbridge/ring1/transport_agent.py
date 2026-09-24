"""Agent implementation of the transport interface.

One QUIC connection carries control, hot and cold. It lives outside Blender,
in the QCBridge Agent: a separate process that owns the connection, the
capture child and (on a replica) Blender's lifecycle. This module never
speaks QUIC — it exchanges frames with the agent over a loopback socket, so
nothing here knows or cares which protocol the agent runs. That is why the
Kyber-to-quinn change did not touch this file beyond its name.

Video does not ride the connection: the replica's encoder sends SRT and
QCView opens the srt:// URL directly.

Mode: normally the agent is already running and we attach to it, found via
QCB_AGENT_PORT/QCB_AGENT_SECRET (an agent-launched Blender) or agent.json in
the user's config dir. QCB_AGENT=spawn starts a private agent instead, which
is what the contract tests use — each gets its own directory, so its config,
certificate and agent.json are isolated.

Replica listens, host connects (same topology as transport_zmq). Request /
reply, heartbeats and pong status stay in Python, byte-compatible with the
zmq transport's control JSON, so ring0 cannot tell the two apart.

Frame, both directions:  u32 BE length | u8 type | body   (see agent/src/link.rs)
"""

from __future__ import annotations

import atexit
import collections
import itertools
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import protocol
from .transport import TransportConfig

T_CONTROL = 0x01
T_HOT = 0x02
T_COLD = 0x03
T_COLD_ACK = 0x04
T_FAST = 0x05       # tier-1 deltas and tombstones: their own QUIC stream
T_FAST_ACK = 0x06
T_CMD = 0x10
T_EVENT = 0x20

HOT_KEY_CAMERA = b"cam"
_COLD_WINDOW = 64                 # messages, against an agent that acks counts
_COLD_WINDOW_BYTES = 32 << 20     # bytes in flight between us and quinn
_FAST_WINDOW_BYTES = 4 << 20      # deltas are small; this only bounds a storm  # same intent as zmq's SNDHWM: backlog belongs in the dirty set
_PEER = b"\x00"    # one peer today; the byte keeps several replicas possible
_TICK = 0.05


def find_agent(explicit: str = "") -> str | None:
    """Explicit path → QCB_AGENT_BIN → a binary bundled next to the addon →
    the agent crate's cargo output (dev checkouts, release before debug)."""
    exe = "qcbridge-agent.exe" if sys.platform == "win32" else "qcbridge-agent"
    here = Path(__file__).resolve()
    repo = here.parents[2]
    for path in (explicit, os.environ.get("QCB_AGENT_BIN", ""), str(here.parents[1] / "bin" / exe)):
        if path and os.path.isfile(path):
            return path
    # Dev checkouts: whichever cargo profile was built most recently. A fixed
    # release-before-debug order once had the contract tests exercising a
    # stale release binary while every hand check ran the fresh debug one —
    # the tests passed and proved nothing.
    builds = [
        str(repo / "agent" / "target" / prof / exe) for prof in ("release", "debug")
    ]
    builds = [b for b in builds if os.path.isfile(b)]
    return max(builds, key=os.path.getmtime) if builds else None


def pack_cold(header: dict, payload: bytes) -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(head)) + head + payload


def cold_parts(header: dict, payload) -> list:
    """The cold body as buffers the link writes in turn — no concatenation
    of a 4 MiB chunk on the main thread (SYNC-AUDIT D4)."""
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return [_PEER, struct.pack(">I", len(head)), head, payload]


_VIEW_MIN = 65536  # payloads at least this big stay memoryviews of the receive buffer


def unpack_cold(body) -> tuple[dict, bytes] | None:
    if len(body) < 4:
        return None
    (n,) = struct.unpack_from(">I", body)
    if len(body) < 4 + n:
        return None
    payload = body[4 + n:]
    if not isinstance(payload, (bytes, bytearray)) and len(payload) < _VIEW_MIN:
        payload = bytes(payload)  # small: a real bytes object, decode()-able
    return protocol.decode_cold([bytes(body[4:4 + n]), payload])


class _FrameLink:
    """Frame queueing, shared by every link. Writes never block the caller:
    frames queue for a writer thread, and hot values conflate per key while
    it is busy. Subclasses own the actual byte pipe."""

    def __init__(self, on_frame: Callable[[int, bytes], None]) -> None:
        self._on_frame = on_frame
        self._proc: subprocess.Popen | None = None
        self._frames: collections.deque[bytes] = collections.deque()
        self._hot: dict[bytes, bytes] = {}
        self._cv = threading.Condition()
        self._closing = False
        self._threads: list[threading.Thread] = []
        self.exited = threading.Event()

    def start(self) -> None:
        raise NotImplementedError

    def _write_loop(self) -> None:
        raise NotImplementedError

    def _read_exact(self, n: int) -> bytes | None:
        raise NotImplementedError

    @staticmethod
    def _frame(kind: int, body) -> list:
        """A frame as the buffers to write in turn: header, then the body's
        parts. Nothing is concatenated — a big blob chunk goes to the socket
        from the memoryview it was sliced as."""
        parts = body if isinstance(body, list) else [body]
        total = sum(len(p) for p in parts)
        return [struct.pack(">IB", total + 1, kind), *parts]

    def send(self, kind: int, body) -> None:
        """body: bytes, or a list of buffers (bytes / memoryview)."""
        with self._cv:
            self._frames.append(self._frame(kind, body))
            self._cv.notify()

    def send_hot(self, key: bytes, value: bytes) -> None:
        body = _PEER + bytes([len(key)]) + key + value
        with self._cv:
            self._hot[key] = self._frame(T_HOT, body)
            self._cv.notify()

    def cmd(self, **fields) -> None:
        self.send(T_CMD, json.dumps(fields, separators=(",", ":")).encode("utf-8"))

    def _stop_proc(self, grace: float = 2.0) -> None:
        """Only set when we spawned a private agent (QCB_AGENT=spawn)."""
        proc = self._proc
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _read_loop(self) -> None:
        while True:
            head = self._read_exact(5)
            if head is None:
                break
            length, kind = struct.unpack(">IB", head)
            body = self._read_exact(length - 1) if length > 1 else b""
            if body is None:
                break
            try:
                self._on_frame(kind, body)
            except Exception:  # a handler bug must not kill the reader
                pass
        self.exited.set()


def agent_socket_info(role: str) -> tuple[str, int, str] | None:
    """(host, port, secret) of a running agent for this role, or None.
    agent.json keeps one entry per role (a machine can run both)."""
    port, secret = os.environ.get("QCB_AGENT_PORT"), os.environ.get("QCB_AGENT_SECRET")
    if port and secret:
        return ("127.0.0.1", int(port), secret)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, "QCBridge", "agent.json")
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)[role]
        return ("127.0.0.1", int(info["port"]), str(info["secret"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def use_agent(role: str) -> bool:
    """True when an agent for this role is reachable without spawning one."""
    return os.environ.get("QCB_AGENT", "") != "spawn" and agent_socket_info(role) is not None


def transport_kind(role: str, pref: str = "") -> str:
    """Which transport a session uses: "agent" or "zmq".

    QCB_TRANSPORT in the environment wins (the smokes and an agent-launched
    Blender set it). Then an explicit preference. Otherwise the default is
    the agent whenever one is registered for this role on the machine
    (agent.json, or the launch environment) — a plain double-click of
    Blender must land in the mode the product ships with, not the frozen
    fallback (2026-09-23: it did not, and the panel showed the 0.1.6
    connection box). No agent → zmq, which still works end to end."""
    env = os.environ.get("QCB_TRANSPORT", "").strip().lower()
    if env in ("agent", "zmq"):
        return env
    pref = (pref or "").strip().lower()
    if pref in ("agent", "zmq"):
        return pref
    return "agent" if use_agent(role.lower()) else "zmq"


class _AgentLink(_FrameLink):
    """Frames over the agent's local TCP socket. The attach handshake is the
    first frame; the reply is an `attached` event. `proc` is set only when we
    spawned the agent ourselves and are therefore responsible for it."""

    def __init__(
        self,
        info: tuple[str, int, str],
        role: str,
        on_frame: Callable[[int, bytes], None],
        proc: subprocess.Popen | None = None,
    ) -> None:
        super().__init__(on_frame)
        self._info = info
        self._role = role
        self._sock = None
        self._proc = proc

    def start(self) -> None:
        import socket

        host, port, secret = self._info
        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(None)
        self._sendall_parts(self._frame(T_CMD, json.dumps(
            {"cmd": "attach", "secret": secret, "role": self._role}).encode("utf-8")))
        for target, name in ((self._read_loop, "qcb-agent-rx"), (self._write_loop, "qcb-agent-tx")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self, grace: float = 1.0) -> None:
        # A shared agent outlives us and only gets a detach; one we spawned
        # is ours to stop.
        self.cmd(cmd="detach")
        with self._cv:
            self._closing = True
            self._cv.notify()
        time.sleep(0.05)
        try:
            self._sock.close()
        except OSError:
            pass
        self._stop_proc(grace)

    def _write_loop(self) -> None:
        while True:
            with self._cv:
                while not self._frames and not self._hot and not self._closing:
                    self._cv.wait()
                batch = list(self._frames)
                self._frames.clear()
                batch.extend(self._hot.values())
                self._hot.clear()
                closing = self._closing
            try:
                for frame in batch:
                    for part in frame:
                        if len(part):
                            self._sock.sendall(part)
            except OSError:
                return
            if closing and not batch:
                return

    def _sendall_parts(self, parts: list) -> None:
        for part in parts:
            if len(part):
                self._sock.sendall(part)

    def _read_exact(self, n: int):
        """One preallocated buffer, filled in place; returns a memoryview.
        Handlers slice it without copying, and a blob chunk reaches the
        reassembler as a view of this buffer (SYNC-AUDIT D4)."""
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            try:
                r = self._sock.recv_into(view[got:])
            except OSError:
                return None
            if not r:
                return None
            got += r
        return view


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def spawn_agent(cfg: TransportConfig, role: str) -> tuple[tuple[str, int, str], subprocess.Popen]:
    """Start a private agent for this role and wait for it to register.

    Everything the instance owns — config, certificate, agent.json — lives in
    one directory, so two agents on one machine cannot collide. `cert_dir`
    picks that directory when given (its parent), which is how a caller keeps
    a replica's certificate stable across restarts.
    """
    binary = find_agent(getattr(cfg, "agent_path", ""))
    if binary is None:
        raise FileNotFoundError(
            "qcbridge-agent not found — set QCB_AGENT_BIN, or build it with "
            "cargo build in agent/"
        )
    cert_dir = getattr(cfg, "cert_dir", "")
    base = Path(cert_dir).parent if cert_dir else Path(tempfile.mkdtemp(prefix="qcb-agent-"))
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / f"{role}.toml"

    peer = f"{cfg.address}:{cfg.port_control}"
    lines = [
        f'role = "{role}"',
        f'token = "{getattr(cfg, "token", "") or "qcbridge"}"',
        # A spawned agent must not touch the user's keychain: its token
        # lives in an owner-only file in this directory (the agent moves
        # it there from this line on first start) and dies with it.
        'token_store = "file"',
        f'listen = "{peer}"' if role == "replica" else f'peer = "{peer}"',
        'blender_path = ""',   # whoever spawned us already has Blender
        "tray = false",
        "video_port = 0",      # video goes over SRT, not through the agent
    ]
    if getattr(cfg, "fingerprint", ""):
        lines.append(f'fingerprint = "{cfg.fingerprint}"')
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A previous agent in this directory may still be registered: it is
    # killed, not asked to quit, so nothing removed its entry. Drop our role
    # first, or we would read the dead port back and dial it.
    info_path = base / "agent.json"
    try:
        doc = json.loads(info_path.read_text(encoding="utf-8"))
        doc.pop(role, None)
        if doc:
            info_path.write_text(json.dumps(doc), encoding="utf-8")
        else:
            info_path.unlink()
    except (OSError, ValueError, AttributeError):
        pass

    log_path = base / f"{role}-agent.log"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    log = open(log_path, "ab")
    # --exit-with-addon: the agent belongs to us. Without it, a Blender that
    # is killed rather than closed leaves the agent holding its UDP port, and
    # the next run fails with "Address already in use".
    proc = subprocess.Popen(
        [binary, "--config", str(cfg_path), "--exit-with-addon"],
        stdout=log, stderr=log, creationflags=flags,
    )
    # Belt and braces for a hard kill of our own process, which never gets to
    # close the link.
    atexit.register(_reap, proc)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            entry = json.loads(info_path.read_text(encoding="utf-8"))[role]
            return ("127.0.0.1", int(entry["port"]), str(entry["secret"])), proc
        except (OSError, ValueError, KeyError, TypeError):
            if proc.poll() is not None:
                raise RuntimeError(f"agent exited at once; see {log_path}")
            time.sleep(0.05)
    proc.kill()
    raise TimeoutError(f"agent did not register in {info_path}; see {log_path}")


def _make_link(cfg: TransportConfig, role: str, on_frame) -> _AgentLink:
    if os.environ.get("QCB_AGENT", "") == "spawn":
        info, proc = spawn_agent(cfg, role)
        return _AgentLink(info, role, on_frame, proc)
    info = agent_socket_info(role)
    if info is None:
        raise FileNotFoundError(
            f"no running QCBridge Agent for role {role} — start the agent, or "
            "set QCB_AGENT=spawn to have a private one started"
        )
    return _AgentLink(info, role, on_frame)


class HostTransportAgent:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._link: _AgentLink | None = None
        self._pending: dict[int, tuple[threading.Event | None, list]] = {}
        self._req_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pong = 0.0
        self._alive = False
        self._link_up = False
        self._cold_outstanding = 0
        self._peer_cb: Callable[[bool], None] | None = None
        self.peer_status: dict = {}
        self.peer_fingerprint = ""   # replica certificate SHA-256 (hex)
        self.peer_pinned = True      # False right after trust-on-first-use
        self.link_note = ""          # last helper-reported reason for being down
        self.cold_dropped = 0        # frames the agent had to discard (cold_dropped events)
        self._credits_bytes = False  # COLD_ACK unit; set from the attached event
        self._has_fast = False       # agent has the fast lane (attached event)
        # The agent compresses cold payloads on the wire, so Blender may
        # write partials uncompressed and the replica loads them 20× faster.
        self.wire_compresses = False
        self._fast_outstanding = 0
        self.video_port = 0          # localhost TCP port serving Annex-B HEVC
        self.stats: dict = {}
        self.agent_version = ""      # set in agent mode
        self.agent_exe = ""
        self.agent_mode = False
        # Mirrored from the agent: it owns these, we display them.
        self.agent_config: dict = {}
        self.peers: list = []        # last discovery result
        self.peers_sources: list = []
        self._cmd_ids = itertools.count(1000)
        self._cmd_replies: dict[int, dict] = {}
        self._attached = threading.Event()

    def wait_attached(self, timeout: float = 3.0) -> bool:
        """True once the agent's `attached` reply — and with it agent_config —
        has arrived. The session reads the derived secrets and peer from there."""
        return self._attached.wait(timeout)

    def start(self) -> None:
        self._link = _make_link(self._cfg, "host", self._on_frame)
        self._link.start()
        self.agent_mode = isinstance(self._link, _AgentLink)
        if self.agent_mode and self._cfg.address:
            # An address in the addon is an explicit override; the agent's own
            # pin still wins. Blank means the agent's configured peer stands —
            # it used to be replaced with a 127.0.0.1 fallback here.
            self._link.cmd(cmd="connect", peer=f"{self._cfg.address}:{self._cfg.port_control}",
                           fingerprint=getattr(self._cfg, "fingerprint", "") or "")
        self._stop.clear()
        self._thread = threading.Thread(target=self._io_loop, name="qcb-host-io", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._link:
            self._link.close()
            self._link = None

    # ── control ──────────────────────────────────────────────────────────────

    def _send_control(self, msg: dict) -> None:
        self._link.send(T_CONTROL, _PEER + protocol.encode_control(msg))

    def request(self, msg: dict, timeout: float) -> dict | None:
        """Blocking — never from Blender's main thread (see transport_zmq)."""
        req_id = next(self._req_ids)
        done = threading.Event()
        slot: list = []
        with self._lock:
            self._pending[req_id] = (done, slot)
        self._send_control({**msg, "req": req_id})
        done.wait(timeout)
        with self._lock:
            self._pending.pop(req_id, None)
        return slot[0] if slot else None

    def request_nowait(self, msg: dict) -> int:
        req_id = next(self._req_ids)
        with self._lock:
            self._pending[req_id] = (None, [])
        self._send_control({**msg, "req": req_id})
        return req_id

    def poll_reply(self, req_id: int) -> dict | None:
        with self._lock:
            pending = self._pending.get(req_id)
            if pending and pending[1]:
                del self._pending[req_id]
                return pending[1][0]
        return None

    def cancel_request(self, req_id: int) -> None:
        with self._lock:
            self._pending.pop(req_id, None)

    # ── hot / cold ───────────────────────────────────────────────────────────

    def send_hot(self, packed: bytes) -> None:
        self.send_hot_keyed(HOT_KEY_CAMERA, packed)

    def send_hot_keyed(self, key: bytes, value: bytes) -> None:
        """Latest-wins per key, conflated at every hop (plan S5 requirement 1:
        the seam for live in-progress drags)."""
        if self._link_up:
            self._link.send_hot(key, value)

    def send_cold(self, header: dict, payload: bytes = b"") -> bool:
        parts = cold_parts(header, payload)
        size = sum(len(p) for p in parts)
        cost = size if self._credits_bytes else 1
        with self._lock:
            window = _COLD_WINDOW_BYTES if self._credits_bytes else _COLD_WINDOW
            # A frame larger than the window still goes when nothing is in
            # flight, or it could never go at all.
            if not self._link_up or (
                self._cold_outstanding and self._cold_outstanding + cost > window
            ):
                return False
            self._cold_outstanding += cost
        self._link.send(T_COLD, parts)
        return True

    def send_fast(self, header: dict, payload: bytes = b"") -> bool:
        """Tier-1 deltas and tombstones. Own stream and own window on an
        agent that has the lane; the cold lane otherwise (the header still
        carries lane/after, so the replica merges either way)."""
        if not self._has_fast:
            return self.send_cold(header, payload)
        parts = cold_parts(header, payload)
        size = sum(len(p) for p in parts)
        with self._lock:
            if not self._link_up or (
                self._fast_outstanding and self._fast_outstanding + size > _FAST_WINDOW_BYTES
            ):
                return False
            self._fast_outstanding += size
        self._link.send(T_FAST, parts)
        return True

    @property
    def peer_alive(self) -> bool:
        return self._alive

    def on_peer_state(self, cb: Callable[[bool], None]) -> None:
        self._peer_cb = cb

    # ── helper frames (reader thread) ────────────────────────────────────────

    def _on_frame(self, kind: int, body) -> None:
        if kind == T_CONTROL:
            reply = protocol.decode_control(bytes(body[1:]))
            if reply is not None:
                self._handle_reply(reply)
        elif kind == T_COLD_ACK:
            (count,) = struct.unpack(">I", body[:4])
            with self._lock:
                self._cold_outstanding = max(0, self._cold_outstanding - count)
        elif kind == T_FAST_ACK:
            (count,) = struct.unpack(">I", body[:4])
            with self._lock:
                self._fast_outstanding = max(0, self._fast_outstanding - count)
        elif kind == T_EVENT:
            self._handle_event(json.loads(bytes(body).decode("utf-8")))

    def _handle_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "attached":  # agent mode: current state at attach time
            self.agent_version = event.get("version", "")
            self.agent_exe = event.get("exe", "")  # the settings window is this binary with --settings
            self.video_port = int(event.get("video_port") or 0)
            self._link_up = bool(event.get("peer_up"))
            self.peer_fingerprint = event.get("peer_fingerprint") or ""
            self.peer_pinned = True
            self.agent_config = dict(event.get("config") or {})
            self._credits_bytes = event.get("credits") == "bytes"
            self._has_fast = "fast" in (event.get("lanes") or [])
            self.wire_compresses = event.get("codec") == "zstd"
            self._attached.set()
        elif name == "config":   # every settings change, from us or the tray
            self.agent_config = dict(event.get("config") or self.agent_config)
            self._stash_reply(event)
        elif name == "peers":
            self.peers = list(event.get("peers") or [])
            self.peers_sources = list(event.get("sources") or [])
            self._stash_reply(event)
        elif name == "rejected":  # used to fall off the end of this chain
            self.link_note = "agent refused the attach: " + (event.get("reason") or "bad secret")
        elif name == "peer":
            self._link_up = bool(event.get("up"))
            if self._link_up:
                self.peer_fingerprint = event.get("fingerprint") or ""
                self.peer_pinned = bool(event.get("pinned", True))
                self.link_note = ""
            else:
                self.link_note = event.get("reason") or ""
                self._last_pong = 0.0
        elif name == "video_listen":
            self.video_port = int(event.get("port") or 0)
        elif name == "stats":
            self.stats = event
        elif name == "cold_dropped":
            # The agent had to discard a cold frame (session down or its
            # queue full) and credited it back so we would not stall. The
            # datablock is NOT on the replica; the reconnect re-handshake
            # ships a bootstrap, and this counter says it happened.
            self.cold_dropped += int(event.get("n") or 1)
        elif name == "error":
            self.link_note = event.get("msg") or ""

    # -- agent commands with a correlated reply ---------------------------
    # T_CMD has no reply channel; the agent answers with an event that echoes
    # `req`. These stash such events so a caller can poll for its own.
    def _stash_reply(self, event: dict) -> None:
        req = event.get("req")
        if isinstance(req, int):
            self._cmd_replies[req] = event

    def poll_cmd_reply(self, req_id: int) -> dict | None:
        """The agent's reply to `req_id`, once; None until it arrives."""
        return self._cmd_replies.pop(req_id, None)

    def discover(self, addr: str | None = None) -> int:
        """Probe one address (the VPN path) or sweep the LAN and phonebook.
        Results land in `peers` and in the correlated `peers` reply."""
        req = next(self._cmd_ids)
        fields = {"cmd": "discover", "req": req}
        if addr:
            fields["addr"] = addr
        self._link.cmd(**fields)
        return req

    def set_config(self, **fields) -> int:
        """Change agent settings. The `config` reply names what changed, what
        needs a restart, and what was rejected."""
        req = next(self._cmd_ids)
        self._link.cmd(cmd="set_config", req=req, set=fields)
        return req

    def _handle_reply(self, reply: dict) -> None:
        if reply.get("kind") == "pong":
            self._last_pong = time.monotonic()
            status = reply.get("status")
            if isinstance(status, dict):
                self.peer_status = status
            return
        with self._lock:
            pending = self._pending.get(reply.get("req"))
        if pending:
            done, slot = pending
            slot.append(reply)
            if done is not None:
                done.set()

    def _io_loop(self) -> None:
        next_ping = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if self._link_up and now >= next_ping:
                self._send_control({"kind": "ping"})
                next_ping = now + self._cfg.heartbeat_interval
            window = self._cfg.heartbeat_interval * self._cfg.heartbeat_misses
            alive = self._link_up and self._last_pong > 0 and (now - self._last_pong) < window
            if alive != self._alive:
                self._alive = alive
                if self._peer_cb:
                    self._peer_cb(alive)
            self._stop.wait(_TICK)


class ReplicaTransportAgent:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._link: _AgentLink | None = None
        self._handler: Callable[[dict], dict] = lambda msg: {"kind": "error"}
        self._status_provider: Callable[[], dict] | None = None
        self._hot: dict[bytes, bytes] = {}
        self._hot_lock = threading.Lock()
        self._cold_q: collections.deque[tuple[dict, bytes]] = collections.deque()
        self._fast_q: collections.deque[tuple[dict, bytes]] = collections.deque()
        self._last_ping = 0.0
        self._ready = threading.Event()
        self._port = 0
        self.fingerprint = ""        # our certificate SHA-256 — show it for pairing
        self.link_note = ""
        self.video_state = "off"
        self.stats: dict = {}
        self.agent_config: dict = {}
        self.peers: list = []
        self.peers_sources: list = []
        self._cmd_ids = itertools.count(1000)
        self._cmd_replies: dict[int, dict] = {}
        self.agent_version = ""
        self.agent_exe = ""
        self.agent_mode = False
        self.quit_requested = False

    def start(self) -> None:
        self._link = _make_link(self._cfg, "replica", self._on_frame)
        self._link.start()
        self.agent_mode = isinstance(self._link, _AgentLink)
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._link:
            self._link.close()
            self._link = None

    def set_request_handler(self, handler: Callable[[dict], dict]) -> None:
        self._handler = handler

    def set_status_provider(self, provider: Callable[[], dict]) -> None:
        self._status_provider = provider

    def poll_hot(self) -> bytes | None:
        with self._hot_lock:
            return self._hot.get(HOT_KEY_CAMERA)

    def poll_hot_keyed(self) -> dict[bytes, bytes]:
        """Snapshot of every key's latest value."""
        with self._hot_lock:
            return dict(self._hot)

    def poll_fast(self, max_items: int) -> list[tuple[dict, bytes]]:
        items = []
        while len(items) < max_items:
            try:
                items.append(self._fast_q.popleft())
            except IndexError:
                break
        return items

    def poll_cold(self, max_items: int) -> list[tuple[dict, bytes]]:
        items = []
        while len(items) < max_items:
            try:
                items.append(self._cold_q.popleft())
            except IndexError:
                break
        return items

    @property
    def peer_alive(self) -> bool:
        window = self._cfg.heartbeat_interval * self._cfg.heartbeat_misses
        return self._last_ping > 0 and (time.monotonic() - self._last_ping) < window

    def bound_ports(self) -> tuple[int, int, int]:
        """One UDP port carries every lane; repeated for interface parity."""
        return (self._port, self._port, self._port)

    # ── video child, owned by the helper (S6/S7 make it native) ──────────────

    def start_video(self, argv: list[str], **params) -> None:
        """argv = the ffmpeg fallback; params (fps, bitrate_mbps, region,
        ten_bit) let the agent build its native capture command instead."""
        self._link.cmd(cmd="video_start", argv=argv, **params)

    def stop_video(self) -> None:
        self._link.cmd(cmd="video_stop")

    # ── helper frames (reader thread = this transport's IO thread) ───────────

    def _on_frame(self, kind: int, body: bytes) -> None:
        if kind == T_CONTROL:
            msg = protocol.decode_control(bytes(body[1:]))
            if msg is not None:
                self._serve_control(msg)
        elif kind == T_HOT:
            klen = body[1]
            with self._hot_lock:
                self._hot[bytes(body[2:2 + klen])] = bytes(body[2 + klen:])
        elif kind == T_COLD:
            decoded = unpack_cold(body[1:])
            if decoded is not None:
                self._cold_q.append(decoded)
        elif kind == T_FAST:
            decoded = unpack_cold(body[1:])
            if decoded is not None:
                self._fast_q.append(decoded)
        elif kind == T_EVENT:
            event = json.loads(bytes(body).decode("utf-8"))
            name = event.get("event")
            if name == "attached":  # ("listening" was a helper-era event the agent never emits)
                self._port = int(event.get("port") or 0)
                self.fingerprint = event.get("fingerprint") or ""
                self.agent_version = event.get("version", "")
                self.agent_exe = event.get("exe", "")
                self.video_state = event.get("video_state") or self.video_state
                self.agent_config = dict(event.get("config") or {})
                self._ready.set()
            elif name == "config":
                self.agent_config = dict(event.get("config") or self.agent_config)
                self._stash_reply(event)
            elif name == "peers":
                self.peers = list(event.get("peers") or [])
                self.peers_sources = list(event.get("sources") or [])
                self._stash_reply(event)
            elif name == "rejected":
                # Before, this timed out five seconds later with no reason.
                self.link_note = "agent refused the attach: " + (event.get("reason") or "bad secret")
                self._ready.set()
            elif name == "quit":
                self.quit_requested = True  # the agent wants Blender closed
            elif name == "peer":
                self.link_note = "" if event.get("up") else (event.get("reason") or "")
                if not event.get("up"):
                    self._last_ping = 0.0
            elif name == "video":
                self.video_state = event.get("state") or ""
            elif name == "stats":
                self.stats = event
            elif name == "error":
                self.link_note = event.get("msg") or ""

    def _stash_reply(self, event: dict) -> None:
        req = event.get("req")
        if isinstance(req, int):
            self._cmd_replies[req] = event

    def poll_cmd_reply(self, req_id: int) -> dict | None:
        return self._cmd_replies.pop(req_id, None)

    def discover(self, addr: str | None = None) -> int:
        req = next(self._cmd_ids)
        fields = {"cmd": "discover", "req": req}
        if addr:
            fields["addr"] = addr
        self._link.cmd(**fields)
        return req

    def set_config(self, **fields) -> int:
        req = next(self._cmd_ids)
        self._link.cmd(cmd="set_config", req=req, set=fields)
        return req

    def _serve_control(self, msg: dict) -> None:
        if msg.get("kind") == "ping":
            self._last_ping = time.monotonic()
            reply = {"kind": "pong"}
            if self._status_provider is not None:
                try:
                    reply["status"] = self._status_provider()
                except Exception:
                    pass
        else:
            try:
                reply = self._handler(msg)
            except Exception as exc:  # handler bugs must not kill the IO thread
                reply = {"kind": "error", "error": repr(exc)}
            reply["req"] = msg.get("req")
        self._link.send(T_CONTROL, _PEER + protocol.encode_control(reply))
