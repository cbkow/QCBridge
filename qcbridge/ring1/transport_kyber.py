"""Kyber implementation of the transport interface (parity plan, phase S5).

One QUIC connection carries every lane. The connection lives outside Blender:
either in the QCBridge Agent (tray app, already running — the addon attaches
to its local socket) or, for tests and dev, in a `qcb-helper` process this
module spawns and talks to over stdin/stdout. Same frames either way. Kyber is
AGPL; the process boundary keeps it out of the addon.

Mode: QCB_AGENT=spawn forces the helper; otherwise the agent is used when
QCB_AGENT_PORT/QCB_AGENT_SECRET are set (an agent-launched Blender) or the
agent's agent.json exists in the user's config dir.

Replica listens, host connects (same topology as transport_zmq). Request /
reply, heartbeats and pong status stay in Python, byte-compatible with the
zmq transport's control JSON, so ring0 cannot tell the two apart.

Frame, both directions:  u32 BE length | u8 type | body   (see qcb-helper.rs)
"""

from __future__ import annotations

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
T_CMD = 0x10
T_EVENT = 0x20

HOT_KEY_CAMERA = b"cam"
_COLD_WINDOW = 64  # same intent as zmq's SNDHWM: backlog belongs in the dirty set
_PEER = b"\x00"    # one peer today; the byte keeps several replicas possible
_TICK = 0.05


def find_helper(explicit: str = "") -> str | None:
    """Explicit path → QCB_HELPER → a binary bundled next to the addon → the
    spike's cargo build dir (dev checkouts)."""
    exe = "qcb-helper.exe" if sys.platform == "win32" else "qcb-helper"
    here = Path(__file__).resolve()
    candidates = [
        explicit,
        os.environ.get("QCB_HELPER", ""),
        str(here.parents[1] / "bin" / exe),
        str(here.parents[2] / "spikes" / "parity" / "kyber-pipe" / "target" / "release" / exe),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def pack_cold(header: dict, payload: bytes) -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(head)) + head + payload


def unpack_cold(body: bytes) -> tuple[dict, bytes] | None:
    if len(body) < 4:
        return None
    (n,) = struct.unpack_from(">I", body)
    if len(body) < 4 + n:
        return None
    return protocol.decode_cold([body[4:4 + n], body[4 + n:]])


class _HelperLink:
    """Owns the helper process. Writes never block the caller: frames queue
    for a writer thread, and hot values conflate per key while it is busy."""

    def __init__(self, argv: list[str], on_frame: Callable[[int, bytes], None]) -> None:
        self._argv = argv
        self._on_frame = on_frame
        self._proc: subprocess.Popen | None = None
        self._frames: collections.deque[bytes] = collections.deque()
        self._hot: dict[bytes, bytes] = {}
        self._cv = threading.Condition()
        self._closing = False
        self._threads: list[threading.Thread] = []
        self.exited = threading.Event()

    def start(self) -> None:
        log = open(Path(tempfile.gettempdir()) / "qcbridge-helper.log", "ab")
        log.write(f"\n--- spawn {time.ctime()} {' '.join(self._argv[:3])} ---\n".encode())
        log.flush()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            bufsize=0, creationflags=flags,
        )
        for target, name in ((self._read_loop, "qcb-helper-rx"), (self._write_loop, "qcb-helper-tx")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _frame(kind: int, body: bytes) -> bytes:
        return struct.pack(">IB", len(body) + 1, kind) + body

    def send(self, kind: int, body: bytes) -> None:
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

    def close(self, grace: float = 1.0) -> None:
        """Flush what is queued (a goodbye rides out here), then ask the
        helper to exit. It also exits on its own when stdin closes."""
        self.cmd(cmd="shutdown")
        with self._cv:
            self._closing = True
            self._cv.notify()
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass

    def _write_loop(self) -> None:
        stdin = self._proc.stdin
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
                    stdin.write(frame)
            except (OSError, ValueError):
                return
            if closing and not batch:
                return

    def _read_exact(self, n: int) -> bytes | None:
        chunks, got = [], 0
        stdout = self._proc.stdout
        while got < n:
            try:
                chunk = stdout.read(n - got)
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

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
    return os.environ.get("QCB_AGENT", "") != "spawn" and agent_socket_info(role) is not None


class _AgentLink(_HelperLink):
    """Same frames as the helper, over the agent's local TCP socket. The
    attach handshake is the first frame; the reply is an `attached` event."""

    def __init__(self, info: tuple[str, int, str], role: str, on_frame: Callable[[int, bytes], None]) -> None:
        super().__init__([], on_frame)
        self._info = info
        self._role = role
        self._sock = None

    def start(self) -> None:
        import socket

        host, port, secret = self._info
        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(None)
        self._sock.sendall(self._frame(T_CMD, json.dumps(
            {"cmd": "attach", "secret": secret, "role": self._role}).encode("utf-8")))
        for target, name in ((self._read_loop, "qcb-agent-rx"), (self._write_loop, "qcb-agent-tx")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self, grace: float = 1.0) -> None:
        self.cmd(cmd="detach")
        with self._cv:
            self._closing = True
            self._cv.notify()
        time.sleep(0.05)
        try:
            self._sock.close()
        except OSError:
            pass

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
                    self._sock.sendall(frame)
            except OSError:
                return
            if closing and not batch:
                return

    def _read_exact(self, n: int) -> bytes | None:
        chunks, got = [], 0
        while got < n:
            try:
                chunk = self._sock.recv(n - got)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)


