"""Port of ufb's translate_path_to test suite (ufb/core/src/utils.rs tests),
plus the two upgrades folded in per decision #15: component-boundary matching
and longest-prefix-first ordering."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.pathmap import (  # noqa: E402
    PathMapping,
    from_canonical,
    is_mapped,
    to_canonical,
    translate,
)


def mapping(win, mac, enabled=True):
    return PathMapping(win=win, mac=mac, enabled=enabled)


NAS = [mapping("C:\\Volumes\\studio-nas\\jobs\\", "/Volumes/studio-nas/jobs")]


# ── ported from ufb ──────────────────────────────────────────────────────────

def test_no_mapping_converts_separators_only():
    assert translate("mac", "win", "/Volumes/X/file", []) == "\\Volumes\\X\\file"


def test_mac_to_win_and_back():
    out = translate("mac", "win", "/Volumes/studio-nas/jobs/250099_x", NAS)
    assert out == "C:\\Volumes\\studio-nas\\jobs\\250099_x"
    back = translate("win", "mac", out, NAS)
    assert back == "/Volumes/studio-nas/jobs/250099_x"


def test_bare_drive_mapping_does_not_capture_other_drives():
    maps = [
        mapping("U:\\", "/Volumes/Jobs_Live"),
        mapping("C:\\Volumes\\union-ny-gfx\\union-jobs\\", "/Volumes/union-ny-gfx/union-jobs"),
    ]
    out = translate("win", "win", "C:/Volumes/union-ny-gfx/union-jobs/261317_x/file.mov", maps)
    assert out == "C:\\Volumes\\union-ny-gfx\\union-jobs\\261317_x\\file.mov"
    out = translate("win", "mac", "U:\\261317_x\\file.mov", maps)
    assert out == "/Volumes/Jobs_Live/261317_x/file.mov"
    out = translate("mac", "win", "/Volumes/Jobs_Live/261317_x/file.mov", maps)
    assert out == "U:\\261317_x\\file.mov"


def test_drive_letterless_win_path_matches():
    out = translate("win", "mac", "Volumes\\studio-nas\\jobs\\250099_x", NAS)
    assert out == "/Volumes/studio-nas/jobs/250099_x"


def test_leading_slash_driveless_win_path_matches():
    out = translate("win", "mac", "\\Volumes\\studio-nas\\jobs\\", NAS)
    assert out == "/Volumes/studio-nas/jobs"


def test_win_to_win_repairs_driveless():
    out = translate("win", "win", "\\Volumes\\studio-nas\\jobs\\250101_demo", NAS)
    assert out == "C:\\Volumes\\studio-nas\\jobs\\250101_demo"


def test_win_to_win_repairs_forward_slash():
    out = translate("win", "win", "/Volumes/studio-nas/jobs/250101_demo", NAS)
    assert out == "C:\\Volumes\\studio-nas\\jobs\\250101_demo"


def test_win_to_win_idempotent_on_canonical():
    canon = "C:\\Volumes\\studio-nas\\jobs\\250101_demo"
    assert translate("win", "win", canon, NAS) == canon


def test_win_to_win_no_mapping_preserves_path():
    p = "C:\\Users\\alice\\Desktop\\notes.txt"
    assert translate("win", "win", p, NAS) == p


def test_mac_to_mac_idempotent():
    p = "/Volumes/studio-nas/jobs/250101_demo"
    assert translate("mac", "mac", p, NAS) == p


def test_win_source_case_insensitive_mac_source_sensitive():
    out = translate("win", "mac", "c:\\volumes\\STUDIO-NAS\\jobs\\X", NAS)
    assert out == "/Volumes/studio-nas/jobs/X"
    out = translate("mac", "win", "/volumes/STUDIO-nas/jobs/X", NAS)
    assert out == "\\volumes\\STUDIO-nas\\jobs\\X"  # no match: passthrough


def test_disabled_mapping_skipped():
    maps = [mapping("C:\\Volumes\\studio-nas\\jobs\\", "/Volumes/studio-nas/jobs", enabled=False)]
    out = translate("mac", "win", "/Volumes/studio-nas/jobs/X", maps)
    assert out == "\\Volumes\\studio-nas\\jobs\\X"


def test_unc_prefix():
    maps = [mapping("\\\\nas\\share", "/Volumes/share")]
    out = translate("mac", "win", "/Volumes/share/proj/file.exr", maps)
    assert out == "\\\\nas\\share\\proj\\file.exr"
    back = translate("win", "mac", out, maps)
    assert back == "/Volumes/share/proj/file.exr"


@pytest.mark.skipif(sys.platform != "darwin", reason="expands against mac $HOME")
def test_tilde_expansion_on_current_os():
    import os

    maps = [mapping("R:\\Projects", "~/qcb/mounts/Projects")]
    home = os.path.expanduser("~")
    out = translate("win", "mac", "R:\\Projects\\Flame\\reel.mov", maps)
    assert out == f"{home}/qcb/mounts/Projects/Flame/reel.mov"
    out = translate("mac", "win", f"{home}/qcb/mounts/Projects/Flame/reel.mov", maps)
    assert out == "R:\\Projects\\Flame\\reel.mov"


# ── the two decision-#15 upgrades ────────────────────────────────────────────

def test_component_boundary_share_never_matches_share_dash_2():
    maps = [mapping("C:\\share", "/Volumes/share")]
    out = translate("mac", "win", "/Volumes/share-2/file", maps)
    assert out == "\\Volumes\\share-2\\file"  # passthrough, not C:\-2\file
    out = translate("mac", "win", "/Volumes/share/file", maps)
    assert out == "C:\\share\\file"


def test_longest_prefix_wins_regardless_of_row_order():
    maps = [
        mapping("Z:\\", "/Volumes/everything"),
        mapping("Z:\\deep\\project", "/Volumes/deep-project"),
    ]
    out = translate("win", "mac", "Z:\\deep\\project\\shot.exr", maps)
    assert out == "/Volumes/deep-project/shot.exr"
    out = translate("win", "mac", "Z:\\other\\shot.exr", maps)
    assert out == "/Volumes/everything/other/shot.exr"


# ── wire-form wrappers + honesty check ───────────────────────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="wrappers use current OS")
def test_canonical_round_trip_on_mac():
    p = "/Volumes/studio-nas/jobs/scene.blend"
    wire = to_canonical(p, NAS)
    assert wire == "C:\\Volumes\\studio-nas\\jobs\\scene.blend"
    assert from_canonical(wire, NAS) == p


def test_is_mapped():
    assert is_mapped("mac", "/Volumes/studio-nas/jobs/x", NAS)
    assert not is_mapped("mac", "/Users/chris/Desktop/x", NAS)
    assert is_mapped("win", "C:\\Volumes\\studio-nas\\jobs\\x", NAS)
    assert not is_mapped("win", "D:\\elsewhere\\x", NAS)
