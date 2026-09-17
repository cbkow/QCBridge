"""Replica pixel path: spawn/supervise the capture-encode ffmpeg (decision #13).

Windows production path: ddagrab (Desktop Duplication) → NVENC, fully
GPU-resident (~15% of one core at 1440p30) — with kiosk mode the display IS
the viewport, no crop. macOS: avfoundation screen capture → VideoToolbox as
the dev fallback (the proper SCK helper is a parallel track).

The addon configures, spawns, and supervises; ffmpeg owns pixels→encode→SRT.
All endpoints come from preferences — nothing is hardcoded (the 127.0.0.1
lesson, results/vpn-stream-notes.md).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    from ..ring1 import probe
except ImportError:  # file-imported by tests with qcbridge/ on sys.path
    from ring1 import probe

_RESTART_BACKOFF = 2.0

# Encoder rungs judged on a production scene (windows-v0-matrix/notes.md).
_RUNG_BITRATE = {
    "hevc_10_420_100": "100M",
    "hevc_10_420_50": "50M",
    "hevc_10_444_50": "50M",
}

_proc: subprocess.Popen | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()
_status = "off"


def build_command(ffmpeg: str, rung: str, srt_url: str, passphrase: str) -> list[str]:
    bitrate = _RUNG_BITRATE.get(rung, "100M")
    url = srt_url
    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}&pbkeylen=16"

    fps = probe.capture_fps()  # 30 unless the parity spike overrides it
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    if sys.platform == "win32":
        cmd += ["-filter_complex", f"ddagrab=output_idx=0:framerate={fps}:draw_mouse=0"]
        if rung == "hevc_10_444_50":
            # True 4:4:4 needs the CPU path — NVENC silently downgrades 4:4:4
            # requests on GPU frames (verify receiver-side, always).
            cmd += ["-vf", "hwdownload,format=bgra,format=yuv444p10le"]
            cmd += ["-c:v", "hevc_nvenc", "-profile:v", "rext"]
        else:
            cmd += ["-c:v", "hevc_nvenc", "-profile:v", "main10"]
        # -delay 0: NVENC's default output delay holds ~4 frames (~130 ms)
        # for nothing we need; ull + bf 0 keep the encoder frame-in/frame-out.
        cmd += [
            "-preset", "p4", "-tune", "ull", "-delay", "0", "-bf", "0",
            "-b:v", bitrate, "-g", str(fps),
        ]
    elif sys.platform == "darwin":
        cmd += [
            "-f", "avfoundation", "-capture_cursor", "0", "-framerate", str(fps),
            "-i", "Capture screen 0",
            "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
            "-pix_fmt", "p010le", "-b:v", bitrate, "-g", str(fps), "-realtime", "1",
        ]
    else:
        raise RuntimeError("no capture path for this platform")
    cmd += ["-f", "mpegts", url]
    return cmd


# An SRT listener serves ONE viewer; when it leaves, the mux fails and
# ffmpeg should exit so we respawn for the next one. On macOS the
# avfoundation input thread can keep ffmpeg alive (ignoring SIGTERM) after
# that error — found live 2026-09-17 — so a fatal output line arms a kill.
_FATAL_OUTPUT = ("Error muxing a packet", "Error writing trailer", "Error closing file")
_FATAL_GRACE_S = 2.0


def is_fatal_output_line(line: str) -> bool:
    return any(marker in line for marker in _FATAL_OUTPUT)


def _pump_log(proc: subprocess.Popen, log) -> None:
    """Copy ffmpeg's stderr to the log; kill a process that reported a fatal
    output error but hasn't exited within the grace period."""
    fatal_at = None
    for raw in iter(proc.stderr.readline, b""):
        log.write(raw)
        log.flush()
        if fatal_at is None and is_fatal_output_line(raw.decode(errors="replace")):
            fatal_at = time.monotonic()
            threading.Thread(
                target=_kill_if_stuck, args=(proc,), name="qcb-pixel-reap", daemon=True
            ).start()


def _kill_if_stuck(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=_FATAL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()


def _supervise(cmd: list[str], log_path: Path) -> None:
    global _proc, _status
    while not _stop.is_set():
        with open(log_path, "ab") as log:
            log.write(f"\n--- spawn {time.ctime()} ---\n".encode())
            log.flush()
            try:
                _proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
            except OSError as exc:
                _status = f"ffmpeg spawn failed: {exc}"
                return
            _status = "streaming"
            pump = threading.Thread(
                target=_pump_log, args=(_proc, log), name="qcb-pixel-log", daemon=True
            )
            pump.start()
            _proc.wait()
            pump.join(timeout=2.0)
        if _stop.is_set():
            break
        _status = f"ffmpeg exited ({_proc.returncode}) — restarting"
        _stop.wait(_RESTART_BACKOFF)
    _status = "off"


def start(ffmpeg: str, rung: str, srt_url: str, passphrase: str) -> None:
    global _thread, _status
    if _thread is not None:
        return
    cmd = build_command(ffmpeg, rung, srt_url, passphrase)
    log_path = Path(tempfile.gettempdir()) / "qcbridge-pixelpath.log"
    _stop.clear()
    _status = "starting"
    _thread = threading.Thread(
        target=_supervise, args=(cmd, log_path), name="qcb-pixel", daemon=True
    )
    _thread.start()


def stop() -> None:
    global _thread, _proc
    _stop.set()
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            _proc.kill()
    if _thread is not None:
        _thread.join(timeout=5.0)
        _thread = None
    _proc = None


def status() -> str:
    return _status
