"""pyzmq implementation of the transport interface (decision #16 topology).

The ONLY ring-1 module allowed to import zmq (decision #14). Replica binds,
host connects. Socket/thread ownership per transport.py's contract.
"""

from __future__ import annotations

import collections
import itertools
import queue
import threading
import time
from typing import Callable

import zmq

from . import protocol
from .transport import TransportConfig

_POLL_MS = 50
_COLD_SNDHWM = 64  # small on purpose: backpressure surfaces early; the
                   # dirty set, not the socket, is where backlog belongs


def _endpoint(address: str, port: int) -> str:
    return f"tcp://{address}:{port}"


class HostTransportZmq:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._ctx: zmq.Context | None = None
        self._hot: zmq.Socket | None = None
        self._cold: zmq.Socket | None = None
        self._outbox: queue.Queue[bytes] = queue.Queue()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._req_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pong = 0.0
        self._alive = False
        self._peer_cb: Callable[[bool], None] | None = None
        self.peer_status: dict = {}  # latest status carried on a pong

    def start(self) -> None:
        self._ctx = zmq.Context.instance()
        self._hot = self._ctx.socket(zmq.PUB)
        self._hot.setsockopt(zmq.LINGER, 0)
        self._hot.connect(_endpoint(self._cfg.address, self._cfg.port_hot))
        self._cold = self._ctx.socket(zmq.PUSH)
        self._cold.setsockopt(zmq.LINGER, 0)
        self._cold.setsockopt(zmq.SNDHWM, _COLD_SNDHWM)
        self._cold.setsockopt(zmq.IMMEDIATE, 1)
        self._cold.connect(_endpoint(self._cfg.address, self._cfg.port_cold))
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._io_loop, name="qcb-host-io", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        for sock in (self._hot, self._cold):
            if sock is not None:
                sock.close(0)
        self._hot = self._cold = None

    def request(self, msg: dict, timeout: float) -> dict | None:
        """Blocking request — NEVER call from Blender's main thread (a dead
        peer means the full timeout as UI freeze). Sessions use the
        nowait/poll pair; this stays for scripts and tests."""
        req_id = next(self._req_ids)
        msg = {**msg, "req": req_id}
        done = threading.Event()
        slot: list = []
        with self._lock:
            self._pending[req_id] = (done, slot)
        self._outbox.put(protocol.encode_control(msg))
        done.wait(timeout)
        with self._lock:
            self._pending.pop(req_id, None)
        return slot[0] if slot else None

    def request_nowait(self, msg: dict) -> int:
        """Queue a request and return immediately; fetch the reply later
        with poll_reply(). Main-thread safe by construction."""
        req_id = next(self._req_ids)
        msg = {**msg, "req": req_id}
        with self._lock:
            self._pending[req_id] = (None, [])
        self._outbox.put(protocol.encode_control(msg))
        return req_id

    def poll_reply(self, req_id: int) -> dict | None:
        """The reply for a request_nowait id, exactly once, or None yet."""
        with self._lock:
            pending = self._pending.get(req_id)
            if pending and pending[1]:
                del self._pending[req_id]
                return pending[1][0]
        return None

    def cancel_request(self, req_id: int) -> None:
        with self._lock:
            self._pending.pop(req_id, None)

    def send_hot(self, packed: bytes) -> None:
        try:
            self._hot.send(packed, zmq.NOBLOCK)
        except zmq.Again:
            pass  # no subscriber: hot is ephemeral by design

    def send_cold(self, header: dict, payload: bytes = b"") -> bool:
        try:
            self._cold.send_multipart(protocol.encode_cold(header, payload), zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False

    @property
    def peer_alive(self) -> bool:
        return self._alive

    def on_peer_state(self, cb: Callable[[bool], None]) -> None:
        self._peer_cb = cb

    # ── IO thread: owns the DEALER socket ────────────────────────────────────

    def _io_loop(self) -> None:
        ctl = self._ctx.socket(zmq.DEALER)
        ctl.setsockopt(zmq.LINGER, 0)
        ctl.connect(_endpoint(self._cfg.address, self._cfg.port_control))
        poller = zmq.Poller()
        poller.register(ctl, zmq.POLLIN)
        next_ping = 0.0
        carry: bytes | None = None  # unsent message survives a would-block
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_ping:
                    try:
                        ctl.send(protocol.encode_control({"kind": "ping"}), zmq.NOBLOCK)
                    except zmq.Again:
                        pass  # buffer full: skipping a ping is what it's for
                    next_ping = now + self._cfg.heartbeat_interval
                while True:
                    if carry is None:
                        try:
                            carry = self._outbox.get_nowait()
                        except queue.Empty:
                            break
                    try:
                        ctl.send(carry, zmq.NOBLOCK)
                        carry = None
                    except zmq.Again:
                        break
                if dict(poller.poll(_POLL_MS)).get(ctl):
                    reply = protocol.decode_control(ctl.recv())
                    if reply is not None:
                        self._handle_reply(reply)
                self._update_liveness(time.monotonic())
        finally:
            # Best-effort flush: a fire-and-forget goodbye queued right
            # before stop() rides out here.
            try:
                if carry is not None:
                    ctl.send(carry, zmq.NOBLOCK)
                while True:
                    ctl.send(self._outbox.get_nowait(), zmq.NOBLOCK)
            except (queue.Empty, zmq.Again):
                pass
            ctl.close(0)

    def _handle_reply(self, reply: dict) -> None:
        if reply.get("kind") == "pong":
            self._last_pong = time.monotonic()
            status = reply.get("status")
            if isinstance(status, dict):
                self.peer_status = status
            return
        req_id = reply.get("req")
        with self._lock:
            pending = self._pending.get(req_id)
        if pending:
            done, slot = pending
            slot.append(reply)
            if done is not None:
                done.set()

    def _update_liveness(self, now: float) -> None:
        window = self._cfg.heartbeat_interval * self._cfg.heartbeat_misses
        alive = self._last_pong > 0 and (now - self._last_pong) < window
        if alive != self._alive:
            self._alive = alive
            if self._peer_cb:
                self._peer_cb(alive)


class ReplicaTransportZmq:
    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._ctx: zmq.Context | None = None
        self._handler: Callable[[dict], dict] = lambda msg: {"kind": "error"}
        self._hot_slot: bytes | None = None
        self._hot_lock = threading.Lock()
        self._cold_q: collections.deque[tuple[dict, bytes]] = collections.deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ping = 0.0
        self._ports: tuple[int, int, int] | None = None
        self._ready = threading.Event()
        self._status_provider: Callable[[], dict] | None = None

    def start(self) -> None:
        self._ctx = zmq.Context.instance()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._io_loop, name="qcb-replica-io", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def set_request_handler(self, handler: Callable[[dict], dict]) -> None:
        self._handler = handler

    def poll_hot(self) -> bytes | None:
        with self._hot_lock:
            return self._hot_slot

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
        """Actual (control, hot, cold) ports — differs from config when
        binding port 0 (tests)."""
        assert self._ports is not None, "start() first"
        return self._ports

    # ── IO thread: owns all three sockets ────────────────────────────────────

    def _io_loop(self) -> None:
        def bind(sock: zmq.Socket, port: int) -> int:
            sock.setsockopt(zmq.LINGER, 0)
            sock.bind(_endpoint(self._cfg.address, port))
            endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
            return int(endpoint.rsplit(":", 1)[1])

        ctl = self._ctx.socket(zmq.ROUTER)
        hot = self._ctx.socket(zmq.SUB)
        hot.setsockopt(zmq.CONFLATE, 1)  # before bind
        hot.setsockopt(zmq.SUBSCRIBE, b"")
        cold = self._ctx.socket(zmq.PULL)
        try:
            self._ports = (
                bind(ctl, self._cfg.port_control),
                bind(hot, self._cfg.port_hot),
                bind(cold, self._cfg.port_cold),
            )
            self._ready.set()
            poller = zmq.Poller()
            for sock in (ctl, hot, cold):
                poller.register(sock, zmq.POLLIN)
            while not self._stop.is_set():
                events = dict(poller.poll(_POLL_MS))
                if events.get(ctl):
                    self._serve_control(ctl)
                if events.get(hot):
                    packed = hot.recv()
                    with self._hot_lock:
                        self._hot_slot = packed
                if events.get(cold):
                    while True:
                        try:
                            frames = cold.recv_multipart(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        decoded = protocol.decode_cold(frames)
                        if decoded is not None:
                            self._cold_q.append(decoded)
        finally:
            for sock in (ctl, hot, cold):
                sock.close(0)

    def set_status_provider(self, provider: Callable[[], dict]) -> None:
        """Small status dict to ride on every pong (called on the IO thread —
        must not touch bpy; reading a plain dict the main thread updates is
        fine). This is the replica ANSWERING the host's heartbeat, never
        pushing (decision #8-clean) — it's how the host learns the replica
        has gaps and can recommend a resync."""
        self._status_provider = provider

    def _serve_control(self, ctl: zmq.Socket) -> None:
        ident, payload = ctl.recv_multipart()
        msg = protocol.decode_control(payload)
        if msg is None:
            return
        if msg.get("kind") == "ping":
            self._last_ping = time.monotonic()
            reply = {"kind": "pong"}
            provider = getattr(self, "_status_provider", None)
            if provider is not None:
                try:
                    reply["status"] = provider()
                except Exception:
                    pass
        else:
            try:
                reply = self._handler(msg)
            except Exception as exc:  # handler bugs must not kill the IO thread
                reply = {"kind": "error", "error": repr(exc)}
            reply["req"] = msg.get("req")
        ctl.send_multipart([ident, protocol.encode_control(reply)])
