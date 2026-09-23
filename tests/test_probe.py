"""Parity-spike probe: strip bit coding, row search, hot-packet stamp."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1 import probe, protocol  # noqa: E402

IDENTITY = tuple(float(x) for x in (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))


def _row(bits, x0, width, block=probe.BLOCK_PX):
    row = bytearray(width)
    for i, bit in enumerate(bits):
        if bit:
            row[x0 + i * block: x0 + (i + 1) * block] = b"\xeb" * block
    return bytes(row)


def test_bits_round_trip():
    bits = probe.encode_bits(1_789_000_123.456, 0x1234)
    assert len(bits) == probe.TOTAL_BITS
    t_ms, seq = probe.decode_bits(bits)
    assert t_ms == int(1_789_000_123.456 * 1000) & 0xFFFFFFFF
    assert seq == 0x1234


def test_corrupt_bit_rejected():
    bits = probe.encode_bits(1000.0, 7)
    bits[20] ^= 1
    assert probe.decode_bits(bits) is None


def test_find_strip_in_row():
    bits = probe.encode_bits(42.5, 99)
    row = _row(bits, 37, 1920)
    x0 = probe.find_strip(row)
    assert x0 is not None
    assert probe.decode_bits(probe.sample_row(row, x0)) == (42500, 99)


def test_latency_unwraps_counter():
    t_ms = 0xFFFFFFF0  # stamp just before the 32-bit wrap
    now_s = (0x1_0000_0000 + 0x10) / 1000.0
    assert probe.latency_ms(now_s, t_ms) == 0x20


def test_hot_packet_probe_is_optional_and_dedup_safe():
    base = dict(frame=1, view_matrix=IDENTITY, lens=50.0, clip_start=0.1, clip_end=100.0)
    plain = protocol.HotState(**base).pack()
    stamped_a = protocol.HotState(**base, t_host=10.0, probe_seq=1).pack()
    stamped_b = protocol.HotState(**base, t_host=10.5, probe_seq=2).pack()
    assert protocol.unpack_hot(plain).t_host == 0.0
    out = protocol.unpack_hot(stamped_b)
    assert (out.t_host, out.probe_seq) == (10.5, 2)
    assert protocol.hot_core(stamped_a) == protocol.hot_core(stamped_b) == plain
    assert protocol.unpack_hot(plain + b"x") is None


def test_capture_fps_env(monkeypatch):
    monkeypatch.setenv("QCB_CAPTURE_FPS", "60")
    assert probe.capture_fps() == 60
    monkeypatch.delenv("QCB_CAPTURE_FPS")
    assert probe.capture_fps() == 30
