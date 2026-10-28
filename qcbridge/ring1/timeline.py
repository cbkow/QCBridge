"""Session timeline: one line per stage, on each end, so a slow start
explains itself without a debug build (2026-10-27: a live project took
three minutes to a picture and nothing said where the time went).

A session's `begin(role, path)` truncates the file and stamps the clock;
every `mark(stage, **detail)` appends one line:

    17:23:51.204 +0.000s session.start role=HOST addon=0.3.0 blender=5.2.2
    17:23:56.410 +5.206s boot.applied bytes=7366412 errors=0 unmapped=2 ...

The file lives in Blender's config dir (`qcbridge-timeline-<role>.log`);
the last `KEEP` lines stay in memory for the panel, the smokes and the
pong. Pure: no bpy. Never raises — a timeline must not cost a session.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

KEEP = 400

_lock = threading.Lock()
_t0: float | None = None
_path: str = ""
_role: str = ""
_lines: deque = deque(maxlen=KEEP)
_last: dict[str, str] = {}   # stage -> last detail string, for mark_changed


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_fmt(v) for v in value) + "]"
    s = str(value)
    return s.replace("\n", " ")[:300]


def begin(role: str, path: str, **detail) -> None:
    """Start a session's timeline: truncate the file, reset the clock."""
    global _t0, _path, _role
    with _lock:
        _t0 = time.monotonic()
        _path = path
        _role = role
        _lines.clear()
        _last.clear()
        try:
            if path:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
        except OSError:
            _path = ""
    mark("session.start", role=role, **detail)


def elapsed() -> float:
    return 0.0 if _t0 is None else time.monotonic() - _t0


def mark(stage: str, **detail) -> str:
    """Append one stage line (returns it). Safe from any thread."""
    t = elapsed()
    wall = time.strftime("%H:%M:%S", time.localtime()) + f".{int((time.time() % 1) * 1000):03d}"
    body = " ".join(f"{k}={_fmt(v)}" for k, v in detail.items())
    line = f"{wall} +{t:.3f}s {stage}" + (f" {body}" if body else "")
    with _lock:
        _lines.append(line)
        _last[stage] = body
        if _path:
            try:
                with open(_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    return line


def mark_changed(stage: str, **detail) -> bool:
    """mark() only when the detail differs from the last mark of that
    stage — for states polled on a timer (the peer's encoder status)."""
    body = " ".join(f"{k}={_fmt(v)}" for k, v in detail.items())
    with _lock:
        same = _last.get(stage) == body
    if same:
        return False
    mark(stage, **detail)
    return True


def tail(n: int = 40) -> list[str]:
    with _lock:
        return list(_lines)[-n:]


def path() -> str:
    return _path


def role() -> str:
    return _role


def reset() -> None:
    """Tests."""
    global _t0, _path, _role
    with _lock:
        _t0 = None
        _path = ""
        _role = ""
        _lines.clear()
        _last.clear()
