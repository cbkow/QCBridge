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


def test_first_missing_marks_the_hole_and_gap_lane_names_the_lane():
    m = LaneMerger()
    assert m.observe(fast(1, 0)[0])
    assert not m.observe(fast(4, 0)[0])
    assert m.fast.first_missing == 2
    assert m.gap_lane == "fast"
    assert m.observe(cold(1)[0])
    assert not m.observe(cold(3)[0])
    assert m.cold.first_missing == 2 and m.gap_lane == "both"
    m.clear_cold_gap()
    assert m.gap_lane == "fast"


def test_fast_applied_advances_only_while_contiguous():
    m = LaneMerger()
    m.fast_done(1)
    m.fast_done(2)
    assert m.fast_applied == 2
    m.fast_done(4)                      # 3 never applied: hold
    assert m.fast_applied == 2
    m.fast_done(3)
    assert m.fast_applied == 4
    m.fast_done(2)                      # a replay below the mark is a no-op
    assert m.fast_applied == 4


def test_clear_fast_gap_catches_applied_up_with_seen():
    m = LaneMerger()
    m.observe(fast(1, 0)[0]); m.fast_done(1)
    m.observe(fast(3, 0)[0]); m.fast_done(3)
    assert m.fast_applied == 1 and m.gap_lane == "fast"
    m.clear_fast_gap()                  # the host re-asserted from seq 2 on
    assert m.fast_applied == 3 and m.gap_lane == "" and m.fast.first_missing is None


def test_parked_release_feeds_fast_done_in_order():
    m = LaneMerger()
    a = fast(1, after=2, uuid="a")
    assert not m.admit(*a)
    m.observe(a[0])
    for h, _p in m.cold_done(2):
        m.fast_done(h["seq"])
    assert m.fast_applied == 1


def test_reset_clears_applied_marks():
    m = LaneMerger()
    m.fast_done(5)
    m.observe(fast(7, 0)[0])
    m.reset()
    assert m.fast_applied == 0 and m.gap_lane == ""
