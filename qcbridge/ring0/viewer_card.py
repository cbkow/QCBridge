"""The viewer card: how QCView on this machine learns about the Blender
stream without being launched by Blender (2026-10-28, DESIGN-NOTES pixels
viewer card). The host add-on writes one JSON file into QCView's Inputs
folder — the same folder rule as QCView's own output manifests
(`src/window/output_bus.cpp manifestDirs`): ProgramData on Windows (never
virtualized, readable by the Store build), Application Support on macOS.
QCView watches the folder, listens on the card's port, and applies the
colour chain from the card's keys. The card is rewritten when anything in
it changes (the host's view, the replica's working space, the mismatch
rows) and removed when the session stops.

Same-machine by design: the AE/Premiere bridges hand off through shared
memory on one machine; this is the file-shaped equivalent for a stream
that crosses the network.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time

CARD_VERSION = 1
CARD_NAME = "blender.json"


def inputs_dir() -> str:
    """QCView's Inputs folder on this machine (created on demand)."""
    env = os.environ.get("QCV_INPUTS_DIR", "")
    if env:
        return env
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/QCView/Inputs")
    if sys.platform == "win32":
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        return os.path.join(pd, "QCView", "Inputs")
    return os.path.expanduser("~/.local/share/QCView/Inputs")


def card_path() -> str:
    return os.path.join(inputs_dir(), CARD_NAME)


def listener_url(port: int, latency_ms: int, passphrase: str) -> str:
    """What QCView opens: an SRT listener on every interface, the
    passphrase the replica's caller carries (the same derivation as the
    legacy link, ring1/protocol.srt_passphrase)."""
    url = f"srt://0.0.0.0:{int(port)}?mode=listener&latency={int(latency_ms) * 1000}"
    if passphrase:
        url += f"&passphrase={passphrase}&pbkeylen=16"
    return url


def build(*, name: str, port: int, latency_ms: int, passphrase: str, colour: dict,
          rows: list, mismatches: list, stream: dict | None, note: str, host_name: str = "") -> dict:
    return {
        "v": CARD_VERSION,
        "kind": "blender",
        "name": name,
        "host": host_name or platform.node(),
        "url": listener_url(port, latency_ms, passphrase),
        "port": int(port),
        "latency_ms": int(latency_ms),
        "status": note,
        # The colour chain for QCView: the SAME keys the qcview://stream
        # link carried (ring1/colour_state.deep_link_params), as a dict.
        "colour": dict(colour or {}),
        "rows": [list(r) for r in (rows or [])],
        "mismatches": [list(m) for m in (mismatches or [])],
        "stream": dict(stream or {}),
        "ts": time.time(),
    }


def content_digest(card: dict) -> str:
    """Everything but the timestamp: rewrite only when this moves."""
    import hashlib
    body = {k: v for k, v in card.items() if k != "ts"}
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def write(card: dict) -> str:
    """Atomic replace; returns the path. Raises OSError when the folder
    cannot be made or written (the caller reports it, once)."""
    d = inputs_dir()
    os.makedirs(d, exist_ok=True)
    path = card_path()
    fd, tmp = tempfile.mkstemp(prefix=".blender-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=1, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def remove() -> bool:
    try:
        os.unlink(card_path())
        return True
    except OSError:
        return False
