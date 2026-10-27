"""The replica pixel path's command lines: readback helper + mux, and the
screen-capture fallback. ring0/pixel_path.py is deliberately bpy-free —
testable as a file import."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring0 import pixel_path  # noqa: E402
from ring1.stream_params import StreamParams  # noqa: E402

URL = "srt://0.0.0.0:9998?mode=listener&latency=120000"


def test_readback_pipeline_shape():
    params = StreamParams(bitrate_mbps=40, width=2560, fps=25)
    cap, mux = pixel_path.build_encode_pipeline("qcb-encode-mac", "ffmpeg", params, "qcb-7-1", URL, "secret123456")
    assert cap == ["qcb-encode-mac", "--shm", "qcb-7-1", "--fps", "25", "--bitrate", "40", "--gop", "25"]
    joined = " ".join(mux)
    assert "-f hevc" in joined and "-i pipe:0" in joined and "-c copy" in joined
    assert "-c:v" not in joined  # no encoding in the mux
    # the wire's VUI tags are pinned in the mux whatever the vendor wrote
    assert "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9:video_full_range_flag=0" in mux
    assert mux[-1].count("passphrase=") == 1 and "pbkeylen=16" in mux[-1]
    assert mux[-1].startswith("srt://0.0.0.0:9998?mode=listener&latency=120000")


def test_passphrase_not_doubled():
    url = pixel_path.build_mux("ffmpeg", 30, URL, "secret123456")[-1]
    assert pixel_path.build_mux("ffmpeg", 30, url, "secret123456")[-1].count("passphrase=") == 1


def test_screen_fallback_is_low_latency(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = pixel_path.build_command("ffmpeg.exe", StreamParams(bitrate_mbps=100, fps=30), URL, "")
    joined = " ".join(cmd)
    # NVENC's default output delay buffers ~4 frames — must be zeroed.
    assert "-tune ull" in joined and "-delay 0" in joined and "-bf 0" in joined
    assert "latency=120000" in joined and "-b:v 100M" in joined
    assert "p010le" in joined and "main10" in joined and "yuv444" not in joined
    monkeypatch.setattr(sys, "platform", "darwin")
    cmd = pixel_path.build_command("ffmpeg", StreamParams(), URL, "pw")
    assert "hevc_videotoolbox" in cmd and cmd[-1].count("passphrase=") == 1


def test_fatal_output_lines_arm_the_reaper():
    assert pixel_path.is_fatal_output_line("[out#0/mpegts @ 0x1] Error muxing a packet")
    assert pixel_path.is_fatal_output_line("[out#0/mpegts @ 0x1] Error closing file: Input/output error")
    assert not pixel_path.is_fatal_output_line("[hevc @ 0x1] Error constructing the frame RPS.")


def test_helper_lookup_honours_the_env(monkeypatch, tmp_path):
    fake = tmp_path / "qcb-encode-x"
    fake.write_bytes(b"")
    monkeypatch.setenv("QCB_PIXEL_HELPER", str(fake))
    assert pixel_path.find_encode_helper() == str(fake)
