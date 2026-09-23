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

import os
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

# The native capture helper (S6 on macOS, S7 on Windows): screen -> hardware
# HEVC on the GPU, Annex-B with AUDs on stdout. When one is present the
# pixel path runs it and keeps ffmpeg only as the mux to SRT; when it is not,
# ffmpeg captures and encodes as before. Same lookup as the agent binary:
# explicit env, then the extension's bin/, then the cargo outputs.
_NATIVE_EXE = "qcb-capture-win.exe" if sys.platform == "win32" else "qcb-capture-mac"
# Rungs the helper can produce. 4:4:4 stays with ffmpeg (a CPU path there too).
_NATIVE_RUNGS = {"hevc_10_420_100": True, "hevc_10_420_50": True}


def find_native_capture() -> str | None:
    if os.environ.get("QCB_CAPTURE_NATIVE", "") == "0":
        return None
    here = Path(__file__).resolve()
    addon = here.parents[1]          # qcbridge/
    repo = here.parents[2]
    explicit = os.environ.get("QCB_CAPTURE_BIN", "")
    for cand in (explicit, str(addon / "bin" / _NATIVE_EXE)):
        if cand and os.path.isfile(cand):
            return cand
    builds = [
        str(repo / "agent" / "target" / prof / _NATIVE_EXE) for prof in ("release", "debug")
    ] + [
        str(repo / "agent" / "capture-win" / "target" / prof / _NATIVE_EXE) for prof in ("release", "debug")
    ]
    builds = [b for b in builds if os.path.isfile(b)]
    return max(builds, key=os.path.getmtime) if builds else None


def build_native_pipeline(
    capture: str, ffmpeg: str, rung: str, srt_url: str, passphrase: str
) -> tuple[list[str], list[str]]:
    """(capture argv, mux argv): the helper encodes, ffmpeg only wraps the
    Annex-B into MPEG-TS over SRT. GOP = one second, as the ffmpeg path's
    -g, so a viewer joining the listener waits at most that for a key."""
    url = srt_url
    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}&pbkeylen=16"
    fps = probe.capture_fps()
    cap = [capture, "--fps", str(fps), "--bitrate", str(rung_mbps(rung)), "--gop", str(fps)]
    if _NATIVE_RUNGS.get(rung):
        cap.append("--10bit")
    mux = [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-f", "hevc", "-framerate", str(fps), "-i", "pipe:0",
        "-c", "copy", "-fps_mode", "passthrough", "-f", "mpegts", url,
    ]
    return cap, mux

# build_command(srt_url=PIPE_OUTPUT): Annex-B HEVC with AUDs on stdout, for
# an external owner of the capture child, when there is one.
PIPE_OUTPUT = "pipe:"

# Encoder rungs judged on a production scene (windows-v0-matrix/notes.md).
_RUNG_BITRATE = {
    "hevc_10_420_100": "100M",
    "hevc_10_420_50": "50M",
    "hevc_10_444_50": "50M",
}

def capture_fps() -> int:
    return probe.capture_fps()


def rung_mbps(rung: str) -> int:
    return int(_RUNG_BITRATE.get(rung, "100M").rstrip("M"))


_proc: subprocess.Popen | None = None       # ffmpeg (capture+encode, or the mux)
_cap_proc: subprocess.Popen | None = None   # the native capture helper, when in use
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
        # ddagrab hands NVENC 8-bit BGRA frames on the GPU. Any pixel-format
        # change has to happen inside this one filtergraph (ffmpeg refuses a
        # -vf next to a -filter_complex source): download, then convert.
        # With only `-profile:v main10` on BGRA input NVENC writes an SPS
        # that says Main 10 over 8-bit samples, and a hardware decoder
        # (QCView's D3D11VA, found 2026-09-23) refuses the surface format
        # and falls to software; the macOS branch pins p010le, so does this.
        graph = f"ddagrab=output_idx=0:framerate={fps}:draw_mouse=0"
        if rung == "hevc_10_444_50":
            # True 4:4:4 needs the CPU path — NVENC silently downgrades 4:4:4
            # requests on GPU frames (verify receiver-side, always).
            graph += ",hwdownload,format=bgra,format=yuv444p10le"
            codec = ["-c:v", "hevc_nvenc", "-profile:v", "rext"]
        else:
            graph += ",hwdownload,format=bgra,format=p010le"
            codec = ["-c:v", "hevc_nvenc", "-profile:v", "main10"]
        cmd += ["-filter_complex", graph] + codec
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
    if srt_url == PIPE_OUTPUT:
        # Raw HEVC carries no timestamps, so ffmpeg defaults to constant-rate
        # output and pads with duplicate frames; at a 120 Hz capture the
        # encoder then falls behind and latency grows without bound (seen
        # live: 90 s behind after 90 s). Pass frames through as captured.
        cmd += ["-fps_mode", "passthrough",
                "-bsf:v", "hevc_metadata=aud=insert", "-f", "hevc", "-"]
    else:
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


