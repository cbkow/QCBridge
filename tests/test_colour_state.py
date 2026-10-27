"""The colour-state block: matrices, digest, mismatch rules, link params,
panel rows (ring1/colour_state.py — pure, no bpy)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1 import colour_state as cs  # noqa: E402
from ring1 import wire_format as wf  # noqa: E402


def _cm(role="HOST", **over):
    cm = cs.empty_cm(role, "5.2.2 LTS")
    cm["config"] = {"kind": "blender", "name": "", "file": "config.ocio", "cache_id": "523755818c3b2ef6", "bundled": True}
    cm["ocio"] = {"available": True, "version": "2.5.0", "error": ""}
    cm["working"] = {"name": "Linear Rec.709", "to_xyz": cs.matrix_rows(cs.M_709_TO_XYZ), "linear": True, "assumed": False}
    cm["display"] = {"device": "sRGB", "emulation": "AUTO", "colorspace": "sRGB", "encoding": "sdr-video",
                     "in_config": True, "xy": {"r": [0.64, 0.33], "g": [0.3, 0.6], "b": [0.15, 0.06], "w": [0.3127, 0.329]},
                     "gamma_est": 2.224}
    cm["view"] = {"transform": "AgX", "look": "None", "exposure": 0.0, "gamma": 1.0,
                  "in_config": True, "look_in_config": True, "is_hdr": False, "use_hdr_view": None}
    cm["render"] = {"engine": "CYCLES", "mapped_engine": "QCB_CYCLES" if role == "REPLICA" else None,
                    "cycles": {"device": "GPU", "preview_samples": 16, "use_preview_denoising": False,
                               "preview_denoiser": "OPENIMAGEDENOISE", "samples": 128, "film_exposure": 1.0},
                    "eevee": {"taa_samples": 16, "use_shadows": True, "use_raytracing": False}}
    cm["views_available"] = ["Standard", "AgX", "Filmic", "False Color", "Raw"]
    cm["looks_available"] = ["AgX - Punchy", "Very High Contrast"]
    cm["display_devices"] = ["sRGB", "Display P3", "Rec.1886", "Rec.2020"]
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cm.get(k), dict):
            cm[k].update(v)
        else:
            cm[k] = v
    cm["digest"] = cs.digest(cm)
    return cm


def test_working_to_2020_matches_the_wire_constant():
    m = cs.working_to_2020(cs.M_709_TO_XYZ)
    assert cs.matrix_close(m, wf.M_709_TO_2020, 1e-3)
    # columns (images of R, G, B) → rows round-trip
    cols = [[cs.M_709_TO_XYZ[i][j] for i in range(3)] for j in range(3)]
    assert cs.mat_from_columns(cols) == cs.M_709_TO_XYZ


def test_matrix_close_tolerates_junk():
    assert not cs.matrix_close(None, cs.M_709_TO_XYZ)
    assert not cs.matrix_close([[1, 2], [3]], cs.M_709_TO_XYZ)
    assert cs.matrix_close(cs.matrix_rows(cs.M_709_TO_XYZ), cs.M_709_TO_XYZ)


def test_digest_ignores_the_list_fields_but_not_the_facts():
    a = _cm()
    b = _cm(views_available=["Only"], looks_available=[])
    assert a["digest"] == b["digest"]
    c = _cm(view={"transform": "Standard"})
    assert c["digest"] != a["digest"]


def test_fallback_block_has_every_key_and_says_assumed():
    fb = cs.fallback_cm("REPLICA", "4.5.0", {"view": {"transform": "Standard"}}, error="no module")
    for key in cs.empty_cm():
        assert key in fb
    assert fb["working"]["assumed"] is True
    assert fb["working"]["name"] == "Linear Rec.709"
    assert cs.matrix_close(fb["working"]["to_xyz"], cs.M_709_TO_XYZ)
    assert fb["view"]["transform"] == "Standard"
    assert fb["ocio"]["available"] is False and fb["ocio"]["error"] == "no module"
    assert fb["digest"]


def test_config_kind():
    assert cs.config_kind(["Linear Rec.709", "sRGB"], True, ["Standard", "AgX"]) == "blender"
    assert cs.config_kind(["ACEScg", "Linear Rec.709 (sRGB)"], True, ["ACES 2.0 - SDR 100 nits"]) == "other"
    assert cs.config_kind(["Linear Rec.709"], True, ["Standard"]) == "other"


def test_no_mismatch_when_both_ends_agree():
    assert cs.mismatches(_cm("HOST"), _cm("REPLICA")) == []


def test_missing_replica_block_is_one_warning():
    rows = cs.mismatches(_cm("HOST"), None)
    assert len(rows) == 1 and rows[0][0] == "warn" and rows[0][1] == "peer_cm"
    assert cs.mismatches(None, _cm("REPLICA")) == []


def test_config_and_working_space_mismatches_are_errors():
    r = _cm("REPLICA", config={"cache_id": "3a34339323cc019a", "file": "config.ocio"})
    rows = cs.mismatches(_cm("HOST"), r)
    assert [k for lv, k, _ in rows if lv == "error"] == ["config"]
    assert "OCIO configs differ" in rows[0][2]
    r2 = _cm("REPLICA", working={"name": "ACEScg", "to_xyz": [[0.66, 0.13, 0.16], [0.27, 0.67, 0.06], [-0.0055, 0.004, 1.09]]})
    rows = cs.mismatches(_cm("HOST"), r2)
    assert any(k == "working" and lv == "error" for lv, k, _ in rows)
    assert cs.worst_level(rows) == "error"


def test_display_view_and_look_availability():
    h = _cm("HOST", display={"device": "Rec.2020"}, view={"transform": "Filmic", "look": "AgX - Punchy"})
    r = _cm("REPLICA", display_devices=["sRGB", "Display P3"], views_available=["Standard", "AgX"], looks_available=["Very High Contrast"])
    rows = {k: lv for lv, k, _ in cs.mismatches(h, r)}
    assert rows["display"] == "error"
    assert rows["view"] == "error"
    assert rows["look"] == "warn"


def test_wb_curves_hdr_engine_and_ocio_rows():
    h = _cm("HOST", wb={"on": True}, curves={"on": True}, view={"is_hdr": True}, ocio={"available": False})
    r = _cm("REPLICA", render={"mapped_engine": "BLENDER_EEVEE"})
    rows = {k: lv for lv, k, _ in cs.mismatches(h, r)}
    assert rows["wb"] == "warn" and rows["curves"] == "warn" and rows["hdr"] == "warn"
    assert rows["engine"] == "warn"
    assert rows["ocio_host"] == "warn"
    assert cs.worst_level(cs.mismatches(h, r)) == "warn"


def test_cycles_differences_are_info():
    r = _cm("REPLICA")
    r["render"]["cycles"]["preview_samples"] = 32
    r["render"]["cycles"]["use_preview_denoising"] = True
    rows = cs.mismatches(_cm("HOST"), r)
    assert rows and rows[-1][0] == "info" and "32 spp" in rows[-1][2] and "denoise" in rows[-1][2]


def test_deep_link_params_tell_the_truth():
    h = _cm("HOST", display={"device": "Rec.2020", "emulation": "AUTO"}, view={"transform": "Standard", "look": "None"})
    r = _cm("REPLICA")
    q = cs.deep_link_params(h, r, {"transfer": "pq", "primaries": "bt2020", "anchor_nits": 100})
    assert q["wire"] == "pq-bt2020-100"
    assert q["input"] == "Linear Rec.709" and q["ws"] == "Linear Rec.709" and q["ws_assumed"] == "0"
    assert q["config"] == "blender::523755818c3b2ef6" and q["config_bundled"] == "1"
    assert q["display"] == "Rec.2020" and q["emulation"] == "auto"
    assert q["view"] == "Standard" and q["look"] == "None"
    assert q["display_xy"].count(",") == 7 and q["display_gamma"] == "2.22"
    assert q["wb"] == "0" and q["curves"] == "0" and q["hdr"] == "0"
    assert q["engine"] == "CYCLES"
    assert q["cm_ok"] == "1" and q["cm_n"] == "0"


def test_deep_link_flags_a_config_mismatch_and_assumed_working_space():
    h = _cm("HOST")
    r = cs.fallback_cm("REPLICA", "4.5.0", {}, error="x")
    q = cs.deep_link_params(h, r, {})
    assert q["cm_ok"] == "0"            # configs differ (fallback has no cache id)
    assert q["ws_assumed"] == "1"
    assert int(q["cm_n"]) >= 2


def test_summary_rows_read_as_words():
    rows = dict(cs.summary_rows(_cm("HOST", display={"device": "Rec.2020"}, view={"transform": "Standard"})))
    assert rows["View"] == "Standard"
    assert rows["Display"].startswith("Rec.2020")
    assert rows["Working space"] == "Linear Rec.709"
    assert "16 spp" in rows["Samples"]
    assert rows["Config"].endswith("(bundled)")
    fb = dict(cs.summary_rows(cs.fallback_cm("HOST", "4.5.0", {})))
    assert fb["Working space"] == "Linear Rec.709 (assumed)"
    assert dict(cs.summary_rows(None))["View"] == "—"


def test_self_notes_flag_a_non_srgb_display_device():
    assert cs.self_notes(_cm("HOST")) == []
    rows = cs.self_notes(_cm("HOST", display={"device": "Rec.2020"}))
    assert rows and rows[0][0] == "warn" and "raw codes" in rows[0][2]
    rows = cs.self_notes(_cm("HOST", view={"transform": "False Color"}))
    assert rows and rows[0][0] == "info"
    assert cs.self_notes(None) == []
