"""Replica pixel path: spawn and supervise the encoder (DESIGN-NOTES pixels).

0.3: no screen capture. Blender's addon reads its own viewport pixels
(ring0/engine_qcb) into a shared-memory ring; the encode helper
(agent/encode-mac, agent/encode-win) converts them to PQ/BT.2020 10-bit
and encodes HEVC Main10 on the GPU, Annex-B on its stdout; ffmpeg wraps
that into MPEG-TS over SRT with `-c copy`. The addon configures, spawns and
supervises; it never touches a pixel.

`pixel_source="screen"` keeps the pre-0.3 path (ddagrab/avfoundation →
hardware HEVC in ffmpeg) as the escape hatch for a Blender release that
breaks the engine subclass. All endpoints come from preferences — nothing
is hardcoded (the 127.0.0.1 lesson).
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
    from ..ring1.stream_params import StreamParams
except ImportError:  # file-imported by tests with qcbridge/ on sys.path
    from ring1.stream_params import StreamParams

_RESTART_BACKOFF = 2.0
_last_argv: list[str] = []   # the helper command line last spawned (smokes read it)


def last_argv() -> list[str]:
    return list(_last_argv)

# The encode helper: shared-memory ring -> PQ/BT.2020 P010/x420 on the GPU
# -> hardware HEVC Main10, Annex-B with AUDs on stdout. Same lookup as the
# agent binary: explicit env, then the extension's bin/, then the cargo and
# swiftc outputs. The 0.2 capture helpers have different names on purpose:
# a stale one is never picked up.
_HELPER_EXE = "qcb-encode-win.exe" if sys.platform == "win32" else "qcb-encode-mac"

# VUI tags pinned in the mux, whatever the vendor encoder wrote: BT.2020
# primaries (9), SMPTE ST 2084 (16), BT.2020 non-constant luminance (9),
# limited range. Copy-safe: hevc_metadata rewrites parameter sets only.
_WIRE_BSF = ("hevc_metadata=colour_primaries=9:transfer_characteristics=16"
             ":matrix_coefficients=9:video_full_range_flag=0")


def installed_helper_paths() -> list[str]:
    """Where the installers put the encode helper, beside the agent
    (release 0.3.0): the PKG's app bundle on macOS, the Inno installer's
    folder on Windows — the same homes as transport_agent.installed_agent_paths.
    A helper extracted from a downloaded extension zip would carry the
    quarantine flag and be refused on macOS; the installed one is notarized."""
    if sys.platform == "darwin":
        return ["/Applications/QCBridge/QCBridge Agent.app/Contents/MacOS/qcb-encode-mac"]
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [os.path.join(pf, "QCBridge", "qcb-encode-win.exe")]
    return []


def find_encode_helper() -> str | None:
    here = Path(__file__).resolve()
    addon = here.parents[1]          # qcbridge/
    repo = here.parents[2]
    explicit = os.environ.get("QCB_PIXEL_HELPER", "")
    for cand in [explicit, str(addon / "bin" / _HELPER_EXE)] + installed_helper_paths():
        if cand and os.path.isfile(cand):
            return cand
    builds = [
        str(repo / "agent" / "target" / prof / _HELPER_EXE) for prof in ("release", "debug")
    ] + [
        str(repo / "agent" / "encode-win" / "target" / prof / _HELPER_EXE) for prof in ("release", "debug")
    ]
    builds = [b for b in builds if os.path.isfile(b)]
    return max(builds, key=os.path.getmtime) if builds else None


def _with_passphrase(url: str, passphrase: str) -> str:
    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}&pbkeylen=16"
    return url


def build_mux(ffmpeg: str, fps: int, srt_url: str, passphrase: str) -> list[str]:
    """ffmpeg as the mux only: Annex-B HEVC on stdin -> MPEG-TS over SRT.
    The helper gets a keyframe request on spawn, so VPS/SPS/PPS lead its
    input and a tiny probe is enough; without that, ffmpeg's default 5 MB
    probe on a raw HEVC pipe kept a joining viewer waiting long enough to
    give up (found 2026-09-23 on Windows)."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-probesize", "65536", "-analyzeduration", "0",
        "-f", "hevc", "-framerate", str(fps), "-i", "pipe:0",
        "-c", "copy", "-bsf:v", _WIRE_BSF, "-fps_mode", "passthrough", "-flush_packets", "1",
        "-f", "mpegts", _with_passphrase(srt_url, passphrase),
    ]


def build_encode_pipeline(
    helper: str, ffmpeg: str, params: StreamParams, ring_name: str, srt_url: str, passphrase: str
) -> tuple[list[str], list[str]]:
    """(helper argv, mux argv) for the readback path."""
    return params.helper_argv(helper, ring_name), build_mux(ffmpeg, params.fps, srt_url, passphrase)


_proc: subprocess.Popen | None = None       # ffmpeg (capture+encode, or the mux)
_cap_proc: subprocess.Popen | None = None   # the native capture helper, when in use
_thread: threading.Thread | None = None
_stop = threading.Event()
_status = "off"


def build_command(ffmpeg: str, params: StreamParams, srt_url: str, passphrase: str) -> list[str]:
    """The screen-capture fallback (`pixel_source="screen"`): ffmpeg captures
    the display and encodes 10-bit 4:2:0 HEVC in hardware. Display-referred
    BT.709, what the 0.2 line streamed."""
    bitrate = f"{params.bitrate_mbps}M"
    url = _with_passphrase(srt_url, passphrase)
    fps = params.fps
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


