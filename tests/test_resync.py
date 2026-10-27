import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.resync import manifest_digest, plan_delta  # noqa: E402


def test_plan_partitions_the_inventories():
    host_alive = {"a", "b", "c", "d"}
    host_hash = {"a": "1", "b": "2", "c": None}          # d never primed either
    replica_held = {"a": "1", "b": "9", "x": "5", "c": "3"}
    p = plan_delta(host_alive, host_hash, replica_held, "s", "s", "cold")
    assert p.tombs == ["x"]                 # replica holds, host lacks
    assert p.resend == ["b", "d"]           # digest differs / replica lacks
    assert p.verify == ["c"]                # host not primed: hash now
    assert p.scene_t1
    assert p.at_state == ["b", "c", "d"]    # cold gap: only what is resent
    assert len(p) == 4 and not p.empty


def test_scene_mismatch_means_full_bootstrap():
    assert plan_delta({"a"}, {}, {"a": "1"}, "s1", "s2", "cold") is None
    assert plan_delta({"a"}, {}, {"a": "1"}, "", "s", "cold") is None
    assert plan_delta({"a"}, {}, {"a": "1"}, "s", "", "cold") is None


def test_at_state_widens_when_the_fast_lane_may_have_lost_paths():
    p = plan_delta({"a", "b"}, {"a": "1", "b": "2"}, {"a": "1", "b": "2"}, "s", "s", "both",
                   at_state_all=["b", "a", "coll"])
    assert p.empty and p.at_state == ["a", "b", "coll"]
    p = plan_delta({"a"}, {"a": "1"}, {"a": "1"}, "s", "s", "unknown", at_state_all=["a"])
    assert p.at_state == ["a"]


def test_manifest_digest_is_order_independent():
    a = manifest_digest({"u1": "d1", "u2": "d2"})
    b = manifest_digest({"u2": "d2", "u1": "d1"})
    assert a == b and len(a) == 32
    assert manifest_digest({"u1": "d1", "u2": "dX"}) != a
    assert manifest_digest({}) != a
