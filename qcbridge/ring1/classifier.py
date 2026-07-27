"""Change classification + per-datablock debounce (sync-protocol.md tiers).

The classifier maps every edit to the cheapest sufficient tier and escalates
when unsure: a recognized non-structural update diffs against the shadow
(tier 1); anything structural or unrecognized ratchets to tier 2 in the
dirty set. The debouncer coalesces slider drags: a datablock flushes only
after being left alone for the debounce window.
"""

from __future__ import annotations

from .dirtyset import Tier

DEBOUNCE_S = 0.15


class Debouncer:
    """uuid → last-touched time; ready = untouched for the window."""

    def __init__(self, window: float = DEBOUNCE_S) -> None:
        self._window = window
        self._touched: dict[str, float] = {}

    def touch(self, uuid: str, now: float) -> None:
        self._touched[uuid] = now

    def ready(self, now: float) -> list[str]:
        due = [u for u, t in self._touched.items() if now - t >= self._window]
        for uuid in due:
            del self._touched[uuid]
        return due

    def drop(self, uuid: str) -> None:
        self._touched.pop(uuid, None)

    def __len__(self) -> int:
        return len(self._touched)


def classify_update(type_key: str | None, is_geometry: bool) -> Tier:
    """Tier for one depsgraph update, from what ring0 observed.

    type_key — shadow.TRACKED key if the datablock type is recognized for
    tier-1 diffing, else None (→ tier 2).

    NOTE the depsgraph's is_updated_geometry flag is deliberately IGNORED for
    recognized types: measured on 5.2 it fires on a light-energy change and
    even a scene-exposure change — useless as a structural signal. Structural
    changes on recognized types are instead detected from the snapshot diff
    (path-set changes and "~" pseudo-paths — see shadow.py/host_handlers) and
    escalated there. The parameter stays for tier-2-only types, where any
    update is a resend regardless.
    """
    del is_geometry
    return Tier.T2 if type_key is None else Tier.T1
