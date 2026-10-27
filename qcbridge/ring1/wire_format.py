"""The wire format, as one pure-Python reference: scene-linear (the OCIO
config's working space — Linear Rec.709 in Blender's own config, but a
parameter here since 2026-10-27, never an assumption) → BT.2020 by the
working space's matrix → PQ (scene 1.0 = ANCHOR_NITS) → BT.2020 non-constant-luminance
Y'CbCr, 10-bit limited range, 4:2:0 by 2×2 average (DESIGN-NOTES pixels
wire). The helpers' kernels (Metal on macOS, HLSL/SIMD on Windows) mirror
this; `tests/test_wire_format.py` pins the numbers and writes the vectors
their `--selftest` must reproduce. QCView undoes it with the same
constants (PQ EOTF → /ANCHOR_NITS → 2020→709).

Chosen on the 2026-10-26/27 overnight spike: at equal bitrate on an AgX
scene PQ in a BT.2020 container scored 43.2 dB / dE 0.77 against 38.9 for
the same curve in a 709 container, matched a 10-bit display-referred stream
at 60 % of its bits and beat the 8-bit one at a third.
"""
from __future__ import annotations

ANCHOR_NITS = 100.0        # scene-linear 1.0 on the wire
PQ_PEAK_NITS = 10000.0

# ST 2084 constants.
M1 = 0.1593017578125
M2 = 78.84375
C1 = 0.8359375
C2 = 18.8515625
C3 = 18.6875

# Linear Rec.709 (D65) → linear BT.2020 (D65): the matrix for Blender's own
# working space, and the default everywhere a matrix is a parameter. The
# replica computes the real one from its OCIO config
# (ring1/colour_state.working_to_2020) and hands it to the helper as
# `--matrix`; QCView never sees it — the wire is BT.2020 either way.
M_709_TO_2020 = (
    (0.6274, 0.3293, 0.0433),
    (0.0691, 0.9195, 0.0114),
    (0.0164, 0.0880, 0.8956),
)
# Inverse, for the receiver and the round-trip test.
M_2020_TO_709 = (
    (1.6605, -0.5876, -0.0728),
    (-0.1246, 1.1329, -0.0083),
    (-0.0182, -0.1006, 1.1187),
)

# BT.2020 NCL luma weights and chroma scales.
KR, KG, KB = 0.2627, 0.6780, 0.0593
CB_DIV = 2.0 * (1.0 - KB)   # 1.8814
CR_DIV = 2.0 * (1.0 - KR)   # 1.4746

# 10-bit limited range.
Y_MIN, Y_MAX = 64, 940
C_MIN, C_MAX = 64, 960
C_MID = 512


def rgb_to_2020(r: float, g: float, b: float, matrix=M_709_TO_2020) -> tuple[float, float, float]:
    """Linear working space → linear BT.2020 by `matrix` (rows), clamped ≥ 0."""
    out = []
    for row in matrix:
        out.append(max(0.0, row[0] * r + row[1] * g + row[2] * b))
    return out[0], out[1], out[2]


def rgb709_to_2020(r: float, g: float, b: float) -> tuple[float, float, float]:
    return rgb_to_2020(r, g, b, M_709_TO_2020)


def matrix_arg(matrix) -> str:
    """The helper's `--matrix` value: nine row-major floats, comma-separated."""
    return ",".join("%.7g" % float(v) for row in matrix for v in row)


def parse_matrix_arg(text: str):
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 9:
        raise ValueError("matrix needs nine values, got %d" % len(vals))
    return tuple(tuple(vals[i * 3:i * 3 + 3]) for i in range(3))


def rgb2020_to_709(r: float, g: float, b: float) -> tuple[float, float, float]:
    out = []
    for row in M_2020_TO_709:
        out.append(row[0] * r + row[1] * g + row[2] * b)
    return out[0], out[1], out[2]


def pq_oetf(linear: float) -> float:
    """Scene-linear (1.0 = ANCHOR_NITS) → PQ signal 0..1."""
    L = max(0.0, min(1.0, linear * ANCHOR_NITS / PQ_PEAK_NITS))
    Lm = L ** M1
    return ((C1 + C2 * Lm) / (1.0 + C3 * Lm)) ** M2