def _supervise_native(cap_cmd_fn, mux_cmd: list[str], log_path: Path) -> None:
    """Two children, one pipe: the helper's stdout is the mux's stdin. Either
    one leaving takes the other down and both come back after the backoff —
    the mux exits when the SRT viewer leaves (one viewer per listener), the
    helper when the ring is retired (exit 7: a bigger ring under a new
    name, which `cap_cmd_fn()` returns on the respawn) or Blender is gone."""
    global _proc, _cap_proc, _status, _last_argv
    outdated = {"strip_matrix": False}   # an old helper rejects --matrix (exit 2 at once)
    while not _stop.is_set():
        cap_cmd = cap_cmd_fn()
        if not cap_cmd:
            _status = "waiting for the frame ring"
            _stop.wait(_RESTART_BACKOFF / 4)
            continue
        if outdated["strip_matrix"] and "--matrix" in cap_cmd:
            i = cap_cmd.index("--matrix")
            cap_cmd = cap_cmd[:i] + cap_cmd[i + 2:]
        spawned_at = time.monotonic()
        _last_argv = list(cap_cmd)
        with open(log_path, "ab") as log:
            log.write(f"\n--- spawn (readback) {time.ctime()} ---\n".encode())
            log.flush()
            try:
                _cap_proc = subprocess.Popen(
                    cap_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                _proc = subprocess.Popen(
                    mux_cmd, stdin=_cap_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
            except OSError as exc:
                _status = f"encoder spawn failed: {exc}"
                if _cap_proc is not None and _cap_proc.poll() is None:
                    _cap_proc.kill()
                return
            _cap_proc.stdout.close()  # the mux owns that end now
            try:
                _cap_proc.stdin.write(b"key\n")  # parameter sets first, for the mux and its viewer
                _cap_proc.stdin.flush()
            except OSError:
                pass
            _status = "streaming (readback)"
            pumps = [
                threading.Thread(target=_pump_log, args=(_proc, log), name="qcb-pixel-log", daemon=True),
                threading.Thread(target=_pump_prefixed, args=(_cap_proc, log, b""), name="qcb-capture-log", daemon=True),
            ]
            for t in pumps:
                t.start()
            while not _stop.is_set() and _proc.poll() is None and _cap_proc.poll() is None:
                _stop.wait(0.25)
            mux_exited_first = _proc.poll() is not None and _cap_proc.poll() is None
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
        if (_cap_proc.returncode == 2 and time.monotonic() - spawned_at < 1.0
                and "--matrix" in cap_cmd and not outdated["strip_matrix"]):
            # A helper from before the working-space matrix: it exits 2 on
            # the unknown flag. Run it the old way (Rec.709 assumed) and say
            # so — the panel shows this status.
            outdated["strip_matrix"] = True
            with open(log_path, "ab") as log:
                log.write(b"\n--- helper rejects its arguments (predates --matrix?): respawning without it ---\n")
            _status = "encoder helper outdated — rebuild (wire assumes Linear Rec.709)"
            continue
        _status = f"stream ended (mux {_proc.returncode}, encoder {_cap_proc.returncode}) — restarting"
        # The mux leaving on its own — a viewer gone, or the listen timed out
        # with nobody there — is the normal case: relisten at once, no gap
        # for the next viewer to fall into. The backoff is for the helper
        # dying (a crash loop would otherwise spin).
        if _cap_proc.returncode is not None and _cap_proc.returncode != 0 and mux_exited_first is False:
            _stop.wait(_RESTART_BACKOFF / 4)
    _status = "off"


_ring_name: str | None = None   # the frame ring the helper reads; swapped on a regrow
_source = "readback"


def restart_ring(ring_name: str) -> None:
    """The producer retired its ring and made a bigger one: the helper is
    exiting (7); its respawn reads the new name."""
    global _ring_name
    _ring_name = ring_name
    child = _cap_proc
    if child is not None and child.poll() is None:
        try:
            child.stdin.write(b"quit\n")
            child.stdin.flush()
        except OSError:
            pass


def start(ffmpeg: str, srt_url: str, passphrase: str, params: StreamParams | None = None,
          source: str = "readback", ring_name: str | None = None) -> None:
    """`source` — "readback" (the helper reads Blender's frame ring named
    `ring_name`) or "screen" (ffmpeg captures the display). Readback with
    no helper installed falls back to screen and says so."""
    global _thread, _status, _ring_name, _source
    if _thread is not None:
        return
    params = params or StreamParams()
    log_path = Path(tempfile.gettempdir()) / "qcbridge-pixelpath.log"
    _stop.clear()
    _status = "starting"
    _ring_name = ring_name
    helper = find_encode_helper() if source == "readback" else None
    if source == "readback" and helper is None:
        _status = "encoder helper not found — screen capture fallback"
        source = "screen"
    _source = source
    if source == "readback":
        mux_cmd = build_mux(ffmpeg, params.fps, srt_url, passphrase)

        def cap_cmd_fn():
            return params.helper_argv(helper, _ring_name) if _ring_name else None

        _thread = threading.Thread(
            target=_supervise_native, args=(cap_cmd_fn, mux_cmd, log_path), name="qcb-pixel", daemon=True
        )
    else:
        cmd = build_command(ffmpeg, params, srt_url, passphrase)
        _thread = threading.Thread(
            target=_supervise, args=(cmd, log_path), name="qcb-pixel", daemon=True
        )
    _thread.start()


def source() -> str:
    return _source


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
