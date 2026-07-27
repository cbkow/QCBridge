"""DirtySet semantics (decision #16): ratchet, tombstones, collapse, drain."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.dirtyset import DirtySet, Tier  # noqa: E402


def test_slider_minute_is_one_entry():
    d = DirtySet()
    for _ in range(1000):
        d.mark("lamp", Tier.T1)
    assert len(d) == 1
    _, dirty = d.drain()
    assert dirty == [("lamp", Tier.T1)]
    assert len(d) == 0


def test_tier_ratchets_up_never_down():
    d = DirtySet()
    d.mark("mesh", Tier.T1)
    d.mark("mesh", Tier.T2)
    d.mark("mesh", Tier.T1)
    _, dirty = d.drain()
    assert dirty == [("mesh", Tier.T2)]


def test_create_then_delete_collapses_to_nothing():
    d = DirtySet()
    d.mark("temp", Tier.T2)
    d.mark_deleted("temp")
    assert len(d) == 0


def test_delete_after_send_tombstones():
    d = DirtySet()
    d.mark("rock", Tier.T2)
    d.drain()  # peer now knows "rock"
    d.mark_deleted("rock")
    tombs, dirty = d.drain()
    assert tombs == ["rock"] and dirty == []


def test_deleted_stays_deleted_until_drained():
    d = DirtySet()
    d.mark("rock", Tier.T2)
    d.drain()
    d.mark_deleted("rock")
    d.mark("rock", Tier.T1)  # stray late event for a dead datablock
    tombs, dirty = d.drain()
    assert tombs == ["rock"] and dirty == []


def test_tombstone_then_second_delete_drain_is_empty():
    d = DirtySet()
    d.mark("rock", Tier.T2)
    d.drain()
    d.mark_deleted("rock")
    d.drain()
    # peer forgot it after the tombstone: a second delete is a no-op
    d.mark_deleted("rock")
    assert len(d) == 0


def test_requeue_after_failed_send():
    d = DirtySet()
    d.mark("mesh", Tier.T2)
    _, dirty = d.drain()
    uuid, tier = dirty[0]
    d.requeue(uuid, tier)
    _, dirty = d.drain()
    assert dirty == [("mesh", Tier.T2)]


def test_epoch_change_drops_tombstones_keeps_dirty():
    d = DirtySet()
    d.mark("keep", Tier.T1)
    d.mark("gone", Tier.T2)
    d.drain()
    d.mark("keep", Tier.T1)
    d.mark_deleted("gone")
    d.peer_forgot_everything()
    tombs, dirty = d.drain()
    assert tombs == [] and dirty == [("keep", Tier.T1)]


def test_counts_for_status():
    d = DirtySet()
    d.mark("a", Tier.T1)
    d.mark("b", Tier.T2)
    d.mark("c", Tier.T2)
    d.drain()
    d.mark("a", Tier.T1)
    d.mark("b", Tier.T2)
    d.mark_deleted("c")
    assert d.counts() == (1, 1, 1)
