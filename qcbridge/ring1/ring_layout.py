"""Frame ring layout: the shared-memory block Blender writes and the encode
helper reads (pure: struct formats and offsets only — ring0/frame_ring.py
owns the mapping, numpy and bpy).

One block = a header page, then `slot_count` slots. Each slot is a
descriptor page followed by a page-aligned pixel region whose rows are
padded to 256 bytes, so a Metal `bytesNoCopy` buffer or a D3D11 upload can
address the pixels straight from the mapping. The discipline is the one
QCView's After Effects ring uses (shared_ring.h in QCView-Player): a seqlock
per slot (odd while the writer is inside, even when published), `latest =
(seq << 8) | slot` (the slot is never derived from the seq), and
`reader_claim` so the writer steps over the slot the reader is copying.

Python cannot fence; every control word is one aligned 8-byte store, and
the reader re-validates the seqlock after its copy, so weak ordering costs a
retry, never a torn frame (DESIGN-NOTES pixels ring).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"QCBF"
VERSION = 1
PAGE = 4096
ROW_ALIGN = 256
DEFAULT_SLOTS = 3
NAME_MAX = 30          # macOS caps POSIX shm names at 31 incl. the "/" Python adds

# Pixel formats (the helper reads pixel_format, never assumes).
PF_RGBA32F = 4

# Slot flags.
F_CAMERA_RECT = 1       # the frame is the camera rectangle (shot mode), not the region
F_PROBE_STAMPED = 2     # the latency probe strip is stamped bottom-left
F_OFFSCREEN_ENGINE = 4  # EEVEE/Workbench offscreen draw, not a Cycles readback
F_FIRST_AFTER_LOAD = 8  # first frame after a bootstrap applied
F_WARMUP = 16           # start-up black (kernels compiling, textures loading): shown as black, not skipped

# States.
STATE_LIVE = 0
STATE_RETIRED = 1       # the producer re-created the ring under a new name

# magic, version, slot_count, producer_pid, slot_stride, pixels_offset
# (inside a slot), pixels_capacity, total_size, latest, reader_claim, state,
# t_created
HEADER_FMT = "<4sIIIQQQQQQId"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
LATEST_OFFSET = struct.calcsize("<4sIIIQQQQ")   # where `latest` sits (8-byte aligned)
READER_CLAIM_OFFSET = LATEST_OFFSET + 8
STATE_OFFSET = READER_CLAIM_OFFSET + 8

# seqlock, w, h, bytes_per_row, pixel_format, flags, frame_number,
# t_read_start, t_read_end, t_publish, frame_seq, probe_seq, probe_t_host
DESC_FMT = "<QIIIIIIIdddQId"
DESC_SIZE = struct.calcsize(DESC_FMT)
SEQLOCK_OFFSET = 0

assert LATEST_OFFSET % 8 == 0 and READER_CLAIM_OFFSET % 8 == 0
assert HEADER_SIZE <= PAGE and DESC_SIZE <= PAGE


def round_up(n: int, to: int) -> int:
    return (n + to - 1) // to * to


def bytes_per_row(width: int, bytes_per_pixel: int = 16) -> int:
    return round_up(width * bytes_per_pixel, ROW_ALIGN)


def capacity_for(width: int, height: int, bytes_per_pixel: int = 16) -> int:
    """Pixel bytes a slot must hold for a width×height frame, page-rounded."""
    return round_up(bytes_per_row(width, bytes_per_pixel) * height, PAGE)


def height_cap(width: int) -> int:
    """The tallest frame a ring sized for `width` accepts: 4:3 of the width
    (a camera rectangle is rarely taller; a taller one retires the ring)."""
    return round_up(width * 3 // 4, 2)


@dataclass(frozen=True)
class Layout:
    slot_count: int
    pixels_capacity: int   # per slot, page-rounded
    slot_stride: int       # desc page + pixels

    @property
    def pixels_offset(self) -> int:
        return PAGE  # inside a slot: after the descriptor page

    @property
    def total_size(self) -> int:
        return PAGE + self.slot_count * self.slot_stride

    def slot_offset(self, slot: int) -> int:
        return PAGE + slot * self.slot_stride

    def pixels_at(self, slot: int) -> int:
        return self.slot_offset(slot) + self.pixels_offset

    def fits(self, width: int, height: int, bytes_per_pixel: int = 16) -> bool:
        return bytes_per_row(width, bytes_per_pixel) * height <= self.pixels_capacity


def layout_for(width: int, height: int | None = None, slots: int = DEFAULT_SLOTS) -> Layout:
    """A layout whose slots hold width × (height or height_cap(width))."""
    cap = capacity_for(width, height if height is not None else height_cap(width))
    return Layout(slot_count=slots, pixels_capacity=cap, slot_stride=PAGE + cap)


def ring_name(pid: int, gen: int) -> str:
    name = f"qcb-{pid}-{gen}"
    assert len(name) <= NAME_MAX
    return name


def pack_header(layout: Layout, pid: int, t_created: float) -> bytes:
    return struct.pack(
        HEADER_FMT, MAGIC, VERSION, layout.slot_count, pid, layout.slot_stride,
        layout.pixels_offset, layout.pixels_capacity, layout.total_size,
        0, 0, STATE_LIVE, t_created,
    )


def unpack_header(buf) -> dict:
    (magic, version, slot_count, pid, slot_stride, pixels_offset, pixels_capacity,
     total_size, latest, reader_claim, state, t_created) = struct.unpack_from(HEADER_FMT, buf, 0)
    return {
        "magic": magic, "version": version, "slot_count": slot_count, "producer_pid": pid,
        "slot_stride": slot_stride, "pixels_offset": pixels_offset,
        "pixels_capacity": pixels_capacity, "total_size": total_size,
        "latest": latest, "reader_claim": reader_claim, "state": state, "t_created": t_created,
    }


def pack_latest(seq: int, slot: int) -> int:
    return (int(seq) << 8) | (int(slot) & 0xFF)


def unpack_latest(latest: int) -> tuple[int, int]:
    return latest >> 8, latest & 0xFF


def pack_desc(
    seqlock: int, w: int, h: int, bpr: int, pixel_format: int, flags: int, frame_number: int,
    t_read_start: float, t_read_end: float, t_publish: float, frame_seq: int,
    probe_seq: int = 0, probe_t_host: float = 0.0,
) -> bytes:
    return struct.pack(
        DESC_FMT, seqlock, w, h, bpr, pixel_format, flags, frame_number, 0,
        t_read_start, t_read_end, t_publish, frame_seq, probe_seq, probe_t_host,
    )


def unpack_desc(buf, offset: int = 0) -> dict:
    (seqlock, w, h, bpr, pf, flags, frame_number, _pad, t0, t1, t2, frame_seq,
     probe_seq, probe_t_host) = struct.unpack_from(DESC_FMT, buf, offset)
    return {
        "seqlock": seqlock, "w": w, "h": h, "bytes_per_row": bpr, "pixel_format": pf,
        "flags": flags, "frame_number": frame_number, "t_read_start": t0, "t_read_end": t1,
        "t_publish": t2, "frame_seq": frame_seq, "probe_seq": probe_seq,
        "probe_t_host": probe_t_host,
    }


def next_slot(last_slot: int, reader_claim: int, slot_count: int) -> int:
    """The slot after `last_slot`, stepping over the one the reader holds.
    `reader_claim` is 0 when nothing is held, else slot + 1."""
    held = reader_claim - 1 if reader_claim else None
    slot = (last_slot + 1) % slot_count
    if slot == held:
        slot = (slot + 1) % slot_count
    return slot
