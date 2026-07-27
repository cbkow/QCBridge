"""Transport interface — the decision-#14 quarantine boundary.

Ring-1 session logic talks to these shapes only; transport_zmq.py is the sole
module allowed to import zmq. Threading contract (enforced by convention,
documented here because it is load-bearing):

  Host   — send_hot()/send_cold() are called from ONE thread only (Blender's
           main thread); control requests + heartbeats run on the transport's
           internal IO thread.
  Replica — all sockets live on the IO thread; callers read completed state
           via poll_hot()/poll_cold(), which touch only thread-safe buffers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class TransportConfig:
    address: str            # host role: replica address to connect to
                            # replica role: interface to bind (e.g. tunnel IP)
    port_control: int
    port_hot: int
    port_cold: int
    heartbeat_interval: float = 1.0
    heartbeat_misses: int = 3


class HostTransport(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...

    def request(self, msg: dict, timeout: float) -> dict | None:
        """Send a control request; block up to `timeout` for the reply."""
        ...

    def send_hot(self, packed: bytes) -> None: ...

    def send_cold(self, header: dict, payload: bytes = b"") -> bool:
        """Non-blocking; False = not deliverable now (peer gone / backpressure)
        — the caller keeps the datablock dirty and retries on a later flush."""
        ...

    @property
    def peer_alive(self) -> bool: ...

    peer_status: dict
    """Latest status dict the replica attached to a pong (empty until one
    arrives). The host reads this to recommend a resync — decision-#8-clean
    because the replica only ever answers."""

    def on_peer_state(self, cb: Callable[[bool], None]) -> None:
        """cb(True) on peer (re)appearing, cb(False) on peer-lost — fired
        from the IO thread; the callback must not touch bpy directly."""
        ...


class ReplicaTransport(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...

    def set_request_handler(self, handler: Callable[[dict], dict]) -> None:
        """handler(request) -> reply, called on the IO thread (never bpy).
        Heartbeat pings are answered internally and never reach the handler."""
        ...

    def set_status_provider(self, provider: Callable[[], dict]) -> None:
        """Small dict to ride on every pong (IO thread — no bpy)."""
        ...

    def poll_hot(self) -> bytes | None:
        """Latest hot message, or None; reading does not consume newer data
        arriving concurrently (last-value register)."""
        ...

    def poll_cold(self, max_items: int) -> list[tuple[dict, bytes]]:
        """Up to max_items complete cold messages, oldest first."""
        ...

    @property
    def peer_alive(self) -> bool: ...
