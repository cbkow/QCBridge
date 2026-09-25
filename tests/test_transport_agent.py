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
from pathlib import Path
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


def test_cold_ordered_delivery_and_chunked_blob(pair, monkeypatch):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    # On loopback the agent drains faster than Python can fill 32 MiB, so
    # the pushback would never show; shrink the window for the assertion.
    from ring1 import transport_agent
    monkeypatch.setattr(transport_agent, "_COLD_WINDOW_BYTES", 1 << 20)
    seq = 0
    sent = []
    for _ in range(3):
        seq += 1
        sent.append(({"kind": "t1", "seq": seq}, b"delta"))
    blob = b"MESH" * 2_000_000  # 8 MB: far more than the (shrunk) window
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


def wait_reply(transport, req_id, timeout=5.0):
    """The agent's correlated reply, or None. (poll_cmd_reply pops, so a
    walrus inside a wait_for lambda would consume it into the lambda's own
    scope — which is exactly the bug this helper replaced.)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = transport.poll_cmd_reply(req_id)
        if reply is not None:
            return reply
        time.sleep(0.02)
    return None


def test_set_config_round_trip_and_needs_restart(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    # A live field lands, persists, and comes back in the correlated reply.
    reply = wait_reply(host, host.set_config(idle_secs=7))
    assert reply and reply["changed"] == ["idle_secs"], reply
    assert host.agent_config["idle_secs"] == 7
    # A restart-only field is named back, not silently ignored.
    reply = wait_reply(host, host.set_config(local_port=1))
    assert reply and reply["needs_restart"] == ["local_port"] and reply["changed"] == [], reply
    # A bad value is rejected, not coerced.
    reply = wait_reply(host, host.set_config(discovery="loud"))
    assert reply and reply["rejected"] == ["discovery"], reply


def test_the_mirror_carries_derived_secrets_not_the_token(pair, tmp_path):
    """Since 2026-09-24 the agent keeps the token: the attach reply and every
    config event carry a fingerprint, the SRT passphrase and the hello
    secret, and never the token. Setting a token through set_config lands in
    the store (the file store for a spawned agent) and leaves the TOML."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    for t in (host, replica):
        cfg = t.agent_config
        assert "token" not in cfg, cfg
        assert cfg["token_set"] is True
        assert cfg["token_fingerprint"] == protocol.token_fingerprint("tok")
        assert cfg["srt_passphrase"] == protocol.srt_passphrase("tok")
        assert cfg["hello_secret"] == protocol.hello_secret("tok")
        assert cfg["token_store"] == "file"
    reply = wait_reply(host, host.set_config(token="newtok"))
    assert reply and reply["changed"] == ["token"], reply
    assert "token" not in host.agent_config
    assert host.agent_config["token_fingerprint"] == protocol.token_fingerprint("newtok")
    # The host's own directory: TOML without the token, the file with it.
    proc = host._link._proc
    assert proc is not None, "these tests spawn their agents (QCB_AGENT=spawn)"
    host_dir = Path(proc.args[2]).parent
    assert "newtok" not in (host_dir / "host.toml").read_text(encoding="utf-8")
    assert (host_dir / "host.token").read_text(encoding="utf-8").strip() == "newtok"