def _pump_prefixed(proc: subprocess.Popen, log, prefix: bytes) -> None:
    for raw in iter(proc.stderr.readline, b""):
        log.write(prefix + raw)
        log.flush()


def _supervise_native(cap_cmd: list[str], mux_cmd: list[str], log_path: Path) -> None:
    """Two children, one pipe: the helper's stdout is the mux's stdin. Either
    one leaving takes the other down and both come back after the backoff —
    the mux exits when the SRT viewer leaves (one viewer per listener), the
    helper when the display changes or the desktop locks."""
    global _proc, _cap_proc, _status
    while not _stop.is_set():
        with open(log_path, "ab") as log:
            log.write(f"\n--- spawn (native) {time.ctime()} ---\n".encode())
            log.flush()
            try:
                _cap_proc = subprocess.Popen(
                    cap_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                _proc = subprocess.Popen(
                    mux_cmd, stdin=_cap_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
            except OSError as exc:
                _status = f"native capture spawn failed: {exc}"
                if _cap_proc is not None and _cap_proc.poll() is None:
                    _cap_proc.kill()
                return
            _cap_proc.stdout.close()  # the mux owns that end now
            _status = "streaming (native)"
            pumps = [
                threading.Thread(target=_pump_log, args=(_proc, log), name="qcb-pixel-log", daemon=True),
                threading.Thread(target=_pump_prefixed, args=(_cap_proc, log, b""), name="qcb-capture-log", daemon=True),
            ]
            for t in pumps:
                t.start()
            while not _stop.is_set() and _proc.poll() is None and _cap_proc.poll() is None:
                _stop.wait(0.25)
            for child in (_proc, _cap_proc):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        child.kill()
            for t in pumps:
                t.join(timeout=2.0)
        if _stop.is_set():
            break
        _status = f"stream ended (mux {_proc.returncode}, capture {_cap_proc.returncode}) — restarting"
        _stop.wait(_RESTART_BACKOFF)
    _status = "off"


def start(ffmpeg: str, rung: str, srt_url: str, passphrase: str) -> None:
    global _thread, _status
    if _thread is not None:
        return
    log_path = Path(tempfile.gettempdir()) / "qcbridge-pixelpath.log"
    _stop.clear()
    _status = "starting"
    native = find_native_capture() if rung in _NATIVE_RUNGS else None
    if native:
        cap_cmd, mux_cmd = build_native_pipeline(native, ffmpeg, rung, srt_url, passphrase)
        _thread = threading.Thread(
            target=_supervise_native, args=(cap_cmd, mux_cmd, log_path), name="qcb-pixel", daemon=True
        )
    else:
        cmd = build_command(ffmpeg, rung, srt_url, passphrase)
        _thread = threading.Thread(
            target=_supervise, args=(cmd, log_path), name="qcb-pixel", daemon=True
        )
    _thread.start()


_external = None  # (stop_fn, state_fn): something else owns the child
_EXTERNAL_STATUS = {"running": "streaming", "restarting": "ffmpeg exited — restarting",
                    "spawn_failed": "ffmpeg spawn failed", "off": "off", "": "off"}


def set_external(stop_fn=None, state_fn=None) -> None:
    """When another process owns the capture child, stop()/status()
    — called from goodbye handling, the panel and pong status — defer to it."""
    global _external
    _external = (stop_fn, state_fn) if stop_fn and state_fn else None


def stop() -> None:
    global _thread, _proc, _cap_proc
    if _external is not None:
        _external[0]()
        return
    _stop.set()
    for child in (_proc, _cap_proc):
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                child.kill()
    if _thread is not None:
        _thread.join(timeout=5.0)
        _thread = None
    _proc = None
    _cap_proc = None


def status() -> str:
    if _external is not None:
        state = _external[1]()
        return _EXTERNAL_STATUS.get(state, state)
    return _status
