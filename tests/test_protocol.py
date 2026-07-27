"""Protocol layer: hot packing, control validation, chunking, seq tracking."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1 import protocol  # noqa: E402


IDENTITY = tuple(float(x) for x in (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))


def test_hot_round_trip():
    import pytest

    state = protocol.HotState(
        frame=-12, view_matrix=IDENTITY, lens=50.0,
        clip_start=0.1, clip_end=1000.0, is_persp=True, hold=False,
    )
    out = protocol.unpack_hot(state.pack())
    # wire floats are float32 — compare approximately
    assert out.frame == state.frame
    assert out.view_matrix == pytest.approx(state.view_matrix)
    assert out.lens == pytest.approx(state.lens)
    assert out.clip_start == pytest.approx(state.clip_start)
    assert out.clip_end == pytest.approx(state.clip_end)
    assert (out.is_persp, out.hold) == (state.is_persp, state.hold)


def test_hot_flags():
    state = protocol.HotState(
        frame=1, view_matrix=IDENTITY, lens=35.0,
        clip_start=0.01, clip_end=100.0, is_persp=False, hold=True,
        camera=True, cam_zoom=12.5, cam_offset=(0.1, -0.2),
    )
    out = protocol.unpack_hot(state.pack())
    assert out.is_persp is False and out.hold is True and out.camera is True
    import pytest
    assert out.cam_zoom == pytest.approx(12.5)
    assert out.cam_offset == pytest.approx((0.1, -0.2))


def test_hot_rejects_garbage():
    assert protocol.unpack_hot(b"junk") is None
    packed = protocol.HotState(
        frame=1, view_matrix=IDENTITY, lens=1.0, clip_start=0.1, clip_end=1.0
    ).pack()
    assert protocol.unpack_hot(b"XXXX" + packed[4:]) is None


def test_hello_check():
    hello = protocol.make_hello("secret", "epoch-a", "5.2.0")
    ok, _ = protocol.check_hello(hello, "secret")
    assert ok
    ok, reason = protocol.check_hello(hello, "other")
    assert not ok and "token" in reason
    bad = dict(hello, protocol=99)
    ok, reason = protocol.check_hello(bad, "secret")
    assert not ok and "protocol" in reason


def test_chunk_reassemble_round_trip():
    data = bytes(range(256)) * 5000  # ~1.25 MB
    r = protocol.Reassembler()
    done = None
    count = 0
    for header, payload in protocol.chunk_blob(
        "t2", "blob-1", data, meta={"uuid": "u1"}, chunk_size=200_000
    ):
        count += 1
        result = r.feed(header, payload)
        if result is not None:
            done = result
    assert count == 7
    header, blob = done
    assert blob == data and header["uuid"] == "u1"
    assert r.pending() == 0


def test_reassembler_interleaved_blobs_and_out_of_order():
    a = b"A" * 500
    b = b"B" * 500
    chunks_a = list(protocol.chunk_blob("t2", "a", a, chunk_size=200))
    chunks_b = list(protocol.chunk_blob("t2", "b", b, chunk_size=200))
    r = protocol.Reassembler()
    results = []
    # interleave, feed a's chunks out of order
    for item in [chunks_a[2], chunks_b[0], chunks_a[0], chunks_b[1],
                 chunks_a[1], chunks_b[2]]:
        out = r.feed(*item)
        if out:
            results.append(out)
    blobs = {h["blob"]["id"]: d for h, d in results}
    assert blobs == {"a": a, "b": b}


def test_reassembler_passes_unchunked_through():
    r = protocol.Reassembler()
    out = r.feed({"kind": "t1", "seq": 4}, b"payload")
    assert out == ({"kind": "t1", "seq": 4}, b"payload")


def test_empty_blob_is_one_chunk():
    chunks = list(protocol.chunk_blob("tomb", "x", b""))
    assert len(chunks) == 1
    r = protocol.Reassembler()
    header, data = r.feed(*chunks[0])
    assert data == b""


def test_seq_tracker_detects_gap():
    t = protocol.SeqTracker()
    assert t.observe(1) and t.observe(2)
    assert not t.observe(4)
    assert t.gaps == 1
    assert t.observe(5)


def test_cold_encode_decode():
    frames = protocol.encode_cold({"kind": "t1", "seq": 9}, b"xyz")
    header, payload = protocol.decode_cold(frames)
    assert header == {"kind": "t1", "seq": 9} and payload == b"xyz"
    assert protocol.decode_cold([b"not-json", b""]) is None
    assert protocol.decode_cold([b"{}"]) is None


def test_srt_passphrase_derivation():
    p = protocol.srt_passphrase("devtoken")
    assert p == protocol.srt_passphrase("devtoken")  # deterministic
    assert 10 <= len(p) <= 79  # SRT's hard requirement, even for short tokens
    assert p != protocol.srt_passphrase("other")
    assert protocol.srt_passphrase("") == ""