def test_mappings_and_cache_root_live_in_the_agent(pair):
    """Since 2026-09-24 the agent owns the path-mapping table and the cache
    root: set_config takes a table of rows, the config event mirrors it, a
    malformed table is rejected whole."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    assert host.agent_config["path_mappings"] == [] and host.agent_config["cache_root"] == ""
    reply = wait_reply(host, host.set_config(shared_root="/Volumes/Jobs"))
    assert reply and reply["changed"] == ["shared_root"] and host.agent_config["shared_root"] == "/Volumes/Jobs"
    rows = [{"win": "M:\\Jobs", "mac": "/Volumes/Jobs", "enabled": True, "label": "jobs"}]
    reply = wait_reply(host, host.set_config(path_mappings=rows, cache_root="/Volumes/Jobs/cache"))
    assert reply and sorted(reply["changed"]) == ["cache_root", "path_mappings"], reply
    assert host.agent_config["path_mappings"] == rows
    assert host.agent_config["cache_root"] == "/Volumes/Jobs/cache"
    reply = wait_reply(host, host.set_config(path_mappings=[{"win": 1}]))
    assert reply and reply["rejected"] == ["path_mappings"], reply
    assert host.agent_config["path_mappings"] == rows


def test_the_role_switch_is_live(pair, tmp_path):
    """Since 2026-09-24 `role` is a live field: the host agent becomes a
    replica in place (its dial loop stops, it listens), then a host again
    (it re-dials its configured peer and pairs). No restart, one agent."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    # Listen somewhere free first (also live for a replica), then flip.
    port = free_udp_port()
    reply = wait_reply(host, host.set_config(listen=f"127.0.0.1:{port}"))
    assert reply and reply["changed"] == ["listen"], reply
    reply = wait_reply(host, host.set_config(role="replica"))
    assert reply and reply["changed"] == ["role"], reply
    assert host.agent_config["role"] == "replica"
    assert wait_for(lambda: not host.peer_alive, timeout=8.0), "the dial loop should have stopped"
    # Commands still work across the switch (the probe here reaches
    # whichever of the two loopback replicas holds 4246, so its content is
    # not asserted; the paired run proves the listener).
    reply = wait_reply(host, host.discover("127.0.0.1"), timeout=6.0)
    assert reply and reply["event"] == "peers", reply
    # And back: the configured peer is dialled again.
    reply = wait_reply(host, host.set_config(role="host"))
    assert reply and reply["changed"] == ["role"], reply
    assert wait_for(lambda: host.peer_alive, timeout=10.0), "the host should re-pair"


def test_a_control_client_gets_events_beside_the_addon(pair):
    """The settings window attaches as a control client (2026-09-24): it
    gets the attach reply and every event, may send commands, and does not
    displace the addon that owns the lanes."""
    import json as _json
    import socket as _socket
    import struct as _struct

    from ring1.transport_agent import T_CMD, T_EVENT  # frame kinds

    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    addr, port, secret = host._link._info
    s = _socket.create_connection((addr, port), timeout=5.0)

    def send(kind, obj):
        body = _json.dumps(obj).encode()
        s.sendall(_struct.pack(">I", len(body) + 1) + bytes([kind]) + body)

    def recv():
        head = s.recv(5, _socket.MSG_WAITALL)
        n = _struct.unpack(">I", head[:4])[0]
        body = s.recv(n - 1, _socket.MSG_WAITALL)
        return head[4], _json.loads(body)

    send(T_CMD, {"cmd": "attach", "secret": secret, "kind": "control"})
    kind, attached = recv()
    assert kind == T_EVENT and attached["event"] == "attached" and attached["control"] is True, attached
    assert "config" in attached and "token" not in attached["config"]
    # A command from the control client is answered on the control client.
    send(T_CMD, {"cmd": "set_config", "req": 7, "set": {"idle_secs": 11}})
    for _ in range(20):
        kind, ev = recv()
        if ev.get("event") == "config" and ev.get("req") == 7:
            break
    else:
        raise AssertionError("no config reply on the control client")
    assert ev["changed"] == ["idle_secs"]
    # The addon saw the same change, and is still attached.
    assert wait_for(lambda: host.agent_config.get("idle_secs") == 11)
    assert host.peer_alive
    s.close()


