"""The colour-state block ("cm"): what each end of the bridge knows about its
own colour management, as plain data, and what the two blocks say about each
other (DESIGN-NOTES pixels, colour state).

Why this exists (2026-10-27): the beauty window looked wrong and every stage
of the pixel maths measured clean. The trouble was assumptions — the working
space taken to be Linear Rec.709, the two ends taken to run the same OCIO
config, the host's Display Device ignored, AgX taken for granted. The block
replaces every one of those with a fact read from the config and the scene
on each machine, so the host panel can show both sides and say, in red,
where they disagree, the wire can be converted from the real working space,
and the link can tell QCView the truth.

This half is pure Python (no bpy, no OCIO): the maths, the digest, the
mismatch rules, the link parameters, the panel rows. `ring0/colour_state.py`
fills the block from bpy + PyOpenColorIO on the main thread.
"""
from __future__ import annotations

import hashlib
import json

CM_VERSION = 1

# CIE XYZ (D65) → linear BT.2020, and linear Rec.709 → CIE XYZ (D65).
M_XYZ_TO_2020 = (
    (1.7166512, -0.3556708, -0.2533663),
    (-0.6666844, 1.6164812, 0.0157685),
    (0.0176399, -0.0427706, 0.9421031),
)
M_709_TO_XYZ = (
    (0.4123908, 0.3575843, 0.1804808),
    (0.2126390, 0.7151687, 0.0721923),
    (0.0193308, 0.1191948, 0.9505322),
)

LEVELS = ("error", "warn", "info")

# Fields that never enter the digest: lists the panel reads but that do not
# change what the two ends agree on.
_DIGEST_SKIP = ("views_available", "looks_available", "digest")


# ---------------------------------------------------------------------------
# matrices
# ---------------------------------------------------------------------------

def mat_mul(a, b):
    """Row-major 3×3 product a · b."""
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def mat_from_columns(cols):
    """Three columns (the images of R, G, B) → row-major 3×3."""
    return tuple(tuple(cols[j][i] for j in range(3)) for i in range(3))


def working_to_2020(to_xyz):
    """Linear working space → linear BT.2020, from the working space's
    matrix to CIE XYZ D65 (what the encoder helper needs)."""
    return mat_mul(M_XYZ_TO_2020, to_xyz)


def matrix_close(a, b, tol: float = 1e-3) -> bool:
    try:
        return all(abs(float(a[i][j]) - float(b[i][j])) <= tol
                   for i in range(3) for j in range(3))
    except (TypeError, IndexError, ValueError):
        return False


def matrix_rows(m) -> list[list[float]]:
    return [[round(float(v), 7) for v in row] for row in m]


# ---------------------------------------------------------------------------
# the block
# ---------------------------------------------------------------------------

def empty_cm(role: str = "", blender: str = "") -> dict:
    """Every key present, nothing known yet."""
    return {
        "v": CM_VERSION, "blender": blender, "role": role,
        "config": {"kind": "unknown", "name": "", "file": "", "cache_id": "", "bundled": None},
        "ocio": {"available": False, "version": "", "error": ""},
        "working": {"name": "", "to_xyz": None, "linear": None, "assumed": True, "source": ""},
        "display": {"device": "", "emulation": None, "colorspace": "", "encoding": "",
                    "in_config": None, "xy": None, "gamma_est": None},
        "view": {"transform": "", "look": "", "exposure": 0.0, "gamma": 1.0,
                 "in_config": None, "look_in_config": None, "is_hdr": None, "use_hdr_view": None},
        "wb": {"on": False, "temperature": None, "tint": None},
        "curves": {"on": False},
        "render": {"engine": "", "mapped_engine": None, "cycles": None, "eevee": None},
        "views_available": [], "looks_available": [],
        "digest": "",
    }


