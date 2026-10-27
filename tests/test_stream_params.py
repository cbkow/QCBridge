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


def test_with_working_adds_the_matrix_to_argv_and_descriptor():
    from ring1 import colour_state as cs
    from ring1 import wire_format as wf
    p = StreamParams(bitrate_mbps=30, width=1920, fps=24)
    assert "--matrix" not in p.helper_argv("h", "r")          # pre-awareness shape
    assert "working" not in p.descriptor()["format"]
    q = p.with_working({"name": "Linear Rec.709", "to_xyz": cs.matrix_rows(cs.M_709_TO_XYZ), "assumed": False})
    argv = q.helper_argv("h", "r")
    assert argv[-2] == "--matrix" and argv[-1].count(",") == 8
    assert cs.matrix_close(wf.parse_matrix_arg(argv[-1]), wf.M_709_TO_2020)
    fmt = q.descriptor()["format"]
    assert fmt["working"]["name"] == "Linear Rec.709" and fmt["working"]["assumed"] is False
    assert cs.matrix_close(fmt["working"]["to_bt2020"], wf.M_709_TO_2020)
    assert {k: v for k, v in fmt.items() if k != "working"} == WIRE_FORMAT
    # no OCIO: assumed Rec.709, said so
    a = p.with_working({"name": "", "to_xyz": None, "assumed": True})
    assert a.working_assumed and a.working_space == "Linear Rec.709"
    assert a.descriptor()["format"]["working"]["assumed"] is True