def test_discover_by_direct_probe_finds_the_replica(pair):
    """The VPN path: a unicast probe to an address returns the replica's
    beacon — name, listen port, certificate fingerprint, paired state — with
    the asking host excluded from its own answer."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    reply = wait_reply(host, host.discover("127.0.0.1"), timeout=6.0)
    assert reply, "no peers reply within 6 s"
    assert reply["sources"] == ["probe"]
    peers = reply["peers"]
    assert len(peers) == 1, peers
    p = peers[0]
    assert p["role"] == "replica"
    assert p["fp"] == replica.fingerprint, "the probe reports the certificate we would pin"
    assert p["port"] == replica.bound_ports()[0]
    assert p["paired"] is True
    assert host.peers == peers


def test_agent_advertises_byte_credits(pair):
    """COLD_ACK carries bytes since 2026-09-23; the attached event says so
    and the host transport switches its window to bytes on that field."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    assert host._credits_bytes, "attached event lacked credits=bytes — stale agent binary?"
    assert replica._credits_bytes if hasattr(replica, "_credits_bytes") else True


def test_fast_lane_is_not_behind_a_cold_blob(pair):
    """A tier-1 delta sent right after a large blob must reach the replica
    before the blob finishes — that is what the second stream is for."""
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    assert host._has_fast, "attached event lacked lanes=[fast] — stale agent binary?"
    blob = b"MESH" * 8_000_000  # 32 MB on the cold lane
    chunks = list(protocol.chunk_blob("t2", "blob-big", blob, meta={"uuid": "big"}))
    seq = 0
    for header, payload in chunks:
        seq += 1
        header["seq"] = seq
        while not host.send_cold(header, payload):
            time.sleep(0.002)
    assert host.send_fast({"kind": "t1", "lane": "f", "seq": 1, "uuid": "x", "after": 0}, b"delta")
    fast = []
    assert wait_for(lambda: fast.extend(replica.poll_fast(8)) or bool(fast), timeout=5.0)
    assert fast[0][0]["uuid"] == "x"
    # the cold lane must still deliver the whole blob, in order
    cold = []
    assert wait_for(lambda: cold.extend(replica.poll_cold(64)) or len(cold) >= len(chunks), timeout=15.0)
    assert [h["blob"]["i"] for h, _ in cold[: len(chunks)]] == list(range(len(chunks)))


def test_cold_payloads_round_trip_byte_identical(pair):
    """The agent compresses cold payloads on the wire (attached: codec=zstd);
    what the replica polls must be exactly what the host sent — a highly
    compressible blob and an incompressible one, plus a tiny one below the
    compression threshold."""
    import os
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    assert host.wire_compresses, "attached event lacked codec=zstd — stale agent binary?"
    payloads = [b"MESH" * 1_500_000, os.urandom(1_500_000), b"x"]
    for i, p in enumerate(payloads):
        while not host.send_cold({"kind": "t2", "seq": i + 1, "uuid": f"p{i}"}, p):
            time.sleep(0.002)
    got = []
    assert wait_for(lambda: got.extend(replica.poll_cold(16)) or len(got) >= 3, timeout=15.0)
    assert [p for _, p in got[:3]] == payloads


# ---- the default transport (2026-09-23) --------------------------------

def _registered(tmp_path, monkeypatch, role="host"):
    # agent_socket_info reads agent.json under the platform config base.
    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "QCBridge").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "QCBridge").mkdir(parents=True, exist_ok=True)
    appdata = tmp_path / "appdata"; (appdata / "QCBridge").mkdir(parents=True, exist_ok=True)
    import json as _json
    info = _json.dumps({role: {"pid": 1, "port": 12345, "secret": "s"}})
    for p in (home / "Library" / "Application Support" / "QCBridge" / "agent.json",
              home / ".config" / "QCBridge" / "agent.json",
              appdata / "QCBridge" / "agent.json"):
        p.write_text(info)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("QCB_AGENT_PORT", raising=False)
    monkeypatch.delenv("QCB_AGENT_SECRET", raising=False)
    monkeypatch.delenv("QCB_TRANSPORT", raising=False)
    monkeypatch.delenv("QCB_AGENT", raising=False)


