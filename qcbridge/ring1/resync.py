"""Delta resync planning (pure: no bpy, no transport).

A cold-lane hole used to cost the whole file. Since 0.3 the replica keeps
an inventory of what it holds — {uuid: digest} of every blob it applied,
plus the manifest the host sends as it hashes what the bootstrap carried
(DESIGN-NOTES sync delta-resync; Octane's engine-data cache keyed by name
and hash). Given the two inventories, the plan is the set difference:
tombstones for what only the replica has, blobs for what differs or the
replica lacks, a verify pass for what the host has not hashed yet, and a
full Scene tier-1 snapshot. Scene STRUCTURE crosses only in the file, so a
scene-digest mismatch means a bootstrap after all.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def manifest_digest(held: dict[str, str]) -> str:
    """One hash over an inventory, order-independent; the pong carries it so
    the host can tell "nothing changed on the replica" without the manifest."""
    h = hashlib.blake2b(digest_size=16)
    for uuid, digest in sorted(held.items()):
        h.update(uuid.encode())
        h.update(b"=")
        h.update(str(digest).encode())
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class DeltaPlan:
    tombs: list[str] = field(default_factory=list)    # replica holds, host lacks
    resend: list[str] = field(default_factory=list)   # host digest known and != replica's, or replica lacks
    verify: list[str] = field(default_factory=list)   # host digest unknown (unprimed): hash now, send on mismatch
    scene_t1: bool = True                              # full TRACKED SCENE snapshot
    at_state: list[str] = field(default_factory=list) # uuids whose "@" paths are re-sent in full

    @property
    def empty(self) -> bool:
        return not (self.tombs or self.resend or self.verify)

    def __len__(self) -> int:
        return len(self.tombs) + len(self.resend) + len(self.verify)


def plan_delta(
    host_alive: set[str] | frozenset[str],
    host_hash: dict[str, str | None],
    replica_held: dict[str, str],
    host_scene: str,
    replica_scene: str,
    gap_lane: str,
    at_state_all: list[str] | tuple[str, ...] = (),
) -> DeltaPlan | None:
    """None = the Scene's structure differs (or is unknown): bootstrap.

    `host_alive` — uuids with users on the host that have a tier-2 path;
    `host_hash` — the host's last-sent digests (None/absent = not primed);
    `replica_held` — the replica's inventory; `at_state_all` — every Object
    and Collection uuid, re-asserted when the fast lane may have lost "@"
    paths too (gap on both lanes or an unknown uuid)."""
    if not host_scene or not replica_scene or host_scene != replica_scene:
        return None
    plan = DeltaPlan()
    for uuid in sorted(replica_held.keys() - set(host_alive)):
        plan.tombs.append(uuid)
    for uuid in sorted(host_alive):
        theirs = replica_held.get(uuid)
        ours = host_hash.get(uuid)
        if theirs is None:
            plan.resend.append(uuid)
        elif ours is None:
            plan.verify.append(uuid)
        elif str(ours) != str(theirs):
            plan.resend.append(uuid)
    if gap_lane == "cold":
        plan.at_state = sorted(set(plan.resend) | set(plan.verify))
    else:
        plan.at_state = sorted(set(at_state_all))
    return plan
