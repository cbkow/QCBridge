"""Shared bits for the parity spike scripts (plain CPython, no Blender)."""

from __future__ import annotations

import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "qcbridge"))
from ring1 import probe, protocol, toolbox  # noqa: E402,F401


def resolve_ffmpeg(explicit: str = "") -> str:
    path, source = toolbox.resolve_ffmpeg(explicit)
    if path is None:
        sys.exit(f"no ffmpeg: {source}")
    print(f"ffmpeg: {path} ({source})", file=sys.stderr, flush=True)
    return path


def srt_url(host: str, port: int, mode: str, latency_ms: int, token: str) -> str:
    url = f"srt://{host}:{port}?mode={mode}&latency={latency_ms * 1000}"
    if token:
        url += f"&passphrase={protocol.srt_passphrase(token)}&pbkeylen=16"
    return url


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


_ = shutil  # keep import for callers that probe PATH themselves
