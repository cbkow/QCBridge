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
