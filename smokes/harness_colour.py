"""Colour harness, the comparison (numpy: run with Blender's own Python,
which has it — `blender -b --python-expr` is not needed, the binary under
Blender.app/Contents/Resources/<ver>/python/bin/ runs this directly).

  python3 smokes/harness_colour.py <work dir>

Compares, on the harness scene (harness_colour_scene.py), every stage of
the beauty pipeline against Blender's own renders (harness_colour_ref.py):

  1. the wire   — one raw yuv420p10le frame pulled off the SRT listener
                  (wire.yuv), decoded by the wire contract and the matrix
                  the replica advertised, vs ref_raw16.png (scene-linear).
                  Gate: PSNR > 30 dB, mean dE2000 < 1.0 (EEVEE: < 1.5), p99 < 6
                  (viewport vs final render bounds the PSNR near 34 dB).
  2. QCView     — QCView's capture of the stream through the link
                  (qcv_capture.png) vs ref_shown.png: the host's view for
                  the host's display, codes as shown. Gate: mean dE2000
                  < 1.5, p99 < 4.0.
  3. (optional) the replica's own window (replica_win.png), reported.

Writes <work dir>/harness.json {"pass": bool, "checks": {...}, "numbers":
{...}} and prints it. Everything it reads is produced by run_harness_colour.sh.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

W, H = 1280, 720


def png_rgb(path: str) -> np.ndarray:
    """PNG → float RGB [0..1] via ffmpeg (QCView's, SRT-capable, also decodes PNG)."""
    ff = os.environ.get("QCB_HARNESS_FFMPEG", "ffmpeg")
    out = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", path, "-pix_fmt", "rgb48le",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    probe = subprocess.run([ff.replace("ffmpeg", "ffprobe") if ff.endswith("ffmpeg") else "ffprobe",
                            "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    w, h = [int(v) for v in probe.split(",")[:2]]
    return np.frombuffer(out, dtype="<u2").reshape(h, w, 3).astype(np.float64) / 65535.0


def decode_wire(path: str, matrix_to_2020) -> np.ndarray:
    """yuv420p10le (limited, BT.2020 NCL, PQ with 100 nits = 1.0) → scene-linear
    in the WORKING space, by the inverse of the advertised working → BT.2020."""
    raw = np.fromfile(path, dtype="<u2")
    Y = raw[:W * H].reshape(H, W).astype(np.float64)
    U = raw[W * H:W * H + (W // 2) * (H // 2)].reshape(H // 2, W // 2).astype(np.float64)
    V = raw[W * H + (W // 2) * (H // 2):].reshape(H // 2, W // 2).astype(np.float64)
    U = np.repeat(np.repeat(U, 2, 0), 2, 1)
    V = np.repeat(np.repeat(V, 2, 0), 2, 1)
    Yn = (Y - 64) / 876.0
    Cb = (U - 512) / 896.0
    Cr = (V - 512) / 896.0
    Rp = Yn + 1.4746 * Cr
    Bp = Yn + 1.8814 * Cb
    Gp = (Yn - 0.2627 * Rp - 0.0593 * Bp) / 0.6780
    E = np.clip(np.stack([Rp, Gp, Bp], -1), 0, 1)
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    Ep = E ** (1 / m2)
    nits = 10000.0 * (np.maximum(Ep - c1, 0) / (c2 - c3 * Ep)) ** (1 / m1)
    lin2020 = nits / 100.0
    M = np.array(matrix_to_2020, dtype=np.float64)
    return lin2020 @ np.linalg.inv(M).T


# ---- CIE dE2000 on display-referred sRGB values (the "as shown" stage) ----

def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = np.clip(rgb, 0, 1)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], -1)


def de2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cm ** 7 / (Cm ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dL = L2 - L1
    dC = C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0, dh)
    dH = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lm = (L1 + L2) / 2
    Cmp = (C1p + C2p) / 2
    hs = h1p + h2p
    hm = np.where(np.abs(h1p - h2p) > 180, (hs + 360) / 2, hs / 2)
    hm = np.where(C1p * C2p == 0, hs, hm)
    T = (1 - 0.17 * np.cos(np.radians(hm - 30)) + 0.24 * np.cos(np.radians(2 * hm))
         + 0.32 * np.cos(np.radians(3 * hm + 6)) - 0.20 * np.cos(np.radians(4 * hm - 63)))
    dth = 30 * np.exp(-(((hm - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cmp ** 7 / (Cmp ** 7 + 25.0 ** 7))
    Sl = 1 + 0.015 * (Lm - 50) ** 2 / np.sqrt(20 + (Lm - 50) ** 2)
    Sc = 1 + 0.045 * Cmp
    Sh = 1 + 0.015 * Cmp * T
    Rt = -np.sin(np.radians(2 * dth)) * Rc
    return np.sqrt((dL / Sl) ** 2 + (dC / Sc) ** 2 + (dH / Sh) ** 2 + Rt * (dC / Sc) * (dH / Sh))


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse <= 0 else 10 * np.log10(1.0 / mse)


def main() -> int:
    S = sys.argv[1]
    host = json.load(open(os.path.join(S, "host.json")))
    fmt = ((host.get("peer_stream") or {}).get("format") or {})
    working = fmt.get("working") or {}
    matrix = working.get("to_bt2020") or [[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]]
    engine = (host.get("cm_host") or {}).get("render", {}).get("engine", "CYCLES")
    checks, numbers = {}, {"working_space": working.get("name"), "engine": engine,
                           "display": host.get("display"), "view": host.get("view")}

    # 1. the wire vs the scene-linear reference
    ref_lin = png_rgb(os.path.join(S, "ref_raw16.png"))
    wire_path = os.path.join(S, "wire.yuv")
    if not os.path.exists(wire_path) or os.path.getsize(wire_path) < W * H * 3:
        checks["wire_matches_linear_render"] = False
        numbers["wire"] = "no frame grabbed (see wiregrab.log)"
        wire = None
    else:
        wire = decode_wire(wire_path, matrix)
    # dE2000 in display terms: both through the same sRGB encode (clipped), so
    # a linear error reads like it would on screen.
    enc = lambda x: np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.clip(x, 0, None) ** (1 / 2.4) - 0.055)
    if wire is not None:
        de_wire = de2000(srgb_to_lab(enc(np.clip(ref_lin, 0, 1))), srgb_to_lab(enc(np.clip(wire, 0, 1))))
        numbers["wire_psnr_db"] = round(psnr(np.clip(ref_lin, 0, 1), np.clip(wire, 0, 1)), 2)
        numbers["wire_de_mean"] = round(float(de_wire.mean()), 3)
        numbers["wire_de_p99"] = round(float(np.percentile(de_wire, 99)), 3)
        # Gates: the reference is a FINAL render, the wire a converged
        # viewport render of the same scene — two samplers and two
        # denoisers, so the pixel-wise PSNR bottoms out near 34 dB on edges
        # (measured 2026-10-27: 33.9 dB at dE mean 0.43). The colour gate is
        # the one that matters; the PSNR gate catches a wrong matrix (a
        # transposed one scores < 25 dB), not sampling.
        de_gate = 1.5 if engine != "CYCLES" else 1.0
        checks["wire_matches_linear_render"] = (numbers["wire_psnr_db"] > 30 and numbers["wire_de_mean"] < de_gate
                                                and numbers["wire_de_p99"] < 6.0)

    # 2. QCView's capture vs the host's picture as shown
    cap = os.path.join(S, "qcv_capture.png")
    if os.path.exists(cap):
        qcv = png_rgb(cap)
        shown = png_rgb(os.path.join(S, "ref_shown.png"))
        if qcv.shape == shown.shape:
            de = de2000(srgb_to_lab(shown), srgb_to_lab(qcv))
            numbers["qcview_de_mean"] = round(float(de.mean()), 3)
            numbers["qcview_de_p99"] = round(float(np.percentile(de, 99)), 3)
            numbers["qcview_psnr_db"] = round(psnr(shown, qcv), 2)
            checks["qcview_matches_host_picture"] = numbers["qcview_de_mean"] < 1.5 and numbers["qcview_de_p99"] < 4.0
            # for the record: how far the host's picture sits from AgX-on-sRGB
            srgb = png_rgb(os.path.join(S, "ref_srgb.png"))
            numbers["host_vs_srgb_view_de_mean"] = round(float(de2000(srgb_to_lab(shown), srgb_to_lab(srgb)).mean()), 3)
        else:
            checks["qcview_matches_host_picture"] = False
            numbers["qcview_capture_shape"] = list(qcv.shape)
    else:
        checks["qcview_matches_host_picture"] = False
        numbers["qcview_capture"] = "missing"

    # 3. the replica's own window, if the runner grabbed it (reported only)
    rep = os.path.join(S, "replica_frame.png")
    if os.path.exists(rep):
        try:
            r = png_rgb(rep)
            if r.shape == ref_lin.shape:
                shown = png_rgb(os.path.join(S, "ref_shown.png"))
                numbers["replica_window_de_mean"] = round(float(de2000(srgb_to_lab(shown), srgb_to_lab(r)).mean()), 3)
        except Exception as exc:  # noqa: BLE001
            numbers["replica_window"] = "unreadable: %s" % exc

    checks = {k: bool(v) for k, v in checks.items()}     # numpy bools are not JSON
    verdict = {"pass": all(checks.values()), "checks": checks, "numbers": numbers}
    json.dump(verdict, open(os.path.join(S, "harness.json"), "w"), indent=1)
    print(json.dumps(verdict, indent=1))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
