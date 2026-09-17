"""Motion-to-photon probe (parity spike S0) — env-gated, off by default.

The host stamps each hot packet with its wall clock + a sequence number; the
replica burns the latest stamp into a strip of black/white blocks; a reader
on the HOST machine decodes the strip and subtracts. QCView always runs next
to the host, so both timestamps come from one machine clock in every OS
pairing — no clock sync.

Strip layout (one row of square blocks, white = 1):
  8-bit marker | 32-bit host ms (mod 2**32) | 16-bit seq | 8-bit check
Blocks are large on purpose: they must survive 4:2:0 HEVC at any rung.

Knobs (environment, read once at session start):
  QCB_PROBE=1          stamp hot packets / draw the strip
  QCB_HOT_HZ=60        host hot-sampler rate (default 30)
  QCB_CAPTURE_FPS=60   replica capture rate (default 30)
  QCB_PROBE_ORBIT=45   host view orbit, degrees/second (0 = off)
"""

from __future__ import annotations

import os

MARKER = (1, 0, 1, 1, 0, 0, 1, 0)
DATA_BITS = 32 + 16 + 8
TOTAL_BITS = len(MARKER) + DATA_BITS
BLOCK_PX = 12


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def enabled() -> bool:
    return os.environ.get("QCB_PROBE") == "1"


def hot_hz() -> float:
    return max(1.0, _env_float("QCB_HOT_HZ", 30.0))


def capture_fps() -> int:
    return max(1, int(_env_float("QCB_CAPTURE_FPS", 30.0)))


def orbit_deg_per_s() -> float:
    return _env_float("QCB_PROBE_ORBIT", 0.0)


def _check(t_ms: int, seq: int) -> int:
    data = t_ms.to_bytes(4, "little") + seq.to_bytes(2, "little")
    return (sum(data) ^ 0xA5) & 0xFF


def encode_bits(t_host: float, seq: int) -> list[int]:
    t_ms = int(t_host * 1000) & 0xFFFFFFFF
    seq &= 0xFFFF
    word = t_ms | (seq << 32) | (_check(t_ms, seq) << 48)
    return list(MARKER) + [(word >> i) & 1 for i in range(DATA_BITS)]


def decode_bits(bits: list[int]) -> tuple[int, int] | None:
    """(host ms mod 2**32, seq) or None if the marker/check fails."""
    if len(bits) != TOTAL_BITS or tuple(bits[: len(MARKER)]) != MARKER:
        return None
    word = 0
    for i, b in enumerate(bits[len(MARKER):]):
        word |= (b & 1) << i
    t_ms = word & 0xFFFFFFFF
    seq = (word >> 32) & 0xFFFF
    if (word >> 48) & 0xFF != _check(t_ms, seq):
        return None
    return t_ms, seq


def sample_row(row: bytes, x0: int, block: int = BLOCK_PX, threshold: int = 128) -> list[int]:
    """Read TOTAL_BITS block centers from one luma row starting at x0."""
    half = block // 2
    return [1 if row[x0 + i * block + half] >= threshold else 0 for i in range(TOTAL_BITS)]


def find_strip(row: bytes, block: int = BLOCK_PX) -> int | None:
    """x0 of a strip whose marker AND check decode on this row, else None."""
    span = TOTAL_BITS * block
    half = block // 2
    for x0 in range(0, len(row) - span + 1):
        if any(
            (row[x0 + i * block + half] >= 128) != bool(m)
            for i, m in enumerate(MARKER)
        ):
            continue
        if decode_bits(sample_row(row, x0, block)) is not None:
            return x0
    return None


def latency_ms(now_s: float, t_ms: int) -> int:
    """Elapsed ms between a decoded stamp and `now` on the same clock,
    unwrapping the 32-bit ms counter (valid for gaps < ~24 days)."""
    return ((int(now_s * 1000) & 0xFFFFFFFF) - t_ms) & 0xFFFFFFFF
