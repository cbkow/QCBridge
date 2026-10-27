"""Stream parameters (pure): what the encoder helper is told and what the
hello's `stream` descriptor advertises. The wire itself is fixed
(wire_format): HEVC Main10 4:2:0, PQ with scene 1.0 = ANCHOR_NITS, BT.2020
container — only the rate, the frame size cap and the frame rate are knobs.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .colour_state import M_709_TO_XYZ, working_to_2020
from .wire_format import ANCHOR_NITS, M_709_TO_2020, matrix_arg

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
    # The working space the wire is converted FROM (2026-10-27): its name,
    # its matrix to CIE XYZ D65, the derived working → BT.2020 matrix the
    # helper applies, and whether it was read from the config or assumed
    # (no PyOpenColorIO). None = the pre-awareness default, Linear Rec.709.
    working_space: str = ""
    working_to_xyz: tuple | None = None
    matrix: tuple | None = None
    working_assumed: bool = False

    def with_working(self, working: dict | None) -> "StreamParams":
        """From the colour-state block's `working` entry."""
        working = working or {}
        to_xyz = working.get("to_xyz")
        if not to_xyz:
            return replace(self, working_space=working.get("name") or "Linear Rec.709",
                           working_to_xyz=tuple(tuple(r) for r in M_709_TO_XYZ),
                           matrix=tuple(tuple(r) for r in M_709_TO_2020), working_assumed=True)
        to_xyz = tuple(tuple(float(v) for v in row) for row in to_xyz)
        return replace(self, working_space=working.get("name") or "",
                       working_to_xyz=to_xyz, matrix=working_to_2020(to_xyz),
                       working_assumed=bool(working.get("assumed")))

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
        argv = [helper, "--shm", ring_name, "--fps", str(self.fps),
                "--bitrate", str(self.bitrate_mbps), "--gop", str(self.fps)]
        if self.matrix is not None:
            argv += ["--matrix", matrix_arg(self.matrix)]
        return argv

    def descriptor(self) -> dict:
        fmt = dict(WIRE_FORMAT)
        if self.matrix is not None:
            fmt["working"] = {
                "name": self.working_space,
                "to_xyz": [list(r) for r in (self.working_to_xyz or ())],
                "to_bt2020": [list(r) for r in self.matrix],
                "assumed": self.working_assumed,
            }
        return {"format": fmt, "width": self.width, "fps": self.fps,
                "bitrate_mbps": self.bitrate_mbps}
