"""Merging two ordered lanes into one apply order (pure: no bpy, no transport).

Cold carries blobs and bootstraps in order; fast carries tier-1 deltas and
tombstones in order; nothing orders one lane against the other. The host
stamps every fast message with `after`: the cold seq it must follow — the
last chunk of the newest blob for that datablock, or the last bootstrap
chunk, whichever is later. The replica parks a fast message until its cold
lane has *applied* that seq, then releases it. Cold is strictly ordered, so
"applied seq >= after" is exact.

Each lane has its own SeqTracker; a gap on either is a gap. The merger also
tracks how far each lane has been APPLIED (not just seen) and where the
first hole is, so the pong can tell the host which lane to repair and from
which seq (fast: a re-assert from ring1.fastlog; cold: a delta or a
bootstrap).
"""
from __future__ import annotations

from .protocol import SeqTracker

LANE_COLD = "c"
LANE_FAST = "f"


class LaneMerger:
    def __init__(self) -> None:
        self.cold = SeqTracker()
        self.fast = SeqTracker()
        self.cold_applied = 0            # highest cold seq fully applied
        self.fast_applied = 0            # highest CONTIGUOUS fast seq applied
        self._fast_done: set[int] = set()  # applied fast seqs above fast_applied
        self._parked: list[tuple[int, dict, bytes]] = []  # (after, header, payload)

    @property
    def gaps(self) -> int:
        return self.cold.gaps + self.fast.gaps

    @property
    def parked(self) -> int:
        return len(self._parked)

    @property
    def gap_lane(self) -> str:
        """Which lane has an unresolved hole: "", "cold", "fast" or "both"."""
        c = self.cold.first_missing is not None
        f = self.fast.first_missing is not None
        return "both" if c and f else "cold" if c else "fast" if f else ""

    def lane_of(self, header: dict) -> str:
        return header.get("lane") or LANE_COLD

    def observe(self, header: dict) -> bool:
        """Track the lane's seq. Returns True if contiguous."""
        seq = header.get("seq")
        if seq is None:
            return True
        tracker = self.fast if self.lane_of(header) == LANE_FAST else self.cold
        return tracker.observe(seq)

    def admit(self, header: dict, payload: bytes) -> bool:
        """Fast-lane message: True = apply now; False = parked until the cold
        seq it names has applied. Cold-lane messages are always admitted."""
        if self.lane_of(header) != LANE_FAST:
            return True
        after = int(header.get("after") or 0)
        if after <= self.cold_applied:
            return True
        self._parked.append((after, header, payload))
        return False

    def cold_done(self, seq: int | None) -> list[tuple[dict, bytes]]:
        """A cold message with `seq` has fully applied. Returns the parked
        fast messages now admissible, in arrival order."""
        if seq is not None and seq > self.cold_applied:
            self.cold_applied = seq
        due = [(h, p) for a, h, p in self._parked if a <= self.cold_applied]
        if due:
            self._parked = [(a, h, p) for a, h, p in self._parked if a > self.cold_applied]
        return due

    def fast_done(self, seq: int | None) -> None:
        """A fast message with `seq` has applied (parked ones included, when
        they are released). `fast_applied` advances only while contiguous:
        a hole holds it, which is what the host reads as "re-assert from
        here"."""
        if seq is None or seq <= self.fast_applied:
            return
        self._fast_done.add(int(seq))
        while self.fast_applied + 1 in self._fast_done:
            self.fast_applied += 1
            self._fast_done.discard(self.fast_applied)

    def clear_fast_gap(self) -> None:
        """The host re-asserted everything from the hole on: the hole is
        closed and applied catches up with seen."""
        self.fast.clear_gap()
        if self.fast.last_seen is not None and self.fast.last_seen > self.fast_applied:
            self.fast_applied = self.fast.last_seen
        self._fast_done = {s for s in self._fast_done if s > self.fast_applied}

    def clear_cold_gap(self) -> None:
        self.cold.clear_gap()

    def reset(self) -> None:
        self.cold = SeqTracker()
        self.fast = SeqTracker()
        self.cold_applied = 0
        self.fast_applied = 0
        self._fast_done.clear()
        self._parked.clear()
