import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.merge import LaneMerger  # noqa: E402


def fast(seq, after, uuid="u"):
    return {"kind": "t1", "lane": "f", "seq": seq, "after": after, "uuid": uuid}, b"d"


def cold(seq, uuid="u"):
    return {"kind": "t2", "seq": seq, "uuid": uuid}, b"blob"


def test_fast_before_its_blob_is_parked_then_released():
    m = LaneMerger()
    # host: blob chunks cold 1..3, then a delta that must follow seq 3
    h, p = fast(1, after=3)
    assert m.observe(h)
    assert not m.admit(h, p)            # parked: cold has applied nothing
    assert m.parked == 1
    for s in (1, 2):
        m.observe(cold(s)[0])
        assert m.cold_done(s) == []     # still waiting for 3
    m.observe(cold(3)[0])
    due = m.cold_done(3)
    assert due == [(h, p)]
    assert m.parked == 0


def test_fast_with_nothing_to_wait_for_applies_at_once():
    m = LaneMerger()
    h, p = fast(1, after=0)
    assert m.admit(h, p)
    m.cold_done(5)
    assert m.admit(*fast(2, after=5))
    assert not m.admit(*fast(3, after=6))


def test_gaps_are_per_lane_and_summed():
    m = LaneMerger()
    assert m.observe(fast(1, 0)[0])
    assert not m.observe(fast(3, 0)[0])   # fast gap
    assert m.observe(cold(1)[0])
    assert m.observe(cold(2)[0])
    assert not m.observe(cold(9)[0])      # cold gap
    assert m.gaps == 2


def test_cold_messages_are_never_parked_and_release_in_order():
    m = LaneMerger()
    a = fast(1, after=2, uuid="a")
    b = fast(2, after=4, uuid="b")
    assert not m.admit(*a) and not m.admit(*b)
    assert m.admit(*cold(1))
    assert m.cold_done(2) == [a]
    assert m.cold_done(4) == [b]
    assert m.cold_done(1) == []          # never regresses


def test_reset_drops_parked_and_trackers():
    m = LaneMerger()
    m.admit(*fast(1, after=9))
    m.observe(cold(7)[0])
    m.reset()
    assert m.parked == 0 and m.cold_applied == 0 and m.gaps == 0
    assert m.observe(cold(1)[0])
