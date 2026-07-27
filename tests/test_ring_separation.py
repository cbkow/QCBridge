"""Enforce the ring model (docs/decisions.md #11, #14).

ring1 must be plain CPython: no Blender-only imports anywhere, zmq only in
transport_zmq.py, and every module must import cleanly outside Blender.
"""

import ast
import importlib
import pathlib
import sys

QCBRIDGE = pathlib.Path(__file__).resolve().parents[1] / "qcbridge"
RING1 = QCBRIDGE / "ring1"

BLENDER_ONLY = {"bpy", "bpy_extras", "bmesh", "mathutils", "gpu", "blf", "bgl", "aud"}
ZMQ_ALLOWED_IN = {"transport_zmq.py"}


def _ring1_files():
    files = sorted(RING1.glob("*.py"))
    assert files, f"no ring1 modules found under {RING1}"
    return files


def _top_level_imports(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_ring1_never_imports_blender_modules():
    for path in _ring1_files():
        hit = set(_top_level_imports(path)) & BLENDER_ONLY
        assert not hit, f"{path.name} imports Blender-only module(s): {sorted(hit)}"


def test_zmq_quarantined_to_transport_zmq():
    for path in _ring1_files():
        if path.name in ZMQ_ALLOWED_IN:
            continue
        hit = {m for m in _top_level_imports(path) if m in ("zmq", "pyzmq")}
        assert not hit, (
            f"{path.name} imports zmq — the wire library is quarantined to "
            f"{sorted(ZMQ_ALLOWED_IN)} (decision #14)"
        )


def test_ring1_imports_cleanly_outside_blender():
    # ring1 is loaded as its own top-level package so qcbridge/__init__.py
    # (which imports bpy) never runs. ring1-internal imports must be relative.
    sys.path.insert(0, str(QCBRIDGE))
    try:
        for path in _ring1_files():
            name = "ring1" if path.stem == "__init__" else f"ring1.{path.stem}"
            if path.stem == "transport_zmq":
                try:
                    import zmq  # noqa: F401
                except ImportError:
                    continue  # wheel only guaranteed inside Blender
            importlib.import_module(name)
    finally:
        sys.path.remove(str(QCBRIDGE))
