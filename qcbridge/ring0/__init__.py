"""Ring 0 — the bpy adapter (docs/decisions.md #11).

Deliberately thin and dumb: handlers, applies, capture supervision, UI glue.
Everything with logic in it belongs in ring1, where it can be tested without
Blender. On the Replica, everything touching bpy runs on the main thread;
everything else runs off-thread and communicates through queues — no third
category (docs/architecture.md).
"""
