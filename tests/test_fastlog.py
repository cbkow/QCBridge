import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.fastlog import FastLog  # noqa: E402


def test_window_since_keeps_last_kind_per_uuid_in_first_seen_order():
    log = FastLog()
    log.record(1, "a", "t1")
    log.record(2, "b", "t1")
    log.record(3, "a", "tomb")
    log.record(4, "c", "t1")
    log.record(5, "b", "t1")
    assert log.window_since(2) == [("b", "t1"), ("a", "tomb"), ("c", "t1")]
    assert log.window_since(1) == [("a", "tomb"), ("b", "t1"), ("c", "t1")]
    assert log.window_since(6) == []


def test_bounded_and_covers():
    log = FastLog(capacity=3)
    assert not log.covers(1)
    for s in range(1, 6):
        log.record(s, f"u{s}", "t1")
    assert log.oldest_seq == 3 and log.newest_seq == 5 and len(log) == 3
    assert log.covers(3) and log.covers(5)
    assert not log.covers(2)          # fell off the ring: the host must bootstrap
    assert not log.covers(None)


def test_clear():
    log = FastLog()
    log.record(1, "a", "t1")
    log.clear()
    assert log.oldest_seq is None and log.window_since(0) == []
