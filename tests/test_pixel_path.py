"""build_command: low-latency flags, latency plumbing, passphrase append.

ring0/pixel_path.py is deliberately bpy-free — testable as a file import.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring0 import pixel_path  # noqa: E402


def test_command_is_low_latency(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = pixel_path.build_command(
        "ffmpeg.exe", "hevc_10_420_100",
        "srt://0.0.0.0:9998?mode=listener&latency=120000", "",
    )
    joined = " ".join(cmd)
    # NVENC's default output delay buffers ~4 frames — must be zeroed.
    assert "-tune ull" in joined and "-delay 0" in joined and "-bf 0" in joined
    assert "latency=120000" in joined
    assert "-b:v 100M" in joined


def test_passphrase_appended_once(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = pixel_path.build_command(
        "ffmpeg.exe", "hevc_10_420_50",
        "srt://0.0.0.0:9998?mode=listener&latency=300000", "secret123456",
    )
    url = cmd[-1]
    assert url.count("passphrase=") == 1 and "pbkeylen=16" in url
    cmd2 = pixel_path.build_command(
        "ffmpeg.exe", "hevc_10_420_50", url, "secret123456"
    )
    assert cmd2[-1].count("passphrase=") == 1  # already present: not doubled


def test_fatal_output_lines_arm_the_reaper():
    # macOS avfoundation can keep ffmpeg alive after the SRT viewer leaves;
    # these stderr lines are what the supervisor kills on.
    assert pixel_path.is_fatal_output_line("[out#0/mpegts @ 0x1] Error muxing a packet")
    assert pixel_path.is_fatal_output_line("[out#0/mpegts @ 0x1] Error closing file: Input/output error")
    assert not pixel_path.is_fatal_output_line("[hevc @ 0x1] Error constructing the frame RPS.")


def test_native_pipeline_shape(monkeypatch):
    # The helper encodes; ffmpeg is only the mux to SRT, on the helper's stdout.
    cap, mux = pixel_path.build_native_pipeline(
        "qcb-capture-win.exe", "ffmpeg.exe", "hevc_10_420_50",
        "srt://0.0.0.0:9998?mode=listener&latency=120000", "secret123456",
    )
    assert cap[0] == "qcb-capture-win.exe" and "--10bit" in cap and "--bitrate" in cap
    assert cap[cap.index("--bitrate") + 1] == "50"
    assert cap[cap.index("--gop") + 1] == cap[cap.index("--fps") + 1]  # one-second GOP
    joined = " ".join(mux)
    assert "-f hevc" in joined and "-i pipe:0" in joined and "-c copy" in joined
    assert "-c:v" not in joined  # no encoding in the mux
    assert mux[-1].count("passphrase=") == 1 and "pbkeylen=16" in mux[-1]


def test_native_capture_can_be_disabled(monkeypatch):
    monkeypatch.setenv("QCB_CAPTURE_NATIVE", "0")
    assert pixel_path.find_native_capture() is None