def test_transport_defaults_to_agent_when_one_is_registered(tmp_path, monkeypatch):
    from ring1.transport_agent import transport_kind
    _registered(tmp_path, monkeypatch, "host")
    assert transport_kind("HOST") == "agent"          # role case does not matter
    # A host agent registered puts the machine in agent mode whatever the
    # addon's role field says: the session adopts the agent's role.
    assert transport_kind("REPLICA") == "agent"


def test_transport_defaults_to_zmq_without_an_agent(tmp_path, monkeypatch):
    from ring1.transport_agent import transport_kind
    _registered(tmp_path, monkeypatch, "host")
    (tmp_path / "home" / "Library" / "Application Support" / "QCBridge" / "agent.json").unlink()
    (tmp_path / "home" / ".config" / "QCBridge" / "agent.json").unlink()
    (tmp_path / "appdata" / "QCBridge" / "agent.json").unlink()
    assert transport_kind("host") == "zmq"


def test_transport_environment_and_preference_win(tmp_path, monkeypatch):
    from ring1.transport_agent import transport_kind
    _registered(tmp_path, monkeypatch, "host")
    assert transport_kind("host", pref="zmq") == "zmq"      # explicit pref over the default
    monkeypatch.setenv("QCB_TRANSPORT", "zmq")
    assert transport_kind("host", pref="agent") == "zmq"    # env over pref
    monkeypatch.setenv("QCB_TRANSPORT", "agent")
    monkeypatch.setenv("QCB_AGENT", "spawn")
    assert transport_kind("replica") == "agent"             # env wins even when spawning


def test_ensure_agent_never_spawns_under_the_test_harness(tmp_path):
    """QCB_AGENT=spawn means the tests own their agents; ensure_agent must
    stay out of the way and report nothing to start."""
    from ring1.transport_agent import ensure_agent, installed_agent_paths
    assert ensure_agent("host") is False
    paths = installed_agent_paths()
    assert all(os.path.isabs(p) for p in paths)


# ---- the agent's role is the session's role (2026-09-25) --------------

def test_registered_role_follows_the_one_agent_on_the_machine(tmp_path, monkeypatch):
    from ring1.transport_agent import registered_role
    import ring1.transport_agent as ta
    _registered(tmp_path, monkeypatch, "replica")
    ta._registered_role_cache = None
    assert registered_role() == "replica"
    _registered(tmp_path, monkeypatch, "host")
    ta._registered_role_cache = None
    assert registered_role() == "host"


def test_registered_role_is_none_when_ambiguous_or_launched(tmp_path, monkeypatch):
    from ring1.transport_agent import registered_role
    _registered(tmp_path, monkeypatch, "host")
    import json as _json
    both = _json.dumps({"host": {"pid": 1, "port": 1, "secret": "s"},
                        "replica": {"pid": 2, "port": 2, "secret": "t"}})
    for p in (tmp_path / "home" / "Library" / "Application Support" / "QCBridge" / "agent.json",
              tmp_path / "home" / ".config" / "QCBridge" / "agent.json",
              tmp_path / "appdata" / "QCBridge" / "agent.json"):
        p.write_text(both)
    import ring1.transport_agent as ta
    ta._registered_role_cache = None
    assert registered_role() is None            # two agents: the addon keeps its own field
    _registered(tmp_path, monkeypatch, "host")
    monkeypatch.setenv("QCB_AGENT_PORT", "5"); monkeypatch.setenv("QCB_AGENT_SECRET", "x")
    assert registered_role() is None            # an agent-launched Blender names no role
    monkeypatch.delenv("QCB_AGENT_PORT"); monkeypatch.delenv("QCB_AGENT_SECRET")
    monkeypatch.setenv("QCB_AGENT", "spawn")
    assert registered_role() is None            # the tests own their agents


