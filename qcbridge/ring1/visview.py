"""Per-viewport visibility (pure: no bpy, no transport).

Local View and per-viewport collection isolation live on the SpaceView3D,
not on any datablock, so no depsgraph event names them and no tier-2 blob
carries them. The host samples a signature of its largest 3D view and sends
it on the keyed hot lane (latest-wins, re-asserted every second); the
replica turns the difference between what it shows and what the host
shows into a few operator calls (DESIGN-NOTES sync vis-signature;
Octane's visible-set signature for empty depsgraph batches).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

KEY = b"vis"


@dataclass(frozen=True)
class VisSig:
    local: tuple[str, ...] = ()        # uuids in Local View (sorted); empty = not in Local View
    local_colls: bool = False          # SpaceView3D.use_local_collections
    hidden_lc: tuple[str, ...] = ()    # collection uuids hidden in THIS viewport only (sorted)

    @classmethod
    def make(cls, local=(), local_colls=False, hidden_lc=()) -> "VisSig":
        return cls(tuple(sorted(set(local))), bool(local_colls), tuple(sorted(set(hidden_lc))))


def encode(sig: VisSig) -> bytes:
    return json.dumps({"l": list(sig.local), "c": sig.local_colls, "h": list(sig.hidden_lc)},
                      separators=(",", ":")).encode("utf-8")


def decode(data: bytes | None) -> VisSig | None:
    if not data:
        return None
    try:
        d = json.loads(data.decode("utf-8"))
        return VisSig.make(d.get("l") or (), d.get("c") or False, d.get("h") or ())
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class VisPlan:
    exit: bool = False                      # leave Local View
    enter_with: tuple[str, ...] = ()        # enter Local View with exactly these
    add: tuple[str, ...] = ()               # already in Local View: add these
    remove: tuple[str, ...] = ()            # already in Local View: drop these
    set_local_colls: bool | None = None     # flip use_local_collections, or leave it
    lc_hide: tuple[str, ...] = ()           # collections to hide in this viewport
    lc_show: tuple[str, ...] = ()           # collections to show again

    @property
    def empty(self) -> bool:
        return not (self.exit or self.enter_with or self.add or self.remove
                    or self.set_local_colls is not None or self.lc_hide or self.lc_show)


def plan(have: VisSig, want: VisSig) -> VisPlan:
    """The operator calls that take `have` to `want`; empty when equal."""
    exit_ = bool(have.local) and not want.local
    enter = want.local if (want.local and not have.local) else ()
    add = remove = ()
    if want.local and have.local:
        add = tuple(sorted(set(want.local) - set(have.local)))
        remove = tuple(sorted(set(have.local) - set(want.local)))
    set_lc = None if have.local_colls == want.local_colls else want.local_colls
    if want.local_colls:
        lc_hide = tuple(sorted(set(want.hidden_lc) - set(have.hidden_lc)))
        lc_show = tuple(sorted(set(have.hidden_lc) - set(want.hidden_lc)))
    else:
        lc_hide = ()
        lc_show = tuple(sorted(have.hidden_lc)) if have.local_colls else ()
    return VisPlan(exit_, enter, add, remove, set_lc, lc_hide, lc_show)
