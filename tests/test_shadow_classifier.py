"""ShadowStore diff semantics + Debouncer + tier classification."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.classifier import Debouncer, classify_update  # noqa: E402
from ring1.dirtyset import Tier  # noqa: E402
from ring1.shadow import ShadowStore, TRACKED  # noqa: E402


def test_first_contact_returns_none_then_diffs():
    s = ShadowStore()
    snap = {"location": [0, 0, 0], "energy": 10.0}
    assert s.diff_and_update("u1", snap) is None
    assert s.diff_and_update("u1", snap) == {"changes": [], "structural": False}
    diff = s.diff_and_update("u1", {"location": [1, 0, 0], "energy": 10.0})
    assert diff == {"changes": [("location", [1, 0, 0])], "structural": False}


def test_path_set_change_is_structural():
    # node added/removed: the path set itself changes → tier-2 escalation
    s = ShadowStore()
    s.diff_and_update("u1", {"a": 1})
    diff = s.diff_and_update("u1", {"a": 1, "b": 2})
    assert diff["structural"] and diff["changes"] == [("b", 2)]
    diff = s.diff_and_update("u1", {"a": 1})
    assert diff["structural"] and diff["changes"] == []


def test_pseudo_path_change_is_structural_and_never_sent():
    # link rewiring / modifier stack ride "~" pseudo-paths
    s = ShadowStore()
    s.diff_and_update("u1", {"x": 1, "~link.a": "n1.out"})
    diff = s.diff_and_update("u1", {"x": 1, "~link.a": "n2.out"})
    assert diff["structural"]
    assert diff["changes"] == []  # pseudo-paths are not property writes


def test_forget():
    s = ShadowStore()
    s.diff_and_update("u1", {"a": 1})
    s.forget("u1")
    assert s.diff_and_update("u1", {"a": 2}) is None  # first contact again


def test_debouncer_coalesces_and_releases():
    d = Debouncer(window=0.15)
    d.touch("u1", 0.0)
    d.touch("u1", 0.1)  # still being dragged
    assert d.ready(0.2) == []  # 0.1 + 0.15 > 0.2
    assert d.ready(0.26) == ["u1"]
    assert d.ready(0.3) == []  # released once


def test_classify():
    # geometry flag is ignored for recognized types (measured unreliable:
    # fires on light-energy and scene-exposure edits) — structure detection
    # happens at the snapshot diff instead
    assert classify_update("OBJECT", is_geometry=False) == Tier.T1
    assert classify_update("OBJECT", is_geometry=True) == Tier.T1
    assert classify_update(None, is_geometry=False) == Tier.T2


def test_tracked_tables_cover_the_decided_set():
    # decision #12 + open-question-2 resolutions: CM settings, render format,
    # viewport denoise must be tier-1 tracked on the scene
    scene = TRACKED["SCENE"]
    assert "view_settings.view_transform" in scene
    assert "render.resolution_x" in scene
    assert "cycles.use_preview_denoising" in scene
