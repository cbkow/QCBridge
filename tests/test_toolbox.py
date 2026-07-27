"""ffmpeg resolution chain: pref → toolbox.json → PATH → honest failure."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.toolbox import read_toolbox, resolve_ffmpeg  # noqa: E402


def _fake_binary(tmp_path, name):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    return str(p)


def test_explicit_pref_wins(tmp_path):
    binary = _fake_binary(tmp_path, "custom-ffmpeg")
    path, source = resolve_ffmpeg(binary, toolbox_file="/nope", which=lambda n: None, spawnable=lambda p: True)
    assert (path, source) == (binary, "preferences")


def test_explicit_pref_missing_is_an_error_not_a_fallthrough(tmp_path):
    binary = _fake_binary(tmp_path, "real-ffmpeg")
    path, source = resolve_ffmpeg(
        "/gone/ffmpeg", toolbox_file="/nope", which=lambda n: binary, spawnable=lambda p: True
    )
    assert path is None and "missing" in source


def test_qcview_toolbox_beats_path(tmp_path):
    qcview = _fake_binary(tmp_path, "qcview-ffmpeg")
    system = _fake_binary(tmp_path, "system-ffmpeg")
    manifest = tmp_path / "toolbox.json"
    manifest.write_text(json.dumps({"ffmpeg": qcview}))
    path, source = resolve_ffmpeg(
        "", toolbox_file=str(manifest), which=lambda n: system, spawnable=lambda p: True
    )
    assert (path, source) == (qcview, "qcview")


def test_stale_toolbox_falls_through_to_path(tmp_path):
    system = _fake_binary(tmp_path, "system-ffmpeg")
    manifest = tmp_path / "toolbox.json"
    manifest.write_text(json.dumps({"ffmpeg": "/uninstalled/ffmpeg"}))
    path, source = resolve_ffmpeg(
        "", toolbox_file=str(manifest), which=lambda n: system, spawnable=lambda p: True
    )
    assert (path, source) == (system, "path")


def test_old_bare_default_treated_as_unset(tmp_path):
    system = _fake_binary(tmp_path, "system-ffmpeg")
    path, source = resolve_ffmpeg("ffmpeg", toolbox_file="/nope", which=lambda n: system, spawnable=lambda p: True)
    assert (path, source) == (system, "path")


def test_nothing_found_is_honest():
    path, source = resolve_ffmpeg("", toolbox_file="/nope", which=lambda n: None, spawnable=lambda p: True)
    assert path is None and "not found" in source


def test_read_toolbox_tolerates_garbage(tmp_path):
    bad = tmp_path / "toolbox.json"
    bad.write_text("{not json")
    assert read_toolbox(str(bad)) == {}
    assert read_toolbox(str(tmp_path / "absent.json")) == {}


def test_msix_unspawnable_qcview_falls_through_to_path(tmp_path):
    # Windows: QCView's toolbox points into WindowsApps, where MSIX ACLs
    # deny direct Popen (WinError 5) — existing-but-unspawnable must skip.
    qcview = _fake_binary(tmp_path, "msix-ffmpeg")
    system = _fake_binary(tmp_path, "system-ffmpeg")
    manifest = tmp_path / "toolbox.json"
    manifest.write_text(json.dumps({"ffmpeg": qcview}))
    path, source = resolve_ffmpeg(
        "", toolbox_file=str(manifest), which=lambda n: system,
        spawnable=lambda p: p != qcview,
    )
    assert (path, source) == (system, "path")


def test_nothing_spawnable_is_honest(tmp_path):
    qcview = _fake_binary(tmp_path, "msix-ffmpeg")
    manifest = tmp_path / "toolbox.json"
    manifest.write_text(json.dumps({"ffmpeg": qcview}))
    path, source = resolve_ffmpeg(
        "", toolbox_file=str(manifest), which=lambda n: None,
        spawnable=lambda p: False,
    )
    assert path is None and "not spawnable" in source
