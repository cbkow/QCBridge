"""Probe reader: decode a stream, read the probe strip, log latency.

Run it on the HOST machine (next to where QCView would run): the stamp in
the strip is the host's time.time(), so `now - stamp` is motion-to-decode
on one clock in every OS pairing.

  python probe_reader.py --srt 10.0.0.5:9998 --latency 120 --token dev --out run.jsonl
  kyber-recv ... | python probe_reader.py --stdin --out run.jsonl

Measures up to decoded frame; present/scanout is not included. Each
sequence number is scored the first time it appears (later repeats of the
same stamp are stale frames, counted separately).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time

from _common import percentile, probe, resolve_ffmpeg, srt_url

_SIZE_RE = re.compile(r"Stream #0:0.*?: Video: rawvideo.*?, (\d{2,5})x(\d{2,5})")


def summarize(lat: list[float], frames: int, fails: int, stale: int, label: str) -> str:
    return (
        f"[{label}] frames={frames} scored={len(lat)} stale={stale} fails={fails} "
        f"p50={percentile(lat, 50):.1f} p95={percentile(lat, 95):.1f} "
        f"p99={percentile(lat, 99):.1f} ms"
    )


def read_exact(stream, n: int) -> bytes:
    """Unbuffered pipes return short reads; a frame is only whole at n."""
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            return b""
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--srt", metavar="HOST:PORT", help="call an SRT listener")
    src.add_argument("--stdin", action="store_true", help="Annex-B HEVC on stdin")
    ap.add_argument("--latency", type=int, default=120, help="SRT latency, ms")
    ap.add_argument("--token", default="")
    ap.add_argument("--band", type=int, default=128, help="rows from the bottom to scan")
    ap.add_argument("--crop", default="", help="W:H:X:Y region to scan instead of the top band "
                    "(e.g. a replica window inside a full-display capture)")
    ap.add_argument("--hwaccel", default="auto", help="auto | none | videotoolbox | d3d11va ...")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--out", default="", help="JSONL per scored frame")
    ap.add_argument("--label", default="run")
    ap.add_argument("--ffmpeg", default="")
    args = ap.parse_args()

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    hw = args.hwaccel
    if hw == "auto":
        hw = {"darwin": "videotoolbox", "win32": "d3d11va"}.get(sys.platform, "none")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
           "-fflags", "nobuffer", "-flags", "low_delay",
           "-probesize", "500000", "-analyzeduration", "500000"]
    if hw != "none":
        cmd += ["-hwaccel", hw]
    if args.stdin:
        cmd += ["-f", "hevc", "-i", "-"]
    else:
        host, port = args.srt.rsplit(":", 1)
        cmd += ["-i", srt_url(host, int(port), "caller", args.latency, args.token)]
    crop = args.crop or f"iw:{args.band}:0:ih-{args.band}"
    cmd += ["-map", "0:v:0", "-fps_mode", "passthrough", "-vf", f"crop={crop},format=gray",
            "-f", "rawvideo", "-"]
    print(" ".join(cmd), file=sys.stderr, flush=True)
    proc = subprocess.Popen(cmd, stdin=None if args.stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    size: list[int] = []
    got_size = threading.Event()

    def pump_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if not got_size.is_set():
                m = _SIZE_RE.search(line)
                if m:
                    size[:] = [int(m.group(1)), int(m.group(2))]
                    got_size.set()
            if "rror" in line or "arning" in line:
                print(f"ffmpeg: {line}", file=sys.stderr, flush=True)

    threading.Thread(target=pump_stderr, daemon=True).start()
    if not got_size.wait(timeout=30):
        proc.kill()
        sys.exit("no video stream within 30 s")
    width, band = size
    frame_bytes = width * band
    block = probe.BLOCK_PX
    print(f"decoding {width}x{band} band", file=sys.stderr, flush=True)

    out = open(args.out, "a", encoding="utf-8") if args.out else None
    lock: tuple[int, int] | None = None  # (row, x0)
    seen_last = -1
    lat: list[float] = []
    frames = fails = stale = 0
    started = time.monotonic()
    last_report = started
    try:
        while True:
            buf = read_exact(proc.stdout, frame_bytes)
            now = time.time()
            if not buf:
                break
            frames += 1
            decoded = None
            if lock is not None:
                row = buf[lock[0] * width: (lock[0] + 1) * width]
                decoded = probe.decode_bits(probe.sample_row(row, lock[1]))
            if decoded is None:
                lock = None
                for y in range(block // 2, band, block // 2):
                    row = buf[y * width: (y + 1) * width]
                    x0 = probe.find_strip(row)
                    if x0 is not None:
                        # Center vertically too: edge rows blur under 4:2:0.
                        def ok(yy: int) -> bool:
                            if not 0 <= yy < band:
                                return False
                            r = buf[yy * width: (yy + 1) * width]
                            return probe.decode_bits(probe.sample_row(r, x0)) is not None
                        lo = hi = y
                        while ok(lo - 1):
                            lo -= 1
                        while ok(hi + 1):
                            hi += 1
                        y = (lo + hi) // 2
                        row = buf[y * width: (y + 1) * width]
                        lock = (y, x0)
                        print(f"strip locked at row {y}, x {x0} (in scanned region)",
                              file=sys.stderr, flush=True)
                        decoded = probe.decode_bits(probe.sample_row(row, x0))
                        break
            if decoded is None:
                fails += 1
            else:
                t_ms, seq = decoded
                if seq == seen_last:
                    stale += 1
                else:
                    seen_last = seq
                    ms = probe.latency_ms(now, t_ms)
                    lat.append(ms)
                    if out:
                        out.write(json.dumps({"label": args.label, "t": round(now, 4),
                                              "seq": seq, "lat_ms": ms}) + "\n")
            if time.monotonic() - last_report >= 5:
                last_report = time.monotonic()
                print(summarize(lat[-600:], frames, fails, stale, args.label + " last~600"),
                      file=sys.stderr, flush=True)
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        proc.kill()
        if out:
            out.close()
        print(summarize(lat, frames, fails, stale, args.label), flush=True)


if __name__ == "__main__":
    main()