def fallback_cm(role: str, blender: str, scene_fields: dict | None = None,
                error: str = "") -> dict:
    """No PyOpenColorIO: the scene's own fields, the working space ASSUMED
    to be Linear Rec.709 (said so), the Rec.709 matrix."""
    cm = empty_cm(role, blender)
    cm["ocio"] = {"available": False, "version": "", "error": error}
    cm["working"] = {"name": "Linear Rec.709", "to_xyz": matrix_rows(M_709_TO_XYZ),
                     "linear": True, "assumed": True, "source": "assumed"}
    for key, val in (scene_fields or {}).items():
        if key in cm and isinstance(val, dict) and isinstance(cm[key], dict):
            cm[key].update(val)
        elif key in cm:
            cm[key] = val
    cm["digest"] = digest(cm)
    return cm


def digest(cm: dict) -> str:
    body = {k: v for k, v in cm.items() if k not in _DIGEST_SKIP}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def config_kind(colorspaces, has_scene_linear: bool, views_on_srgb) -> str:
    """"blender": a Blender config (bundled or derived, like the studio's):
    defines `Linear Rec.709`, has the scene_linear role and an AgX view on
    sRGB. Anything else is "other" (an ACES config, say)."""
    names = set(colorspaces or ())
    views = set(views_on_srgb or ())
    if "Linear Rec.709" in names and has_scene_linear and "AgX" in views:
        return "blender"
    return "other"


# ---------------------------------------------------------------------------
# what the two blocks say about each other
# ---------------------------------------------------------------------------

def _g(cm, *path, default=None):
    cur = cm
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return default if cur is None else cur


def mismatches(host: dict | None, replica: dict | None) -> list[tuple[str, str, str]]:
    """(level, key, text) rows for the host panel. Empty when both ends
    agree. A missing replica block (an older replica) is one warn row."""
    out: list[tuple[str, str, str]] = []
    if not host:
        return out
    if not replica:
        return [("warn", "peer_cm", "replica reports no colour state (older add-on)")]

    # Compared by CONTENT (2026-10-28): a file's Working Space makes Blender
    # derive a new config with a new cache ID on every load, so two Blenders
    # on the same file never share an ID. What matters is whether the
    # working space, display, view and look in use mean the same thing.
    hw, rw = host.get("working", {}), replica.get("working", {})
    same_ws = ((hw.get("name") or "") == (rw.get("name") or "")
               and matrix_close(hw.get("to_xyz"), rw.get("to_xyz")))

    device = _g(host, "display", "device", default="")
    view = _g(host, "view", "transform", default="")
    look = _g(host, "view", "look", default="")
    r_views = replica.get("views_available") or []
    r_views_known = bool(r_views) or bool(_g(replica, "display", "in_config", default=False))
    display_missing = bool(device) and (
        (_g(replica, "display", "in_config") is False and _g(replica, "display", "device") == device)
        or (bool(replica.get("display_devices")) and device not in replica["display_devices"]))
    view_missing = bool(view) and r_views_known and bool(r_views) and view not in r_views
    look_missing = bool(look) and look != "None" and bool(replica.get("looks_available")) \
        and look not in replica["looks_available"]

    hc, rc = host.get("config", {}), replica.get("config", {})
    h_id = (hc.get("cache_id") or "", hc.get("name") or "")
    r_id = (rc.get("cache_id") or "", rc.get("name") or "")
    if h_id != r_id:
        if same_ws and not (display_missing or view_missing or look_missing):
            out.append(("info", "config",
                        "OCIO configs differ (host %s · replica %s) — the working space, display, view and look in use agree"
                        % (_config_label(hc), _config_label(rc))))
        else:
            out.append(("warn", "config",
                        "⚠ OCIO configs differ: host %s · replica %s"
                        % (_config_label(hc), _config_label(rc))))

    if not same_ws:
        h_src = str(hw.get("source") or "")
        why = " — the host file sets it" if h_src.startswith("file:") else ""
        out.append(("error", "working",
                    "⚠ working space differs: host %s · replica %s%s"
                    % (hw.get("name") or "?", rw.get("name") or "?", why)))

    if display_missing:
        out.append(("error", "display", "⚠ replica config has no display '%s'" % device))
    if view_missing:
        out.append(("error", "view", "⚠ replica has no view '%s' on '%s'" % (view, device or "?")))
    if look_missing:
        out.append(("warn", "look", "look '%s' missing on replica" % look))

    if _g(host, "wb", "on"):
        out.append(("warn", "wb", "white balance in use — not reproduced in QCView"))
    if _g(host, "curves", "on"):
        out.append(("warn", "curves", "curves in use — not reproduced in QCView"))
    if _g(host, "view", "is_hdr"):
        out.append(("warn", "hdr", "HDR view on — QCView shows SDR"))

    h_engine = _g(host, "render", "engine", default="")
    r_mapped = _g(replica, "render", "mapped_engine")
    r_engine = _g(replica, "render", "engine", default="")
    if h_engine == "CYCLES" and r_mapped and r_mapped != "QCB_CYCLES":
        out.append(("warn", "engine", "replica engine %s (expected QCB_CYCLES)" % r_mapped))
    elif h_engine and r_engine and h_engine != r_engine:
        out.append(("warn", "engine", "replica engine %s · host %s" % (r_engine, h_engine)))

    for label, cm in (("host", host), ("replica", replica)):
        if not _g(cm, "ocio", "available", default=False):
            out.append(("warn", "ocio_" + label,
                        "working space assumed Linear Rec.709 on the %s (no PyOpenColorIO)" % label))

    hcy, rcy = _g(host, "render", "cycles"), _g(replica, "render", "cycles")
    if isinstance(hcy, dict) and isinstance(rcy, dict):
        diffs = []
        for key, short in (("device", ""), ("preview_samples", "spp"), ("use_preview_denoising", "denoise")):
            if hcy.get(key) != rcy.get(key):
                diffs.append("%s%s" % (rcy.get(key), (" " + short) if short else ""))
        if diffs:
            out.append(("info", "cycles", "replica Cycles: " + " · ".join(str(d) for d in diffs)))
    return out