def _make_link(cfg: TransportConfig, role: str, on_frame, spawn_args: Callable[[], list[str]]):
    if use_agent(role):
        return _AgentLink(agent_socket_info(role), role, on_frame)
    return _HelperLink(spawn_args(), on_frame)


def _helper_args(cfg: TransportConfig, role: str) -> list[str]:
    helper = find_helper(getattr(cfg, "helper_path", ""))
    if helper is None:
        raise FileNotFoundError(
            "qcb-helper not found — set QCB_HELPER or build spikes/parity/kyber-pipe"
        )
    args = [helper, "--role", role, "--token", getattr(cfg, "token", "") or "qcbridge"]
    if getattr(cfg, "cap_mbps", 0):
        args += ["--cap-mbps", str(cfg.cap_mbps)]
    return args


class HostTransportKyber:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._link: _HelperLink | None = None
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
        self.video_port = 0          # localhost TCP port serving Annex-B HEVC
        self.stats: dict = {}
        self.agent_version = ""      # set in agent mode
        self.agent_mode = False

    def start(self) -> None:
        def spawn_args() -> list[str]:
            args = _helper_args(self._cfg, "host")
            args += ["--connect", f"{self._cfg.address}:{self._cfg.port_control}"]
            if getattr(self._cfg, "fingerprint", ""):
                args += ["--fingerprint", self._cfg.fingerprint]
            if getattr(self._cfg, "video_listen", ""):
                args += ["--video-listen", self._cfg.video_listen]
            return args

        self._link = _make_link(self._cfg, "host", self._on_frame, spawn_args)
        self._link.start()
        self.agent_mode = isinstance(self._link, _AgentLink)
        if self.agent_mode:
            # The agent holds the connection; tell it where (its own pin wins).
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
        with self._lock:
            if not self._link_up or self._cold_outstanding >= _COLD_WINDOW:
                return False
            self._cold_outstanding += 1
        self._link.send(T_COLD, _PEER + pack_cold(header, payload))
        return True

    @property
    def peer_alive(self) -> bool:
        return self._alive

    def on_peer_state(self, cb: Callable[[bool], None]) -> None:
        self._peer_cb = cb

    # ── helper frames (reader thread) ────────────────────────────────────────

    def _on_frame(self, kind: int, body: bytes) -> None:
        if kind == T_CONTROL:
            reply = protocol.decode_control(body[1:])
            if reply is not None:
                self._handle_reply(reply)
        elif kind == T_COLD_ACK:
            (count,) = struct.unpack(">I", body[:4])
            with self._lock:
                self._cold_outstanding = max(0, self._cold_outstanding - count)
        elif kind == T_EVENT:
            self._handle_event(json.loads(body.decode("utf-8")))

    def _handle_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "attached":  # agent mode: current state at attach time
            self.agent_version = event.get("version", "")
            self.video_port = int(event.get("video_port") or 0)
            self._link_up = bool(event.get("peer_up"))
            self.peer_fingerprint = event.get("peer_fingerprint") or ""
            self.peer_pinned = True
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
        elif name == "error":
            self.link_note = event.get("msg") or ""

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


class ReplicaTransportKyber:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._link: _HelperLink | None = None
        self._handler: Callable[[dict], dict] = lambda msg: {"kind": "error"}
        self._status_provider: Callable[[], dict] | None = None
        self._hot: dict[bytes, bytes] = {}
        self._hot_lock = threading.Lock()
        self._cold_q: collections.deque[tuple[dict, bytes]] = collections.deque()
        self._last_ping = 0.0
        self._ready = threading.Event()
        self._port = 0
        self.fingerprint = ""        # our certificate SHA-256 — show it for pairing
        self.link_note = ""
        self.video_state = "off"
        self.stats: dict = {}
        self.agent_version = ""
        self.agent_mode = False
        self.quit_requested = False

    def start(self) -> None:
        def spawn_args() -> list[str]:
            args = _helper_args(self._cfg, "replica")
            args += ["--listen", f"{self._cfg.address}:{self._cfg.port_control}"]
            if getattr(self._cfg, "cert_dir", ""):
                args += ["--cert-dir", self._cfg.cert_dir]
            return args

        self._link = _make_link(self._cfg, "replica", self._on_frame, spawn_args)
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
            msg = protocol.decode_control(body[1:])
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
        elif kind == T_EVENT:
            event = json.loads(body.decode("utf-8"))
            name = event.get("event")
            if name in ("listening", "attached"):
                self._port = int(event.get("port") or 0)
                self.fingerprint = event.get("fingerprint") or ""
                self.agent_version = event.get("version", "")
                self.video_state = event.get("video_state") or self.video_state
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
