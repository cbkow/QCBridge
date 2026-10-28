import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.liveness import ResyncPolicy, replica_restarted  # noqa: E402


def test_restart_detected_only_against_a_known_epoch():
    assert not replica_restarted({}, None)
    assert not replica_restarted({"epoch": "b"}, None)        # never handshaken
    assert not replica_restarted({}, "a")                     # old replica, no epoch
    assert not replica_restarted({"epoch": "a"}, "a")
    assert replica_restarted({"epoch": "b"}, "a")


def test_policy_serves_once_per_replica_state():
    p = ResyncPolicy(min_interval=10.0)
    assert not p.should_resync({"want_resync": False, "bootstraps": 1}, 0.0)
    assert p.should_resync({"want_resync": True, "bootstraps": 1}, 0.0)
    # same state again, even much later: our bootstrap is in flight
    assert not p.should_resync({"want_resync": True, "bootstraps": 1}, 60.0)
    # the bootstrap applied and the flag cleared
    assert not p.should_resync({"want_resync": False, "bootstraps": 2}, 61.0)
    # a new gap after that is a new state
    assert p.should_resync({"want_resync": True, "bootstraps": 2}, 62.0)
    assert p.sent == 2


def test_policy_rate_limits_a_flapping_replica():
    p = ResyncPolicy(min_interval=10.0)
    assert p.should_resync({"want_resync": True, "bootstraps": 0}, 0.0)
    # replica applied it (1) and immediately gapped again
    assert not p.should_resync({"want_resync": True, "bootstraps": 1}, 3.0)
    assert p.should_resync({"want_resync": True, "bootstraps": 1}, 10.0)


def test_reset_after_handshake_serves_the_same_count_again():
    p = ResyncPolicy(min_interval=0.0)
    assert p.should_resync({"want_resync": True, "bootstraps": 3}, 0.0)
    assert not p.should_resync({"want_resync": True, "bootstraps": 3}, 1.0)
    p.reset()
    assert p.should_resync({"want_resync": True, "bootstraps": 3}, 2.0)


def _covers_from(oldest):
    return lambda seq: seq is not None and seq >= oldest


def test_decide_reasserts_a_fast_hole_without_a_bootstrap():
    p = ResyncPolicy(min_interval=10.0, fast_interval=1.0)
    st = {"want_resync": True, "bootstraps": 1, "resyncs": 0, "gap_lane": "fast", "fast_gap_at": 7}
    assert p.decide(st, 0.0, ("fast_ack",), _covers_from(3)) == "fast"
    assert p.fast_sent == 1 and p.sent == 0
    # same state again: in flight
    assert p.decide(st, 5.0, ("fast_ack",), _covers_from(3)) == "none"
    # the replica applied the re-assert and gapped again: a new state, rate-limited at 1 s
    st2 = dict(st, resyncs=1)
    assert p.decide(st2, 5.5, ("fast_ack",), _covers_from(3)) == "fast"
    assert p.decide(dict(st2, resyncs=2), 5.8, ("fast_ack",), _covers_from(3)) == "none"
    assert p.decide(dict(st2, resyncs=2), 7.0, ("fast_ack",), _covers_from(3)) == "fast"


def test_decide_falls_back_to_full_when_fast_is_not_an_option():
    p = ResyncPolicy(min_interval=10.0)
    base = {"want_resync": True, "bootstraps": 1, "resyncs": 0, "gap_lane": "fast", "fast_gap_at": 7}
    # the replica cannot ack fast seqs (older replica, no caps)
    assert p.decide(base, 0.0, (), _covers_from(3)) == "full"
    # the hole fell off the host's log
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(base, 0.0, ("fast_ack",), _covers_from(9)) == "full"
    # a cold-lane hole is never a fast repair
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(dict(base, gap_lane="cold"), 0.0, ("fast_ack",), _covers_from(3)) == "full"
    # an unknown uuid means a blob never arrived
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(dict(base, gap_lane="unknown"), 0.0, ("fast_ack",), _covers_from(3)) == "full"


def test_served_state_keys_on_bootstraps_and_resyncs():
    p = ResyncPolicy(min_interval=0.0, fast_interval=0.0)
    st = {"want_resync": True, "bootstraps": 2, "resyncs": 3, "gap_lane": "cold"}
    assert p.decide(st, 0.0) == "full"
    assert p.decide(st, 1.0) == "none"
    assert p.decide(dict(st, resyncs=4), 2.0) == "full"   # a fast repair landed meanwhile
    assert p.decide(dict(st, bootstraps=3, resyncs=4), 3.0) == "full"


def test_decide_delta_when_the_replica_can_take_one():
    p = ResyncPolicy(min_interval=10.0, fast_interval=1.0)
    st = {"want_resync": True, "bootstraps": 1, "resyncs": 0, "gap_lane": "cold"}
    caps = ("fast_ack", "delta_resync")
    # the manifest is on its way: nothing yet, and no state is served
    assert p.decide(st, 0.0, caps, None, "pending") == "none"
    assert p.decide(st, 0.5, caps, None, "ready") == "delta"
    assert p.delta_sent == 1 and p.sent == 0
    assert p.decide(st, 5.0, caps, None, "ready") == "none"          # in flight
    st2 = dict(st, resyncs=1)
    assert p.decide(st2, 5.5, caps, None, "ready") == "delta"         # a new hole, rate-limited at 1 s
    # a scene-structure mismatch, or a replica without the cap, is a bootstrap
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(st, 0.0, caps, None, "mismatch") == "full"
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(st, 0.0, ("fast_ack",), None, "ready") == "full"


def test_reset_with_the_handshake_state_waits_for_the_handshake_bootstrap():
    """2026-10-28: a want_resync the replica raised BEFORE the handshake
    must not draw a second full file right after the handshake's own."""
    p = ResyncPolicy(min_interval=10.0)
    st = {"want_resync": True, "gap_lane": "cold", "bootstraps": 1, "resyncs": 0}
    p.reset((1, 0))
    assert p.decide(st, 0.0, (), None, None) == "none"      # served by the handshake bootstrap
    st2 = dict(st, bootstraps=2)                              # it landed, the flag is still up
    assert p.decide(st2, 0.5, (), None, None) == "full"
    p2 = ResyncPolicy(min_interval=10.0)
    p2.reset(None)
    assert p2.decide(st, 0.0, (), None, None) == "full"       # no state known: the old behaviour


def test_delta_that_plans_nothing_is_rate_limited_like_a_full_bootstrap():
    """2026-10-28: a persistent structure difference used to re-ship the
    file at the 1 s fast cadence through the delta fallback."""
    caps = ("fast_ack", "delta_resync")
    st = {"want_resync": True, "gap_lane": "cold", "bootstraps": 1, "resyncs": 0}
    p = ResyncPolicy(min_interval=10.0)
    assert p.decide(st, 0.0, caps, None, "ready") == "delta"
    assert p.allow_full(0.0) is True            # the first fallback goes
    st2 = dict(st, bootstraps=2)
    assert p.decide(st2, 1.5, caps, None, "ready") == "delta"
    assert p.allow_full(1.5) is False           # too soon: un-served, asked again next tick
    assert p.decide(st2, 2.6, caps, None, "ready") == "delta"   # asked again once the 1 s fast interval passed
    assert p.allow_full(10.5) is True
    assert p.sent == 2