def self_notes(cm: dict | None) -> list[tuple[str, str, str]]:
    """Rows about ONE end's own state that a viewer should know regardless
    of the peer. Measured 2026-10-27: Blender 5.2.2 (macOS) writes the
    chosen Display Device's codes straight into its sRGB-tagged window —
    a Rec.2020 or Display P3 device shows a muted picture on the artist's
    own screen, whatever `emulation` says."""
    out: list[tuple[str, str, str]] = []
    if not cm:
        return out
    device = _g(cm, "display", "device", default="")
    if device and device != "sRGB" and _g(cm, "display", "in_config", default=True):
        enc = _g(cm, "display", "encoding", default="") or ""
        if enc in ("", "sdr-video", "display-linear"):
            out.append(("warn", "display_device",
                        "Display Device '%s' on an sRGB window: Blender shows the raw codes — the picture is muted"
                        % device))
    view = _g(cm, "view", "transform", default="")
    if view in ("False Color", "Raw"):
        out.append(("info", "view_kind", "view '%s' is a diagnostic view, not a picture" % view))
    return out


def _config_label(c: dict) -> str:
    name = c.get("name") or c.get("file") or "?"
    kind = c.get("kind") or ""
    cid = (c.get("cache_id") or "")[:8]
    bits = [name]
    if kind and kind != "unknown":
        bits.append(kind)
    if cid:
        bits.append(cid)
    return "/".join(bits)


def worst_level(rows) -> str:
    levels = {r[0] for r in rows}
    for lv in LEVELS:
        if lv in levels:
            return lv
    return ""


# ---------------------------------------------------------------------------
# the link and the panel
# ---------------------------------------------------------------------------

def _b(v) -> str:
    return "1" if v else "0"


