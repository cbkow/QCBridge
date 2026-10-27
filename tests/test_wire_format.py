"""Pins the wire-format maths and writes the vectors the helpers' --selftest
must reproduce (tests/fixtures/pq_vectors.json)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1 import wire_format as wf  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pq_vectors.json"
FIXTURE_P3 = pathlib.Path(__file__).resolve().parent / "fixtures" / "pq_vectors_p3.json"
# Linear DCI-P3 D65 → CIE XYZ D65: the second fixture's working space, far
# enough from Rec.709 that a helper ignoring --matrix, or transposing it,
# fails (2026-10-27 colour awareness).
M_P3_TO_XYZ = ((0.4865709, 0.2656677, 0.1982173), (0.2289746, 0.6917385, 0.0792869), (0.0, 0.0451134, 1.0439444))


def test_pq_anchor_and_endpoints():
    assert abs(wf.pq_oetf(1.0) - 0.5081) < 5e-4          # 100 nit
    assert wf.pq_oetf(0.0) < 1e-6                        # c1**m2, the curve's true zero
    assert abs(wf.pq_oetf(wf.PQ_PEAK_NITS / wf.ANCHOR_NITS) - 1.0) < 1e-9
    assert wf.pq_oetf(1e6) == 1.0                         # clamps at the peak
    assert wf.pq_oetf(-5.0) == wf.pq_oetf(0.0)            # negatives clip to black
    for lin in (0.0, 0.001, 0.18, 1.0, 10.0, 99.0):
        assert abs(wf.pq_eotf(wf.pq_oetf(lin)) - lin) < 1e-6 * max(1.0, lin)


def test_matrix_rows_sum_to_one_and_round_trip():
    for row in wf.M_709_TO_2020:
        assert abs(sum(row) - 1.0) < 1e-3
    assert all(abs(c - 1.0) < 1e-3 for c in wf.rgb709_to_2020(1.0, 1.0, 1.0))
    for rgb in ((1.0, 0.0, 0.0), (0.2, 0.7, 0.1), (0.0, 0.0, 1.0)):
        back = wf.rgb2020_to_709(*wf.rgb709_to_2020(*rgb))
        assert all(abs(a - b) < 2e-3 for a, b in zip(rgb, back))


def test_white_and_black_codes():
    assert wf.quantize(*wf.encode_pixel(0.0, 0.0, 0.0)) == (64, 512, 512)
    y, cb, cr = wf.encode_pixel(1.0, 1.0, 1.0)
    assert abs(cb) < 1e-6 and abs(cr) < 1e-6                 # grey has no chroma
    dy, dcb, dcr = wf.quantize(y, cb, cr)
    assert (dcb, dcr) == (512, 512) and dy == round(876 * 0.5081 + 64)
    assert wf.quantize(*wf.encode_pixel(1e6, 1e6, 1e6)) == (940, 512, 512)


def test_pixel_round_trip_through_quantization():
    # 10-bit PQ steps are ~0.5–1 % of luminance above 100 nit and the NCL
    # chroma quantization adds to that on saturated colours: 3 % is the
    # honest bound for 4:4:4 at the pixel; the overnight spike measured
    # 53–59 dB for the full frame before any codec.
    for rgb in ((0.18, 0.18, 0.18), (1.0, 0.2, 0.05), (0.02, 0.4, 0.9), (5.0, 3.0, 1.0)):
        q = wf.quantize(*wf.encode_pixel(*rgb))
        back = wf.decode_pixel(*wf.dequantize(*q))
        for a, b in zip(rgb, back):
            assert abs(a - b) <= 0.03 * max(1.0, a) + 2e-3


def test_tile_chroma_is_the_2x2_average_and_fixture_is_current():
    tile = [[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)], [(0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]]
    out = wf.encode_tile(tile)
    assert out["w"] == 2 and out["h"] == 2 and len(out["cb"]) == 1 and len(out["cb"][0]) == 1
    cbs, crs = [], []
    for row in tile:
        for px in row:
            _y, cb, cr = wf.encode_pixel(*px)
            cbs.append(cb); crs.append(cr)
    _, dcb, dcr = wf.quantize(0.0, sum(cbs) / 4, sum(crs) / 4)
    assert out["cb"][0][0] == dcb and out["cr"][0][0] == dcr
    assert wf.plane_word(1023) == 0xFFC0 and wf.plane_word(64) == 64 << 6
    # the 16×16 selftest vectors: a ramp with saturated corners, encoded
    # from Linear Rec.709 (Blender's own working space) and from Linear
    # DCI-P3 D65 — each fixture names its working space and carries its
    # working → BT.2020 matrix, which the helpers' --selftest must apply.
    from ring1 import colour_state as cs
    vec = [[(((i + j) % 16) / 15.0 * 2.0, (i / 15.0) ** 2, (j / 15.0) * 0.5) for i in range(16)] for j in range(16)]
    vec[0][0] = (1e6, 0.0, 0.0); vec[15][15] = (0.0, 0.0, 1e6)
    m_p3 = cs.working_to_2020(M_P3_TO_XYZ)
    for path, name, matrix in ((FIXTURE, "Linear Rec.709", wf.M_709_TO_2020), (FIXTURE_P3, "Linear DCI-P3 D65", m_p3)):
        expect = {"anchor_nits": wf.ANCHOR_NITS, "working_space": name, "matrix": cs.matrix_rows(matrix),
                  "input": vec, "planes": wf.encode_tile(vec, matrix)}
        if path.exists():
            assert json.loads(path.read_text()) == json.loads(json.dumps(expect)), (
                "wire_format changed: delete the fixture to regenerate and rebuild the helpers' selftest"
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(expect))
    # a transposed matrix must not pass: the fixtures are sensitive to it
    mt = tuple(tuple(m_p3[j][i] for j in range(3)) for i in range(3))
    assert wf.encode_tile(vec, mt)["y"] != wf.encode_tile(vec, m_p3)["y"]


def test_matrix_is_a_parameter_and_the_arg_round_trips():
    assert wf.rgb_to_2020(1.0, 0.0, 0.0) == wf.rgb709_to_2020(1.0, 0.0, 0.0)
    ident = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    assert wf.rgb_to_2020(0.2, 0.5, 0.9, ident) == (0.2, 0.5, 0.9)
    assert wf.encode_pixel(1.0, 0.0, 0.0, ident) != wf.encode_pixel(1.0, 0.0, 0.0)
    text = wf.matrix_arg(wf.M_709_TO_2020)
    assert text.count(",") == 8
    back = wf.parse_matrix_arg(text)
    assert all(abs(back[i][j] - wf.M_709_TO_2020[i][j]) < 1e-6 for i in range(3) for j in range(3))
    try:
        wf.parse_matrix_arg("1,2,3")
    except ValueError:
        pass
    else:
        raise AssertionError("short matrix accepted")