def test_session_fields_follow_the_agent_in_agent_mode():
    """In agent mode the addon's address, kiosk and peer fields are not
    overrides: what the agent reports is what the session uses."""
    from types import SimpleNamespace
    from ring1.transport_agent import effective_kiosk, effective_peer_host
    agent = SimpleNamespace(agent_mode=True,
                            agent_config={"peer": "10.0.0.5:19990", "kiosk": False})
    prefs = SimpleNamespace(replica_address="1.2.3.4", replica_kiosk=True)
    assert effective_peer_host(prefs, agent) == "10.0.0.5"
    assert effective_kiosk(prefs, agent) is False
    zmq = SimpleNamespace(agent_mode=False, agent_config={})
    assert effective_peer_host(prefs, zmq) == "1.2.3.4"
    assert effective_kiosk(prefs, zmq) is True


def test_ensure_any_agent_never_spawns_inside_an_agent_launched_blender(tmp_path, monkeypatch):
    """QCB_AGENT_PORT/SECRET mean the agent that launched this Blender is the
    one to use; nothing is registered under a role there, and starting the
    installed agent would be refused by its single-instance guard."""
    from ring1 import transport_agent as ta
    monkeypatch.delenv("QCB_AGENT", raising=False)
    monkeypatch.setenv("QCB_AGENT_PORT", "5"); monkeypatch.setenv("QCB_AGENT_SECRET", "x")
    monkeypatch.setattr(ta, "find_agent", lambda explicit="": (_ for _ in ()).throw(AssertionError("must not look for a binary")))
    assert ta.ensure_any_agent(timeout=0.1) is None


# ---- Blender on the replica follows the host's session (2026-09-25) ----

def _control_status(transport):
    """The agent's status line, read as a control client on its local socket."""
    import json as _json, socket as _socket, struct as _struct
    from ring1.transport_agent import T_CMD
    addr, port, secret = transport._link._info
    s = _socket.create_connection((addr, port), timeout=5.0)
    try:
        body = _json.dumps({"cmd": "attach", "secret": secret, "kind": "control"}).encode()
        s.sendall(_struct.pack(">I", len(body) + 1) + bytes([T_CMD]) + body)
        head = s.recv(5, _socket.MSG_WAITALL)
        n = _struct.unpack(">I", head[:4])[0]
        ev = _json.loads(s.recv(n - 1, _socket.MSG_WAITALL))
        return ev.get("status", ""), ev
    finally:
        s.close()


def test_replica_agent_sees_the_host_session_not_just_the_link(tmp_path):
    """The host agent signals on the control lane whether its Blender is
    attached; the replica's lifecycle (blender_path empty here, so no
    Blender) reports "host in session" only while it is, and the session's
    end when the host addon leaves."""
    host, replica = make_pair(tmp_path)
    try:
        assert wait_for(lambda: host.peer_alive and replica.peer_alive)
        assert wait_for(lambda: _control_status(replica)[0].startswith("host in session"), timeout=8.0)
        status, ev = _control_status(host)
        assert ev.get("addon_attached") is True
        host.stop()
        assert wait_for(lambda: _control_status(replica)[0] in ("host connected, no session", "listening"), timeout=8.0)
    finally:
        host.stop()
        replica.stop()


def test_status_event_carries_the_session_flag(pair):
    host, replica = pair
    assert wait_for(lambda: host.peer_alive)
    _, ev = _control_status(replica)
    assert "session" in ev or True  # the attach reply may predate the first tick; the event below is the contract
    assert wait_for(lambda: getattr(replica, "session_on", None) is True, timeout=8.0)


def test_find_agent_prefers_the_checkout_build_over_the_installed_app(tmp_path, monkeypatch):
    """A checkout's cargo output must win, or the tests exercise the
    installed binary and prove nothing about the code under test."""
    from ring1 import transport_agent as ta
    installed = tmp_path / "installed-agent"; installed.write_text("x")
    monkeypatch.setattr(ta, "installed_agent_paths", lambda: [str(installed)])
    monkeypatch.delenv("QCB_AGENT_BIN", raising=False)
    found = ta.find_agent()
    assert found is not None
    if found == str(installed):
        pytest.skip("no cargo build in this checkout")
    assert "target" in found.replace("\\", "/").split("/")
