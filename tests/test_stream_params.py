import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.stream_params import StreamParams, WIRE_FORMAT  # noqa: E402


def test_defaults_and_clamps():
    p = StreamParams.from_prefs(SimpleNamespace())
    assert (p.bitrate_mbps, p.width, p.fps) == (50, 2560, 30)
    p = StreamParams.from_prefs(SimpleNamespace(stream_bitrate_mbps=9999, stream_width=1001, stream_fps=1))
    assert p.bitrate_mbps == 400 and p.width == 1000 and p.fps == 5   # width even, everything clamped
    p = StreamParams.from_prefs(SimpleNamespace(stream_bitrate_mbps="x"))
    assert p.bitrate_mbps == 50


def test_helper_argv_and_descriptor():
    p = StreamParams(bitrate_mbps=30, width=1920, fps=24)
    assert p.helper_argv("/x/qcb-encode-mac", "qcb-1-1") == [
        "/x/qcb-encode-mac", "--shm", "qcb-1-1", "--fps", "24", "--bitrate", "30", "--gop", "24"]
    d = p.descriptor()
    assert d["format"] == WIRE_FORMAT and d["format"]["transfer"] == "pq" and d["format"]["anchor_nits"] == 100.0
    assert d["width"] == 1920 and d["fps"] == 24 and d["bitrate_mbps"] == 30
