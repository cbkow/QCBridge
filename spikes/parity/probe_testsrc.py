"""Synthetic probe sender: frames stamped with the probe strip, no Blender.

Isolates encode + transport + decode, so transports (SRT settings, Kyber)
can be compared with identical input on any OS. Random noise below the strip
makes the encoder actually spend its bitrate.

  python probe_testsrc.py --srt-listen 0.0.0.0:9998 --latency 120 --token dev
  python probe_testsrc.py --stdout | <sender>              (Annex-B HEVC)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from _common import probe, resolve_ffmpeg, srt_url


EXTRA_VT: list[str] = [a for a in os.environ.get("QCB_VT_EXTRA", "").split() if a]


def encoder_args(fps: int, bitrate: str, encoder: str) -> list[str]:
    if encoder == "auto":
        encoder = {"darwin": "videotoolbox", "win32": "nvenc"}.get(sys.platform, "x265")
    gop = ["-g", str(fps)]
    if encoder == "videotoolbox":
        return ["-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-pix_fmt", "p010le",
                "-realtime", "1", "-prio_speed", "1", *EXTRA_VT, "-b:v", bitrate, *gop]
    if encoder == "nvenc":
        return ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                "-preset", "p4", "-tune", "ull", "-delay", "0", "-bf", "0",
                "-b:v", bitrate, *gop]
    return ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p10le", "-b:v", bitrate, *gop]


def build_frame(width: int, height: int, bits: list[int], noise: bool) -> bytes:
    b = probe.BLOCK_PX
    frame = bytearray(os.urandom(width * height)) if noise else bytearray(width * height)
    band = 3 * b
    top = height - 48 - band  # same place as the addon: bottom-left, 48 px up
    frame[top * width: (top + band) * width] = bytes(band * width)  # backdrop
    for i, bit in enumerate(bits):
        if bit:
            x = b + i * b
            for y in range(top + b, top + 2 * b):
                start = y * width + x
                frame[start: start + b] = b"\xeb" * b
    return bytes(frame)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument("--srt-listen", metavar="HOST:PORT")
    out.add_argument("--stdout", action="store_true", help="Annex-B HEVC on stdout")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", default="50M")
    ap.add_argument("--encoder", default="auto", choices=["auto", "videotoolbox", "nvenc", "x265"])
    ap.add_argument("--latency", type=int, default=120, help="SRT latency, ms")
    ap.add_argument("--token", default="")
    ap.add_argument("--no-noise", action="store_true")
    ap.add_argument("--seconds", type=float, default=0, help="0 = run until killed")
    ap.add_argument("--ffmpeg", default="")
    args = ap.parse_args()

    width, height = (int(v) for v in args.size.lower().split("x"))
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning",
           "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
           "-r", str(args.fps), "-i", "-",
           *encoder_args(args.fps, args.bitrate, args.encoder)]
    if args.stdout:
        # AUD NALs mark access-unit boundaries for pipe consumers.
        cmd += ["-bsf:v", "hevc_metadata=aud=insert", "-f", "hevc", "-"]
    else:
        host, port = args.srt_listen.rsplit(":", 1)
        cmd += ["-f", "mpegts", srt_url(host, int(port), "listener", args.latency, args.token)]
    print(" ".join(cmd), file=sys.stderr, flush=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=None if args.stdout else subprocess.DEVNULL)

    period = 1.0 / args.fps
    seq = 0
    next_t = time.perf_counter()
    deadline = time.monotonic() + args.seconds if args.seconds else None
    late = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            # Feed at full fps always: rawvideo has no PTS, so a sparse feed
            # runs the stream clock slow (stage-0 lesson).
            frame = build_frame(width, height, probe.encode_bits(time.time(), seq), not args.no_noise)
            proc.stdin.write(frame)
            seq += 1
            next_t += period
            wait = next_t - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            else:
                late += 1
                if wait < -period:
                    next_t = time.perf_counter()  # don't burst to catch up
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        print(f"sent {seq} frames, {late} late", file=sys.stderr, flush=True)
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
