import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1 import ring_layout as rl  # noqa: E402


def test_header_round_trip_and_fixed_word_offsets():
    lay = rl.layout_for(2560)
    buf = bytearray(rl.PAGE)
    buf[: rl.HEADER_SIZE] = rl.pack_header(lay, pid=4242, t_created=12.5)
    h = rl.unpack_header(buf)
    assert h["magic"] == rl.MAGIC and h["version"] == rl.VERSION
    assert h["slot_count"] == 3 and h["producer_pid"] == 4242
    assert h["slot_stride"] == lay.slot_stride and h["total_size"] == lay.total_size
    assert h["latest"] == 0 and h["reader_claim"] == 0 and h["state"] == rl.STATE_LIVE
    # the control words the two processes poll are where the constants say
    struct.pack_into("<Q", buf, rl.LATEST_OFFSET, rl.pack_latest(7, 2))
    struct.pack_into("<Q", buf, rl.READER_CLAIM_OFFSET, 3)
    struct.pack_into("<I", buf, rl.STATE_OFFSET, rl.STATE_RETIRED)
    h = rl.unpack_header(buf)
    assert rl.unpack_latest(h["latest"]) == (7, 2)
    assert h["reader_claim"] == 3 and h["state"] == rl.STATE_RETIRED


def test_slots_and_pixels_are_page_aligned_and_rows_padded():
    lay = rl.layout_for(2560)
    assert lay.pixels_capacity % rl.PAGE == 0
    for s in range(lay.slot_count):
        assert lay.slot_offset(s) % rl.PAGE == 0
        assert lay.pixels_at(s) % rl.PAGE == 0
        assert lay.pixels_at(s) + lay.pixels_capacity <= lay.total_size
    assert rl.bytes_per_row(2560) == 2560 * 16                 # already a multiple of 256
    assert rl.bytes_per_row(1000) == rl.round_up(16000, 256)   # 16128
    assert lay.fits(2560, rl.height_cap(2560))
    assert lay.fits(2560, 1440) and lay.fits(1920, 1080)
    assert not lay.fits(2560, rl.height_cap(2560) + 2)        # taller: retire + re-create


def test_desc_round_trip():
    d = rl.pack_desc(
        seqlock=4, w=1920, h=1080, bpr=rl.bytes_per_row(1920), pixel_format=rl.PF_RGBA32F,
        flags=rl.F_CAMERA_RECT | rl.F_PROBE_STAMPED, frame_number=17,
        t_read_start=1.0, t_read_end=1.002, t_publish=1.003, frame_seq=9,
        probe_seq=3, probe_t_host=123.4,
    )
    assert len(d) == rl.DESC_SIZE <= rl.PAGE
    u = rl.unpack_desc(d)
    assert u["seqlock"] == 4 and u["w"] == 1920 and u["h"] == 1080
    assert u["flags"] & rl.F_CAMERA_RECT and u["flags"] & rl.F_PROBE_STAMPED
    assert u["frame_seq"] == 9 and u["probe_seq"] == 3 and u["probe_t_host"] == 123.4
    assert u["pixel_format"] == rl.PF_RGBA32F


def test_next_slot_steps_over_the_readers_claim():
    assert rl.next_slot(0, 0, 3) == 1
    assert rl.next_slot(2, 0, 3) == 0
    assert rl.next_slot(0, 2, 3) == 2      # reader holds slot 1 (claim = slot+1)
    assert rl.next_slot(1, 3, 3) == 0      # reader holds slot 2
    assert rl.next_slot(2, 1, 3) == 1      # reader holds slot 0


def test_ring_name_fits_the_posix_limit():
    assert rl.ring_name(99999, 999) == "qcb-99999-999"
    assert len(rl.ring_name(2**31 - 1, 2**31 - 1)) <= rl.NAME_MAX
