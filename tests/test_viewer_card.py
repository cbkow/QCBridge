"""QCView's card (ring0/viewer_card.py is bpy-free): the folder rule, the
listener URL, the content digest and the atomic write/remove."""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring0 import viewer_card as vc  # noqa: E402


def test_listener_url_carries_port_latency_and_passphrase():
    assert vc.listener_url(9998, 120, "abc") == "srt://0.0.0.0:9998?mode=listener&latency=120000&passphrase=abc&pbkeylen=16"
    assert vc.listener_url(9000, 60, "") == "srt://0.0.0.0:9000?mode=listener&latency=60000"


def test_inputs_dir_follows_the_env_override_then_the_os():
    os.environ["QCV_INPUTS_DIR"] = "/tmp/x-inputs"
    try:
        assert vc.inputs_dir() == "/tmp/x-inputs"
    finally:
        del os.environ["QCV_INPUTS_DIR"]
    d = vc.inputs_dir()
    assert d.endswith(os.path.join("QCView", "Inputs"))


def test_build_write_digest_remove_round_trip():
    with tempfile.TemporaryDirectory() as d:
        os.environ["QCV_INPUTS_DIR"] = os.path.join(d, "Inputs")   # created on demand
        try:
            card = vc.build(name="Live · a.blend", port=9998, latency_ms=120, passphrase="pp",
                            colour={"wire": "pq-bt2020-100", "view": "Standard"}, rows=[("View", "Standard", "Standard")],
                            mismatches=[("info", "config", "x")], stream={"port": 9998, "push": {"addr": "10.0.0.2"}},
                            note="connected", host_name="mac")
            assert card["kind"] == "blender" and card["url"].startswith("srt://0.0.0.0:9998?mode=listener")
            d1 = vc.content_digest(card)
            card2 = dict(card, ts=card["ts"] + 100)
            assert vc.content_digest(card2) == d1          # the timestamp is not content
            card3 = dict(card, colour={"wire": "pq-bt2020-100", "view": "AgX"})
            assert vc.content_digest(card3) != d1
            path = vc.write(card)
            assert path == vc.card_path() and os.path.isfile(path)
            back = json.load(open(path))
            assert back["colour"]["view"] == "Standard" and back["rows"][0] == ["View", "Standard", "Standard"]
            assert not [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".blender-")]   # no temp left
            assert vc.remove() is True and not os.path.exists(path)
            assert vc.remove() is False
        finally:
            del os.environ["QCV_INPUTS_DIR"]
