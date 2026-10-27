"""Host-side log of what left on the fast lane (pure: no bpy, no transport).

The fast lane is latest-wins per datablock and carries no acknowledgement
by itself; the replica's pong says how far it has APPLIED (`fast_applied`)
and where the first hole is (`fast_gap_at`). When a hole is reported, the
host does not need the lost message back — it needs the current state of
every datablock named at or after the hole. This log remembers which uuids
those were, bounded, so the re-assert is a handful of full tier-1
snapshots instead of a whole-file bootstrap (DESIGN-NOTES sync fast-ack;
the idea is Octane's "advance the camera shadow only on echo").
"""
from __future__ import annotations

from collections import deque


class FastLog:
    def __init__(self, capacity: int = 4096) -> None:
        self._entries: deque[tuple[int, str, str]] = deque(maxlen=capacity)

    def record(self, seq: int, uuid: str, kind: str) -> None:
        self._entries.append((int(seq), uuid, kind))

    @property
    def oldest_seq(self) -> int | None:
        return self._entries[0][0] if self._entries else None

    @property
    def newest_seq(self) -> int | None:
        return self._entries[-1][0] if self._entries else None

    def covers(self, seq: int | None) -> bool:
        """True when a hole at `seq` is inside what this log still remembers."""
        return seq is not None and self.oldest_seq is not None and seq >= self.oldest_seq

    def window_since(self, seq: int) -> list[tuple[str, str]]:
        """(uuid, kind) for every message with seq >= `seq`: the LAST kind
        per uuid wins (a tombstone after a delta means gone; a delta after a
        tombstone means re-created), in first-seen order."""
        last: dict[str, str] = {}
        for s, uuid, kind in self._entries:
            if s >= seq:
                last[uuid] = kind
        return list(last.items())

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
