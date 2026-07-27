"""The host's cold-side state: a dirty set, not an event queue (decision #16).

Last-value-register philosophy at datablock granularity: backlog is bounded
by scene size by construction. Entries are `uuid → dirty(tier) | tombstone`;
the tier is a one-way ratchet per entry (a tier-1 dirty upgraded by a
structural edit flushes as tier-2) and clears on drain.

Deletion bookkeeping: a tombstone is only worth sending if the peer may have
heard of the uuid (it was drained at least once this session). Create-then-
delete while disconnected collapses to nothing.
"""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    T1 = 1  # property deltas
    T2 = 2  # datablock resend


_TOMBSTONE = -1


class DirtySet:
    def __init__(self) -> None:
        self._entries: dict[str, int] = {}  # uuid → Tier value or _TOMBSTONE
        self._sent: set[str] = set()        # uuids the peer may know

    def mark(self, uuid: str, tier: Tier) -> None:
        current = self._entries.get(uuid)
        if current == _TOMBSTONE:
            return  # deleted stays deleted; a recreated datablock has a fresh uuid
        self._entries[uuid] = max(current or 0, int(tier))

    def mark_deleted(self, uuid: str) -> None:
        if uuid not in self._sent:
            self._entries.pop(uuid, None)  # peer never heard of it: collapse
            return
        self._entries[uuid] = _TOMBSTONE

    def drain(self) -> tuple[list[str], list[tuple[str, Tier]]]:
        """Take everything: (tombstones, dirty) — tombstones flush first
        (frees names/memory on the replica before arrivals). Clears the set;
        drained uuids join the peer-may-know set (tombstoned ones leave it)."""
        tombstones = []
        dirty = []
        for uuid, value in self._entries.items():
            if value == _TOMBSTONE:
                tombstones.append(uuid)
                self._sent.discard(uuid)
            else:
                dirty.append((uuid, Tier(value)))
                self._sent.add(uuid)
        self._entries.clear()
        return tombstones, dirty

    def requeue(self, uuid: str, tier: Tier) -> None:
        """A drained entry whose send failed (backpressure/peer gone) goes
        back in — same ratchet rules apply if it re-dirtied meanwhile."""
        self.mark(uuid, tier)

    def assume_known(self, uuid: str) -> None:
        """Seed the peer-may-know set: datablocks present at session start
        are known to the peer via bootstrap/shared state, so their deletion
        must tombstone even if never drained this session."""
        self._sent.add(uuid)

    def requeue_tombstone(self, uuid: str) -> None:
        """A drained tombstone whose send failed goes back as a tombstone
        (drain removed the uuid from the peer-may-know set, so mark_deleted
        would wrongly collapse it)."""
        self._entries[uuid] = _TOMBSTONE
        self._sent.add(uuid)

    def peer_forgot_everything(self) -> None:
        """Epoch changed → full bootstrap will run; tombstones for the old
        session are meaningless."""
        self._sent.clear()
        self._entries = {
            u: v for u, v in self._entries.items() if v != _TOMBSTONE
        }

    def __len__(self) -> int:
        return len(self._entries)

    def counts(self) -> tuple[int, int, int]:
        """(t1, t2, tombstones) — for the status surfaces."""
        t1 = sum(1 for v in self._entries.values() if v == Tier.T1)
        t2 = sum(1 for v in self._entries.values() if v == Tier.T2)
        tomb = sum(1 for v in self._entries.values() if v == _TOMBSTONE)
        return t1, t2, tomb