def pq_eotf(signal: float) -> float:
    """PQ signal 0..1 → scene-linear (1.0 = ANCHOR_NITS)."""
    E = max(0.0, min(1.0, signal))
    Em = E ** (1.0 / M2)
    L = (max(0.0, Em - C1) / (C2 - C3 * Em)) ** (1.0 / M1)
    return L * PQ_PEAK_NITS / ANCHOR_NITS


def ycbcr(rp: float, gp: float, bp: float) -> tuple[float, float, float]:
    """Non-constant-luminance Y'CbCr from PQ-encoded R'G'B' (Y' 0..1, C ±0.5)."""
    y = KR * rp + KG * gp + KB * bp
    return y, (bp - y) / CB_DIV, (rp - y) / CR_DIV


def ycbcr_inverse(y: float, cb: float, cr: float) -> tuple[float, float, float]:
    rp = y + CR_DIV * cr
    bp = y + CB_DIV * cb
    gp = (y - KR * rp - KB * bp) / KG
    return rp, gp, bp


def quantize(y: float, cb: float, cr: float) -> tuple[int, int, int]:
    dy = int(round(876.0 * y + 64.0))
    dcb = int(round(896.0 * cb + 512.0))
    dcr = int(round(896.0 * cr + 512.0))
    return (max(Y_MIN, min(Y_MAX, dy)), max(C_MIN, min(C_MAX, dcb)), max(C_MIN, min(C_MAX, dcr)))


def dequantize(dy: int, dcb: int, dcr: int) -> tuple[float, float, float]:
    return (dy - 64.0) / 876.0, (dcb - 512.0) / 896.0, (dcr - 512.0) / 896.0


def encode_pixel(r: float, g: float, b: float, matrix=M_709_TO_2020) -> tuple[float, float, float]:
    """Scene-linear (working space, by `matrix`) → (Y', Cb, Cr) floats, before quantization."""
    r2, g2, b2 = rgb_to_2020(r, g, b, matrix)
    return ycbcr(pq_oetf(r2), pq_oetf(g2), pq_oetf(b2))


def decode_pixel(y: float, cb: float, cr: float) -> tuple[float, float, float]:
    rp, gp, bp = ycbcr_inverse(y, cb, cr)
    return rgb2020_to_709(pq_eotf(rp), pq_eotf(gp), pq_eotf(bp))


def encode_tile(rgb: list[list[tuple[float, float, float]]], matrix=M_709_TO_2020) -> dict:
    """A small frame (rows of (r,g,b) scene-linear) → 10-bit planes as the
    helper must produce them: `y` (h×w codes), `cb`/`cr` (h/2×w/2 codes, the
    2×2 average of per-pixel chroma — centre siting). Codes are 0..1023; the
    helper stores each as code << 6 in its 16-bit plane words."""
    h = len(rgb)
    w = len(rgb[0]) if h else 0
    assert h % 2 == 0 and w % 2 == 0, "4:2:0 needs even dimensions"
    yc = [[0] * w for _ in range(h)]
    cbf = [[0.0] * w for _ in range(h)]
    crf = [[0.0] * w for _ in range(h)]
    for j in range(h):
        for i in range(w):
            y, cb, cr = encode_pixel(*rgb[j][i], matrix=matrix)
            yc[j][i] = quantize(y, 0.0, 0.0)[0]
            cbf[j][i], crf[j][i] = cb, cr
    cbc = [[0] * (w // 2) for _ in range(h // 2)]
    crc = [[0] * (w // 2) for _ in range(h // 2)]
    for j in range(0, h, 2):
        for i in range(0, w, 2):
            cb = (cbf[j][i] + cbf[j][i + 1] + cbf[j + 1][i] + cbf[j + 1][i + 1]) / 4.0
            cr = (crf[j][i] + crf[j][i + 1] + crf[j + 1][i] + crf[j + 1][i + 1]) / 4.0
            _, dcb, dcr = quantize(0.0, cb, cr)
            cbc[j // 2][i // 2], crc[j // 2][i // 2] = dcb, dcr
    return {"w": w, "h": h, "y": yc, "cb": cbc, "cr": crc}


def plane_word(code: int) -> int:
    """10-bit code → the 16-bit word both x420 (macOS) and P010 (Windows) carry."""
    return (code & 0x3FF) << 6
