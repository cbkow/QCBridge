"""Live localhost integration of the agent transport pair.

Same contract as test_transport_zmq.py, over one QUIC connection between two
qcbridge-agent processes this module spawns (QCB_AGENT=spawn). Each agent
gets its own directory, so config, certificate and agent.json are isolated
and a replica's certificate is stable across restarts within a test.

This file IS the transport contract: the four cases below the zmq suite does
not cover — keyed hot, credit-window backpressure, trust-on-first-use
pinning, and a token rejected at the QUIC layer — are the reason it exists.
Skipped whole-module when the agent isn't built (cargo build in agent/).
"""

import os
import pathlib
import socket
import sys
import time

os.environ["QCB_AGENT"] = "spawn"  # never attach to a running agent from tests

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))

from ring1 import protocol  # noqa: E402
from ring1.transport import TransportConfig  # noqa: E402
from ring1.transport_agent import (  # noqa: E402
    HostTransportAgent, ReplicaTransportAgent, find_agent, pack_cold, unpack_cold,
)

if find_agent() is None:
    pytest.skip("qcbridge-agent binary not built", allow_module_level=True)

HEARTBEAT = 0.1


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_pair(tmp_path, token_host="tok", token_replica="tok", fingerprint=""):
    port = free_udp_port()
    replica = ReplicaTransportAgent(TransportConfig(
        address="127.0.0.1", port_control=port, port_hot=0, port_cold=0,
        heartbeat_interval=HEARTBEAT, token=token_replica, cert_dir=str(tmp_path / "cert"),
    ))
    replica.start()
    host = HostTransportAgent(TransportConfig(
        address="127.0.0.1", port_control=port, port_hot=0, port_cold=0,
        heartbeat_interval=HEARTBEAT, token=token_host, fingerprint=fingerprint,
    ))
    host.start()
    return host, replica


@pytest.fixture()
def pair(tmp_path):
    host, replica = make_pair(tmp_path)
    yield host, replica
    host.stop()
    replica.stop()


def test_cold_pack_round_trip():
    header, payload = unpack_cold(pack_cold({"kind": "t1", "seq": 4}, b"\x00\x01blob"))
    assert header == {"kind": "t1", "seq": 4} and payload == b"\x00\x01blob"
    assert unpack_cold(b"\x00") is None


def test_handshake_token_accept_and_deny(pair):
    host, replica = pair

    def handler(msg):
        ok, reason = protocol.check_hello(msg, "secret")
        return {"kind": "hello_reply", "ok": ok, "reason": reason, "epoch": "r-1"}

    replica.set_request_handler(handler)
    assert wait_for(lambda: host.peer_alive)
    reply = host.request(protocol.make_hello("secret", "h-1", "5.2.0"), timeout=2.0)
    assert reply and reply["ok"] and reply["epoch"] == "r-1"
    reply = host.request(protocol.make_hello("wrong", "h-1", "5.2.0"), timeout=2.0)
    assert reply and not reply["ok"] and "token" in reply["reason"]


def test_handler_exception_does_not_kill_io(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    replica.set_request_handler(lambda msg: 1 / 0)
    reply = host.request({"kind": "boom"}, timeout=2.0)
    assert reply and reply["kind"] == "error"
    replica.set_request_handler(lambda msg: {"kind": "ok"})
    reply = host.request({"kind": "fine"}, timeout=2.0)
    assert reply and reply["kind"] == "ok"


def test_hot_latest_wins_and_keys_are_independent(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    for frame in range(1, 51):
        state = protocol.HotState(
            frame=frame, view_matrix=tuple(float(i) for i in range(16)),
            lens=50.0, clip_start=0.1, clip_end=100.0,
        )
        host.send_hot(state.pack())
        host.send_hot_keyed(b"light.energy", str(frame).encode())
        time.sleep(0.001)
    assert wait_for(
        lambda: (s := replica.poll_hot()) is not None and protocol.unpack_hot(s).frame == 50
    ), "latest camera state should be the last one sent"
    assert wait_for(lambda: replica.poll_hot_keyed().get(b"light.energy") == b"50")


def test_cold_ordered_delivery_and_chunked_blob(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    seq = 0
    sent = []
    for _ in range(3):
        seq += 1
        sent.append(({"kind": "t1", "seq": seq}, b"delta"))
    blob = b"MESH" * 2_000_000  # 8 MB: far more chunks than the credit window
    for header, payload in protocol.chunk_blob(
        "t2", "blob-9", blob, meta={"uuid": "u9"}, chunk_size=64_000
    ):
        seq += 1
        header["seq"] = seq
        sent.append((header, payload))

    refused = 0
    for header, payload in sent:
        while not host.send_cold(header, payload):
            refused += 1          # window full: the caller retries, as the dirty set does
            time.sleep(0.002)
    assert refused > 0, "credit window should have pushed back on an 8 MB burst"

    received = []
    assert wait_for(
        lambda: len(received) >= seq or (received.extend(replica.poll_cold(64)) and False),
        timeout=20.0,
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
    assert wait_for(lambda: host.peer_alive)
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
    assert host.poll_reply(req) is None

    cancelled = host.request_nowait({"kind": "probe2"})
    host.cancel_request(cancelled)
    time.sleep(0.3)
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
    assert wait_for(lambda: not host.peer_alive, timeout=6.0)
    assert states[-1] is False


def test_trust_on_first_use_then_pin(tmp_path):
    host, replica = make_pair(tmp_path)
    try:
        assert wait_for(lambda: host.peer_alive)
        assert len(replica.fingerprint) == 64
        assert host.peer_fingerprint == replica.fingerprint  # learned, not configured
    finally:
        host.stop()
        replica.stop()
    # Pinned to the right cert: connects. Pinned to another: never comes up.
    host, replica = make_pair(tmp_path, fingerprint=replica.fingerprint)
    try:
        assert wait_for(lambda: host.peer_alive)
    finally:
        host.stop()
        replica.stop()
    host, replica = make_pair(tmp_path, fingerprint="00" * 32)
    try:
        assert not wait_for(lambda: host.peer_alive, timeout=1.5)
        assert wait_for(lambda: "certificate changed" in host.link_note)
    finally:
        host.stop()
        replica.stop()


def test_wrong_quic_token_never_connects(tmp_path):
    host, replica = make_pair(tmp_path, token_host="nope")
    try:
        assert not wait_for(lambda: host.peer_alive, timeout=1.5)
    finally:
        host.stop()
        replica.stop()
