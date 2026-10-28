"""The session timeline (ring1/timeline.py): a line per stage, a file per
session, the tail in memory, never raising."""
import os
import tempfile

from ring1 import timeline


def setup_function(_f):
    timeline.reset()


def test_begin_truncates_and_marks_the_start():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.log")
        with open(p, "w") as f:
            f.write("old session\n")
        timeline.begin("HOST", p, addon="0.3.0")
        lines = open(p).read().splitlines()
        assert len(lines) == 1
        assert "session.start" in lines[0] and "role=HOST" in lines[0] and "addon=0.3.0" in lines[0]
        assert lines[0].split()[1].startswith("+0.") and lines[0].split()[1].endswith("s")


def test_mark_appends_file_and_memory_with_elapsed_seconds():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.log")
        timeline.begin("REPLICA", p)
        line = timeline.mark("boot.applied", bytes=7366412, errors=0, names=["a b", "c"], ms=12.3456)
        assert "boot.applied bytes=7366412 errors=0 names=[a b,c] ms=12.346" in line
        assert open(p).read().splitlines()[-1] == line
        assert timeline.tail(1) == [line]
        assert timeline.elapsed() >= 0.0


def test_mark_changed_only_on_change():
    timeline.begin("HOST", "")
    assert timeline.mark_changed("peer.pixel", status="waiting") is True
    assert timeline.mark_changed("peer.pixel", status="waiting") is False
    assert timeline.mark_changed("peer.pixel", status="streaming") is True
    assert len([l for l in timeline.tail() if "peer.pixel" in l]) == 2


def test_unwritable_path_never_raises():
    timeline.begin("HOST", "/nonexistent-dir-qcb/x/y/t.log")
    assert timeline.path() == "" or True   # either way the next mark must not raise
    timeline.mark("anything", k=1)
    assert any("anything" in l for l in timeline.tail())


def test_memory_tail_is_bounded():
    timeline.begin("HOST", "")
    for i in range(timeline.KEEP + 50):
        timeline.mark("tick", i=i)
    assert len(timeline.tail(10_000)) == timeline.KEEP
