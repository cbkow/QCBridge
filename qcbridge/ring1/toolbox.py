"""ffmpeg resolution: preferences → QCView's toolbox.json → PATH.

QCView is the suite's ffmpeg provider (stage-2 decision): it writes a small
manifest of tool paths into its app-data dir at startup. The chain:

  1. an explicit path in the addon preferences (power-user override —
     if set but missing, that's an error, not a silent fallthrough);
  2. QCView's toolbox.json `ffmpeg` entry;
  3. plain `ffmpeg` on PATH;
  4. nothing — the caller surfaces it, streaming just doesn't start.

Sync-only mode never calls any of this.
"""

from __future__ import annotations

import json
import os
import shutil
import sys


def toolbox_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(base, "QCView", "toolbox.json") if base else ""
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/QCView/toolbox.json"
        )
    return os.path.expanduser("~/.local/share/QCView/toolbox.json")


def read_toolbox(path: str | None = None) -> dict:
    path = toolbox_path() if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _spawnable(path: str) -> bool:
    """A file existing is NOT enough on Windows: MSIX packages live under
    WindowsApps, whose ACLs deny direct Popen (WinError 5) — QCView's own
    install is exactly that. Resolution means "can actually run it", so
    verify with a real spawn. Costs ~100–300 ms; callers run resolution
    off the main thread."""
    import subprocess

    try:
        subprocess.run(
            [path, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_ffmpeg(
    pref_value: str = "",
    toolbox_file: str | None = None,
    which=shutil.which,
    spawnable=_spawnable,
) -> tuple[str | None, str]:
    """Returns (ffmpeg_path | None, source) — source is one of
    "preferences", "qcview", "path", or a human-readable failure reason.
    Every rung is spawn-verified; an unspawnable candidate falls through."""
    pref = (pref_value or "").strip()
    if pref and pref != "ffmpeg":  # "ffmpeg" was the old bare default
        if os.path.isfile(pref) and spawnable(pref):
            return pref, "preferences"
        if os.path.isfile(pref):
            return None, f"preferences ffmpeg exists but won't spawn: {pref}"
        return None, f"preferences path missing: {pref}"
    qcview = read_toolbox(toolbox_file).get("ffmpeg", "")
    if qcview and os.path.isfile(qcview) and spawnable(qcview):
        return qcview, "qcview"
    found = which("ffmpeg")
    if found and spawnable(found):
        return found, "path"
    if qcview or found:
        return None, "found but not spawnable (MSIX ACL?) — set a path in preferences"
    return None, "not found — set a path in preferences or install QCView"
