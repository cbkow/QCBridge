"""Ring 1 — sync logic (docs/decisions.md #11).

HARD RULE: no module in this package may import bpy (or any Blender-only
module: bmesh, mathutils, gpu, blf). Enforced by tests/test_ring_separation.py.
Only transport_zmq.py may import zmq (decision #14 quarantine).

Everything here is importable and testable under plain CPython.
"""
