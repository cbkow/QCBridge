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