def deep_link_params(host: dict | None, replica: dict | None, fmt: dict | None = None) -> dict[str, str]:
    """The colour part of the qcview://stream query, raw strings (the caller
    percent-encodes). Everything the viewer needs to follow the host's
    picture by name — nothing inferred, nothing assumed silently."""
    host = host or {}
    replica = replica or {}
    fmt = fmt or {}
    rows = mismatches(host, replica) if host else []
    wire = "%s-%s-%d" % (fmt.get("transfer", "pq"), fmt.get("primaries", "bt2020"),
                         int(fmt.get("anchor_nits", 100)))
    replica_ws = _g(replica, "working", "name", default="") or _g(host, "working", "name", default="")
    hc = host.get("config", {})
    q = {
        "wire": wire,
        "input": replica_ws,
        "ws": _g(host, "working", "name", default=""),
        "ws_source": str(_g(host, "working", "source", default="") or ""),
        "ws_assumed": _b(_g(host, "working", "assumed", default=True) or _g(replica, "working", "assumed", default=False)),
        "config": "%s:%s:%s" % (hc.get("kind") or "unknown", hc.get("name") or "", hc.get("cache_id") or ""),
        "config_bundled": _b(hc.get("bundled")),
        "display": _g(host, "display", "device", default=""),
        "emulation": str(_g(host, "display", "emulation", default="") or "").lower(),
        "display_cs": _g(host, "display", "colorspace", default=""),
        "view": _g(host, "view", "transform", default=""),
        "look": _g(host, "view", "look", default="") or "None",
        "exposure": "%.4g" % float(_g(host, "view", "exposure", default=0.0) or 0.0),
        "gamma": "%.4g" % float(_g(host, "view", "gamma", default=1.0) or 1.0),
        "wb": _b(_g(host, "wb", "on")),
        "curves": _b(_g(host, "curves", "on")),
        "hdr": _b(_g(host, "view", "is_hdr")),
        "engine": _g(host, "render", "engine", default=""),
        "cm_ok": _b(worst_level(rows) != "error"),
        "cm_n": str(len(rows)),
    }
    xy = _g(host, "display", "xy")
    if isinstance(xy, dict) and all(k in xy for k in ("r", "g", "b", "w")):
        q["display_xy"] = ",".join("%.5f" % float(v) for k in ("r", "g", "b", "w") for v in xy[k])
    gamma_est = _g(host, "display", "gamma_est")
    if gamma_est is not None:
        q["display_gamma"] = "%.3g" % float(gamma_est)
    return q


def summary_rows(cm: dict | None) -> list[tuple[str, str]]:
    """(label, value) for one panel column; "—" where nothing is known."""
    if not cm:
        return [(label, "—") for label in ("Config", "Working space", "Display", "View",
                                            "Look", "Exposure / gamma", "Engine", "Samples")]
    c = cm.get("config", {})
    cfg = _config_label(c) if (c.get("name") or c.get("file")) else "—"
    if c.get("bundled"):
        cfg += " (bundled)"
    ws = _g(cm, "working", "name", default="") or "—"
    if _g(cm, "working", "assumed"):
        ws += " (assumed)"
    elif str(_g(cm, "working", "source", default="") or "").startswith("file:"):
        ws += " (file)"
    device = _g(cm, "display", "device", default="") or "—"
    emu = _g(cm, "display", "emulation")
    if emu:
        device += " · emulation %s" % str(emu).lower()
    view = _g(cm, "view", "transform", default="") or "—"
    if _g(cm, "view", "is_hdr"):
        view += " · HDR"
    look = _g(cm, "view", "look", default="") or "None"
    extra = []
    if _g(cm, "wb", "on"):
        extra.append("white balance")
    if _g(cm, "curves", "on"):
        extra.append("curves")
    if extra:
        look += " · " + ", ".join(extra)
    eg = "%.2f / %.2f" % (float(_g(cm, "view", "exposure", default=0.0) or 0.0),
                          float(_g(cm, "view", "gamma", default=1.0) or 1.0))
    engine = _g(cm, "render", "engine", default="") or "—"
    mapped = _g(cm, "render", "mapped_engine")
    if mapped and mapped != engine:
        engine += " → " + mapped
    cy = _g(cm, "render", "cycles")
    ev = _g(cm, "render", "eevee")
    if isinstance(cy, dict) and engine.startswith("CYCLES"):
        samples = "%s spp · %s · denoise %s" % (cy.get("preview_samples", "?"), cy.get("device", "?"),
                                               "on" if cy.get("use_preview_denoising") else "off")
    elif isinstance(ev, dict):
        samples = "%s taa · shadows %s · rt %s" % (ev.get("taa_samples", "?"),
                                                  "on" if ev.get("use_shadows") else "off",
                                                  "on" if ev.get("use_raytracing") else "off")
    else:
        samples = "—"
    return [("Config", cfg), ("Working space", ws), ("Display", device), ("View", view),
            ("Look", look), ("Exposure / gamma", eg), ("Engine", engine), ("Samples", samples)]
