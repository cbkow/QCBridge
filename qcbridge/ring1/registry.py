"""Datablock identity registry (sync-protocol.md §Datablock identity).

The wire keys on UUIDs, never names. Blender gives datablocks no stable UUID,
so the Host stamps one as a custom property at first contact (ring0/identity.py
does the stamping; this module owns the bookkeeping, which has one real job:
catching duplicates — duplicating a datablock copies its custom properties, so
two live datablocks can carry the same stamp until the copy is restamped).

`session_uid` is Blender's runtime-unique int per datablock (stable for the
process lifetime, never reused within a session) — the collision discriminator.
"""

from __future__ import annotations


class IdentityRegistry:
    """uuid ↔ session_uid bookkeeping for one role's session."""

    def __init__(self) -> None:
        self._uuid_to_suid: dict[str, int] = {}
        self._suid_to_uuid: dict[int, str] = {}

    def claim(self, uuid: str, session_uid: int) -> str:
        """Register `uuid` as carried by the datablock with `session_uid`.

        Returns:
          "ok"        — first contact, or re-seen unchanged.
          "collision" — a *different* live datablock already carries this
                        uuid (a duplicate); the caller must restamp the
                        newcomer with a fresh uuid and claim again.
        """
        holder = self._uuid_to_suid.get(uuid)
        if holder is None:
            self._register(uuid, session_uid)
            return "ok"
        if holder == session_uid:
            return "ok"
        return "collision"

    def forget_uuid(self, uuid: str) -> None:
        suid = self._uuid_to_suid.pop(uuid, None)
        if suid is not None:
            self._suid_to_uuid.pop(suid, None)

    def uuid_for(self, session_uid: int) -> str | None:
        return self._suid_to_uuid.get(session_uid)

    def session_uid_for(self, uuid: str) -> int | None:
        return self._uuid_to_suid.get(uuid)

    def __len__(self) -> int:
        return len(self._uuid_to_suid)

    def _register(self, uuid: str, session_uid: int) -> None:
        # A datablock re-stamped with a fresh uuid keeps its session_uid;
        # drop any stale reverse entry so the maps stay consistent.
        old_uuid = self._suid_to_uuid.get(session_uid)
        if old_uuid is not None and old_uuid != uuid:
            self._uuid_to_suid.pop(old_uuid, None)
        self._uuid_to_suid[uuid] = session_uid
        self._suid_to_uuid[session_uid] = uuid
