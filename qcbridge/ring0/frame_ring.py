"""The frame ring writer: Blender's side of the shared-memory handoff to the
encoder helper (layout in ring1/ring_layout.py; the helpers mirror it).

One block of POSIX / Windows shared memory: a header page and three slots.
A frame is published by copying it into the next free slot under the
slot's seqlock and then bumping `latest`; the helper picks the newest slot,
claims it so the writer steps over it, converts, and re-checks the seqlock.
Python has no fence, but each control word is one aligned 8-byte store, so
a torn frame is impossible and a weak-ordering race costs the helper one
retry (DESIGN-NOTES pixels ring).

Sized for `width × 0.75·width`; a taller frame retires the ring and a new
one is created under a new name (a Windows section still held by the
helper hands back the old object, so a fresh name is the only safe
replace). The pixel path restarts the helper on the new name.
"""
from __future__ import annotations

import os
import struct
import time
from multiprocessing import shared_memory

import numpy as np

from ..ring1 import probe, ring_layout as rl

_DEBUG = bool(os.environ.get("QCB_DEBUG"))
_gen = 0


class FrameRing:
    def __init__(self, width: int, slots: int = rl.DEFAULT_SLOTS) -> None:
        global _gen
        _gen += 1
        self.gen = _gen
        self.width = width
        self.layout = rl.layout_for(width, slots=slots)
        self.name = rl.ring_name(os.getpid(), self.gen)
        # Untracked: the resource tracker would unlink the block when THIS
        # process exits anyway (and log a KeyError on the way); we unlink in
        # close(). `track` exists since Python 3.13 (Blender 4.2+).
        try:
            self.shm = shared_memory.SharedMemory(name=self.name, create=True,
                                                  size=self.layout.total_size, track=False)
        except TypeError:
            self.shm = shared_memory.SharedMemory(name=self.name, create=True, size=self.layout.total_size)
            try:
                from multiprocessing import resource_tracker
                resource_tracker.unregister(self.shm._name, "shared_memory")  # noqa: SLF001
            except Exception:
                pass
        self.buf = self.shm.buf
        self.buf[: rl.HEADER_SIZE] = rl.pack_header(self.layout, os.getpid(), time.time())
        self.seq = 0
        self.last_slot = -1
        self.published = 0
        self.retired = False
        if _DEBUG:
            print(f"qcb ring {self.name}: {self.layout.total_size >> 20} MiB, {slots} slots", flush=True)

    # ── geometry ────────────────────────────────────────────────────────────

    def fits(self, width: int, height: int) -> bool:
        return self.layout.fits(width, height)

    # ── publish ─────────────────────────────────────────────────────────────

    def publish(self, arr: np.ndarray, *, flags: int = 0, frame_number: int = 0,
                t_read_start: float = 0.0, t_read_end: float = 0.0,
                probe_stamp: tuple[float, int] | None = None) -> bool:
        """`arr` — float32 (h, w, 4), rows bottom-up as read from the
        framebuffer. False when the frame does not fit (caller retires)."""
        if self.retired:
            return False
        h, w = int(arr.shape[0]), int(arr.shape[1])
        if not self.fits(w, h):
            return False
        reader_claim = struct.unpack_from("<Q", self.buf, rl.READER_CLAIM_OFFSET)[0]
        slot = rl.next_slot(self.last_slot, int(reader_claim), self.layout.slot_count)
        base = self.layout.slot_offset(slot)
        bpr = rl.bytes_per_row(w)
        # seqlock odd: the writer is inside
        seqlock = struct.unpack_from("<Q", self.buf, base + rl.SEQLOCK_OFFSET)[0] | 1
        struct.pack_into("<Q", self.buf, base + rl.SEQLOCK_OFFSET, seqlock)
        dst = np.ndarray((h, bpr // 4), dtype=np.float32, buffer=self.buf,
                         offset=self.layout.pixels_at(slot))
        np.copyto(dst[:, : w * 4], arr.reshape(h, w * 4))
        if probe_stamp is not None:
            self._stamp(dst, w, h, probe_stamp)
            flags |= rl.F_PROBE_STAMPED
        self.seq += 1
        desc = rl.pack_desc(
            seqlock + 1, w, h, bpr, rl.PF_RGBA32F, flags, frame_number,
            t_read_start, t_read_end, time.time(), self.seq,
            probe_stamp[1] if probe_stamp else 0, probe_stamp[0] if probe_stamp else 0.0,
        )
        self.buf[base + 8: base + rl.DESC_SIZE] = desc[8:]       # everything but the seqlock
        struct.pack_into("<Q", self.buf, base + rl.SEQLOCK_OFFSET, seqlock + 1)  # even: published
        struct.pack_into("<Q", self.buf, rl.LATEST_OFFSET, rl.pack_latest(self.seq, slot))
        self.last_slot = slot
        self.published += 1
        return True

    @staticmethod
    def _stamp(dst: np.ndarray, w: int, h: int, stamp: tuple[float, int]) -> None:
        """The parity probe strip, the overlay's geometry (bottom-left, one
        block of black on every side, white = 1-bit) stamped into the
        scene-linear frame: black 0.0, ones 10.0 (well above the reader's
        threshold after PQ; 1.0 would land under it)."""
        b = probe.BLOCK_PX
        bits = probe.encode_bits(*stamp)
        x = b
        y0 = 48 + b
        y1 = y0 + b
        x1 = min(w, x + len(bits) * b + b)
        if y1 + b > h or x1 <= x:
            return
        view = dst[:, : w * 4].reshape(h, w, 4)
        view[y0 - b: y1 + b, 0: x1, :3] = 0.0   # backdrop (rows are bottom-up: y is from the bottom)
        for i, bit in enumerate(bits):
            if bit:
                view[y0:y1, x + i * b: x + (i + 1) * b, :3] = 10.0

    # ── lifecycle ───────────────────────────────────────────────────────────

    def retire(self) -> None:
        """Tell the helper this block is dead (it exits 7 and is respawned
        on the next ring's name)."""
        if not self.retired:
            struct.pack_into("<I", self.buf, rl.STATE_OFFSET, rl.STATE_RETIRED)
            self.retired = True

    def close(self) -> None:
        self.retire()
        try:
            self.buf = None
            self.shm.close()
            self.shm.unlink()
        except Exception:
            pass
