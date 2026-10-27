"""Stream parameters (pure): what the encoder helper is told and what the
hello's `stream` descriptor advertises. The wire itself is fixed
(wire_format): HEVC Main10 4:2:0, PQ with scene 1.0 = ANCHOR_NITS, BT.2020
container — only the rate, the frame size cap and the frame rate are knobs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .wire_format import ANCHOR_NITS

DEFAULT_BITRATE_MBPS = 50
DEFAULT_WIDTH = 2560
DEFAULT_FPS = 30

# The descriptor QCView and the host panel read; additive next to the
# pre-0.3 keys (enabled/port/latency_ms).
WIRE_FORMAT = {
    "codec": "hevc", "profile": "main10", "chroma": "420",
    "transfer": "pq", "primaries": "bt2020", "matrix": "bt2020nc",
    "range": "limited", "anchor_nits": ANCHOR_NITS, "source": "linear",
}


@dataclass(frozen=True)
class StreamParams:
    bitrate_mbps: int = DEFAULT_BITRATE_MBPS
    width: int = DEFAULT_WIDTH   # the camera frame is fitted to at most this many px wide
    fps: int = DEFAULT_FPS

    @classmethod
    def from_prefs(cls, prefs) -> "StreamParams":
        def _int(name, default, lo, hi):
            try:
                return max(lo, min(hi, int(getattr(prefs, name, default))))
            except (TypeError, ValueError):
                return default
        width = _int("stream_width", DEFAULT_WIDTH, 640, 7680) & ~1
        return cls(
            bitrate_mbps=_int("stream_bitrate_mbps", DEFAULT_BITRATE_MBPS, 5, 400),
            width=width,
            fps=_int("stream_fps", DEFAULT_FPS, 5, 120),
        )

    def helper_argv(self, helper: str, ring_name: str) -> list[str]:
        """The encoder helper's command line. GOP = one second, so a viewer
        joining the listener waits at most that for a key."""
        return [helper, "--shm", ring_name, "--fps", str(self.fps),
                "--bitrate", str(self.bitrate_mbps), "--gop", str(self.fps)]

    def descriptor(self) -> dict:
        return {"format": dict(WIRE_FORMAT), "width": self.width, "fps": self.fps,
                "bitrate_mbps": self.bitrate_mbps}
