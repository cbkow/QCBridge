"""Host-side decisions about the replica's liveness, from the pong status
dict alone (pure: no bpy, no transport).

The replica only ever answers (decision #8), so everything the host learns
about its state rides the pong: its session epoch, whether it wants a
resync, how many bootstraps it has applied. These helpers turn that into
"re-handshake" / "send a bootstrap" decisions the session acts on from the
main thread.
"""
from __future__ import annotations


def replica_restarted(status: dict, known_epoch: str | None) -> bool:
    """True when the pong names a replica epoch other than the one we paired
    with: the replica process restarted (or another one answered) and holds
    none of our state. Unknown/absent epochs never trigger — a pre-handshake
    pong or an older replica must not start a re-handshake loop."""
    epoch = status.get("epoch")
    return bool(epoch) and known_epoch is not None and epoch != known_epoch


class ResyncPolicy:
    """Honour the replica's `want_resync` once per replica state, rate-limited.

    The replica raises the flag on a seq gap or an unknown uuid and clears it
    when a bootstrap applies (its `bootstraps` count goes up). We send one
    bootstrap per (flag, bootstraps) pair, never faster than `min_interval`,
    so a flapping link cannot turn into a bootstrap storm."""

    def __init__(self, min_interval: float = 10.0) -> None:
        self.min_interval = min_interval
        self._last_sent = float("-inf")
        self._served_for: int | None = None  # replica bootstraps count we served
        self.sent = 0

    def should_resync(self, status: dict, now: float) -> bool:
        if not status.get("want_resync"):
            return False
        boots = status.get("bootstraps")
        if boots is not None and boots == self._served_for:
            return False  # our bootstrap is in flight or arrived; wait for the flag to clear
        if now - self._last_sent < self.min_interval:
            return False
        self._last_sent = now
        self._served_for = boots
        self.sent += 1
        return True

    def reset(self) -> None:
        """A fresh handshake resets the pairing; serve the next request anew."""
        self._served_for = None
