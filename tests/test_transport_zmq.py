"""Live localhost integration of the zmq transport pair.

Real TCP sockets, short heartbeats. Skipped whole-module if pyzmq is absent
(it is a dev-venv dependency; inside Blender it comes from the bundled wheel).
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
zmq = pytest.importorskip("zmq")

from ring1 import protocol  # noqa: E402
from ring1.transport import TransportConfig  # noqa: E402
from ring1.transport_zmq import HostTransportZmq, ReplicaTransportZmq  # noqa: E402

HEARTBEAT = 0.1


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def pair():
    replica = ReplicaTransportZmq(
        TransportConfig(
            address="127.0.0.1", port_control=0, port_hot=0, port_cold=0,
            heartbeat_interval=HEARTBEAT,
        )
    )
    replica.start()
    ctl, hot, cold = replica.bound_ports()
    host = HostTransportZmq(
        TransportConfig(
            address="127.0.0.1", port_control=ctl, port_hot=hot, port_cold=cold,
            heartbeat_interval=HEARTBEAT,
        )
    )
    host.start()
    yield host, replica
    host.stop()
    replica.stop()


def test_handshake_token_accept_and_deny(pair):
    host, replica = pair

    def handler(msg):
        ok, reason = protocol.check_hello(msg, "secret")
        return {"kind": "hello_reply", "ok": ok, "reason": reason, "epoch": "r-1"}

    replica.set_request_handler(handler)
    reply = host.request(protocol.make_hello("secret", "h-1", "5.2.0"), timeout=2.0)
    assert reply and reply["ok"] and reply["epoch"] == "r-1"
    reply = host.request(protocol.make_hello("wrong", "h-1", "5.2.0"), timeout=2.0)
    assert reply and not reply["ok"] and "token" in reply["reason"]


def test_handler_exception_does_not_kill_io(pair):
    host, replica = pair
    replica.set_request_handler(lambda msg: 1 / 0)
    reply = host.request({"kind": "boom"}, timeout=2.0)
    assert reply and reply["kind"] == "error"
    replica.set_request_handler(lambda msg: {"kind": "ok"})
    reply = host.request({"kind": "fine"}, timeout=2.0)
    assert reply and reply["kind"] == "ok"


def test_hot_latest_wins(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)  # subscriber connected by then
    time.sleep(0.2)  # PUB/SUB join is async; let the subscription land
    for frame in range(1, 51):
        state = protocol.HotState(
            frame=frame,
            view_matrix=tuple(float(i) for i in range(16)),
            lens=50.0, clip_start=0.1, clip_end=100.0,
        )
        host.send_hot(state.pack())
        time.sleep(0.001)
    assert wait_for(
        lambda: (s := replica.poll_hot()) is not None
        and protocol.unpack_hot(s).frame == 50
    ), "latest hot state should be the last one sent"


def test_cold_ordered_delivery_and_chunked_blob(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    seq = 0
    for _ in range(3):
        seq += 1
        assert host.send_cold({"kind": "t1", "seq": seq}, b"delta")
    blob = b"MESH" * 100_000
    for header, payload in protocol.chunk_blob(
        "t2", "blob-9", blob, meta={"uuid": "u9"}, chunk_size=64_000
    ):
        seq += 1
        header["seq"] = seq
        assert host.send_cold(header, payload)

    received = []
    assert wait_for(
        lambda: len(received) >= seq
        or (received.extend(replica.poll_cold(64)) and False)
    ), f"expected {seq} cold messages, got {len(received)}"

    tracker = protocol.SeqTracker()
    reassembler = protocol.Reassembler()
    blobs = []
    for header, payload in received:
        assert tracker.observe(header["seq"])
        done = reassembler.feed(header, payload)
        if done and done[0]["kind"] == "t2":
            blobs.append(done)
    assert tracker.gaps == 0
    assert len(blobs) == 1 and blobs[0][1] == blob and blobs[0][0]["uuid"] == "u9"


def test_request_nowait_poll_and_cancel(pair):
    host, replica = pair
    replica.set_request_handler(lambda msg: {"kind": "ok", "echo": msg.get("kind")})
    fetched = []
    req = host.request_nowait({"kind": "probe"})

    def fetch():
        reply = host.poll_reply(req)
        if reply is not None:
            fetched.append(reply)
        return bool(fetched)

    assert wait_for(fetch)
    assert fetched[0]["echo"] == "probe"
    assert host.poll_reply(req) is None  # consumed exactly once

    cancelled = host.request_nowait({"kind": "probe2"})
    host.cancel_request(cancelled)
    time.sleep(0.3)  # reply arrives and is dropped
    assert host.poll_reply(cancelled) is None


def test_pong_carries_replica_status(pair):
    host, replica = pair
    replica.set_status_provider(lambda: {"seq": 9, "gaps": 3, "unknown": 1})
    assert wait_for(lambda: host.peer_status.get("gaps") == 3)
    assert host.peer_status == {"seq": 9, "gaps": 3, "unknown": 1}


def test_liveness_both_sides_and_peer_lost(pair):
    host, replica = pair
    states = []
    host.on_peer_state(states.append)
    assert wait_for(lambda: host.peer_alive and replica.peer_alive)
    assert states[:1] == [True]
    replica.stop()
    assert wait_for(lambda: not host.peer_alive, timeout=3.0)
    assert states[-1] is False
