"""Host-side decisions about the replica's liveness, from the pong status
dict alone (pure: no bpy, no transport).

The replica only ever answers (decision #8), so everything the host learns
about its state rides the pong: its session epoch, whether it wants a
resync, which lane has the hole, how many bootstraps and resyncs it has
applied. These helpers turn that into "re-handshake" / "re-assert the fast
window" / "send a bootstrap" decisions the session acts on from the main
thread.
"""
from __future__ import annotations

CAP_FAST_ACK = "fast_ack"
CAP_DELTA = "delta_resync"


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
    when the repair applies: a bootstrap (its `bootstraps` count goes up) or
    a fast re-assert (`resyncs` goes up). We answer once per
    (flag, bootstraps, resyncs) state, never faster than `min_interval` for
    a bootstrap and `fast_interval` for a re-assert, so a flapping link
    cannot turn into a bootstrap storm."""

    VERDICTS = ("none", "fast", "delta", "full")

    def __init__(self, min_interval: float = 10.0, fast_interval: float = 1.0) -> None:
        self.min_interval = min_interval
        self.fast_interval = fast_interval
        self._last_sent = float("-inf")
        self._last_fast = float("-inf")
        self._served_for: tuple | None = None  # (bootstraps, resyncs) we served
        self.sent = 0
        self.fast_sent = 0
        self.delta_sent = 0

    @staticmethod
    def _state(status: dict) -> tuple:
        return (status.get("bootstraps"), status.get("resyncs", 0))

    def decide(self, status: dict, now: float, caps: tuple | list | None = None,
               fast_covers=None, delta_state: str | None = None) -> str:
        """"none" | "fast" | "delta" | "full".

        "fast" when the hole is on the fast lane only, the replica can
        acknowledge fast seqs (`fast_ack` in its caps) and the host's fast
        log still covers the hole (`fast_covers(fast_gap_at)` is True).
        "delta" when the replica can take one (`delta_resync` in its caps)
        and the caller's inventory comparison says "ready" (`delta_state`;
        "pending" = the manifest is on its way, answer nothing yet;
        "mismatch"/None = the Scene's structure differs, bootstrap).
        "full" otherwise. The caller acts on the verdict at once."""
        if not status.get("want_resync"):
            return "none"
        state = self._state(status)
        if state == self._served_for:
            return "none"  # our repair is in flight or arrived; wait for the flag to clear
        gap_lane = status.get("gap_lane", "")
        if (gap_lane == "fast" and caps and CAP_FAST_ACK in caps
                and fast_covers is not None and fast_covers(status.get("fast_gap_at"))):
            if now - self._last_fast < self.fast_interval:
                return "none"
            self._last_fast = now
            self._served_for = state
            self.fast_sent += 1
            return "fast"
        if caps and CAP_DELTA in caps and delta_state in ("ready", "pending"):
            if delta_state == "pending":
                return "none"  # the manifest is being fetched
            if now - self._last_fast < self.fast_interval:
                return "none"
            self._last_fast = now
            self._served_for = state
            self.delta_sent += 1
            return "delta"
        if now - self._last_sent < self.min_interval:
            return "none"
        self._last_sent = now
        self._served_for = state
        self.sent += 1
        return "full"

    def allow_full(self, now: float) -> bool:
        """A caller that decided "delta" found nothing to plan (the Scene's
        structure differs) and wants the file instead: the full-bootstrap
        rate limit applies to it too (2026-10-28: without it a persistent
        structure difference re-shipped the file at the 1 s fast cadence;
        0.2.1 allowed one per `min_interval`). False = not yet; the
        decision is un-served so the next tick asks again."""
        if now - self._last_sent < self.min_interval:
            self._served_for = None
            return False
        self._last_sent = now
        self.sent += 1
        return True

    def should_resync(self, status: dict, now: float) -> bool:
        """The pre-0.3 question: bootstrap or not. Kept for callers that
        know nothing about lanes."""
        return self.decide(status, now) == "full"

    def reset(self) -> None:
        """A fresh handshake resets the pairing; serve the next request anew."""
        self._served_for = None
