"""Fill the colour-state block from this Blender: bpy for the scene's colour
management and render settings, PyOpenColorIO for the config in use (the
working space and its matrix, the displays, the views, the display
primaries). Main thread only — bpy and the OCIO config load live here; the
IO thread reads the finished dict from `session.state`.

PyOpenColorIO ships inside Blender's Python (2.5 in 5.2; probed 2026-10-27)
and `os.environ["OCIO"]` names the config Blender loaded, bundled or the
studio's. When the import fails (a stripped build, an older Blender) the
block says so and the working space is ASSUMED Linear Rec.709 — shown as
such on the panel, carried as `ws_assumed=1` in the link.
"""
from __future__ import annotations

import math
import os

import bpy

from ..ring1 import colour_state as cs

try:
    import PyOpenColorIO as OCIO  # type: ignore
except Exception as _exc:  # pragma: no cover - depends on the Blender build
    OCIO = None
    _IMPORT_ERROR = str(_exc)
else:
    _IMPORT_ERROR = ""

_cache: dict = {"env": None, "cfg": None, "identity": None, "working": None,
                "displays": None, "looks": None, "display_cache": {}}

_XYZ_ROLE = "cie_xyz_d65_interchange"


# ---------------------------------------------------------------------------
# the config
# ---------------------------------------------------------------------------

def _load_config():
    """The config Blender is running, cached by the OCIO env value."""
    if OCIO is None:
        return None
    env = os.environ.get("OCIO", "")
    if _cache["cfg"] is not None and _cache["env"] == env:
        return _cache["cfg"]
    try:
        cfg = OCIO.GetCurrentConfig()
    except Exception:
        try:
            cfg = OCIO.Config.CreateFromEnv()
        except Exception:
            cfg = None
    _cache.update({"env": env, "cfg": cfg, "identity": None, "working": None,
                   "displays": None, "looks": None, "display_cache": {}})
    return cfg


def config_identity(cfg) -> dict:
    if _cache["identity"] is not None:
        return dict(_cache["identity"])
    env = os.environ.get("OCIO", "")
    try:
        local = os.path.normpath(bpy.utils.resource_path("LOCAL"))
        bundled = bool(env) and os.path.normpath(env).startswith(local)
    except Exception:
        bundled = None
    name = ""
    cache_id = ""
    kind = "unknown"
    if cfg is not None:
        try:
            name = cfg.getName() or ""
            cache_id = (cfg.getCacheID() or "")[:16]
            names = [c.getName() for c in cfg.getColorSpaces()]
            views = list(cfg.getViews("sRGB")) if "sRGB" in list(cfg.getDisplays()) else []
            kind = cs.config_kind(names, cfg.hasRole("scene_linear"), views)
        except Exception:
            pass
    ident = {"kind": kind, "name": name, "file": os.path.basename(env) if env else "",
             "cache_id": cache_id, "bundled": bundled}
    _cache["identity"] = ident
    return dict(ident)


def _cpu(cfg, src: str, dst: str):
    return cfg.getProcessor(src, dst).getDefaultCPUProcessor()


def working_block(cfg) -> dict:
    """The scene_linear role: its name and matrix to CIE XYZ D65, with a
    linearity check (the role should be linear; say so if it is not)."""
    if _cache["working"] is not None:
        return dict(_cache["working"])
    out = {"name": "", "to_xyz": None, "linear": None, "assumed": True}
    try:
        ws = cfg.getColorSpace(OCIO.ROLE_SCENE_LINEAR)
        name = ws.getName() if ws else ""
        out["name"] = name
        if name and cfg.hasRole(_XYZ_ROLE):
            p = _cpu(cfg, name, _XYZ_ROLE)
            cols = [p.applyRGB([1.0, 0.0, 0.0]), p.applyRGB([0.0, 1.0, 0.0]), p.applyRGB([0.0, 0.0, 1.0])]
            white = p.applyRGB([1.0, 1.0, 1.0])
            half = p.applyRGB([0.5, 0.5, 0.5])
            linear = all(abs(half[i] - 0.5 * white[i]) < 1e-4 for i in range(3))
            additive = all(abs(sum(cols[j][i] for j in range(3)) - white[i]) < 1e-4 for i in range(3))
            out["to_xyz"] = cs.matrix_rows(cs.mat_from_columns(cols))
            out["linear"] = bool(linear and additive)
            out["assumed"] = False
    except Exception:
        pass
    if out["to_xyz"] is None:
        out["to_xyz"] = cs.matrix_rows(cs.M_709_TO_XYZ)
        out["assumed"] = True
        out["linear"] = True
    _cache["working"] = out
    return dict(out)


def _display_colorspace(cfg, device: str, view: str) -> str:
    """The display's own colourspace (the Standard view's, or the display's
    name) — never the view's: the display → display step of emulation must
    not undo a view baked into a colourspace (studio configs do that)."""
    for v in ("Standard", view):
        try:
            n = cfg.getDisplayViewColorSpaceName(device, v)
        except Exception:
            n = None
        if n and n != "<USE_DISPLAY_NAME>":
            return n
    return device


def display_block(cfg, device: str, view: str, emulation) -> dict:
    key = (device, view)
    if key in _cache["display_cache"]:
        out = dict(_cache["display_cache"][key])
        out["emulation"] = emulation
        return out
    out = {"device": device, "emulation": emulation, "colorspace": "", "encoding": "",
           "in_config": None, "xy": None, "gamma_est": None}
    try:
        displays = list(cfg.getDisplays())
        out["in_config"] = device in displays
        if out["in_config"]:
            dcs = _display_colorspace(cfg, device, view)
            out["colorspace"] = dcs
            spc = cfg.getColorSpace(dcs)
            out["encoding"] = (spc.getEncoding() or "") if spc else ""
            if cfg.hasRole(_XYZ_ROLE) and spc is not None:
                p = _cpu(cfg, dcs, _XYZ_ROLE)

                def xy(v):
                    X, Y, Z = v
                    s = X + Y + Z
                    return [round(X / s, 5), round(Y / s, 5)] if s > 0 else [0.0, 0.0]

                out["xy"] = {k: xy(p.applyRGB(list(c))) for k, c in
                             (("r", (1, 0, 0)), ("g", (0, 1, 0)), ("b", (0, 0, 1)), ("w", (1, 1, 1)))}
                yw = p.applyRGB([1.0, 1.0, 1.0])[1]
                yh = p.applyRGB([0.5, 0.5, 0.5])[1]
                if yw > 0 and yh > 0:
                    out["gamma_est"] = round(math.log(yh / yw) / math.log(0.5), 3)
    except Exception:
        pass
    _cache["display_cache"][key] = dict(out)
    return out


def _views(cfg, device: str) -> list[str]:
    try:
        return list(cfg.getViews(device))
    except Exception:
        return []


def _looks(cfg) -> list[str]:
    if _cache["looks"] is None:
        try:
            _cache["looks"] = [lk.getName() for lk in cfg.getLooks()]
        except Exception:
            _cache["looks"] = []
    return list(_cache["looks"])


def _displays(cfg) -> list[str]:
    if _cache["displays"] is None:
        try:
            _cache["displays"] = list(cfg.getDisplays())
        except Exception:
            _cache["displays"] = []
    return list(_cache["displays"])


# ---------------------------------------------------------------------------
# the scene
# ---------------------------------------------------------------------------

def _scene_fields(scene, role: str) -> dict:
    vs, ds = scene.view_settings, scene.display_settings
    out = {
        "display": {"device": ds.display_device, "emulation": getattr(ds, "emulation", None)},
        "view": {"transform": vs.view_transform, "look": vs.look,
                 "exposure": float(vs.exposure), "gamma": float(vs.gamma),
                 "is_hdr": getattr(vs, "is_hdr", None), "use_hdr_view": getattr(vs, "use_hdr_view", None)},
        "wb": {"on": bool(getattr(vs, "use_white_balance", False)),
               "temperature": getattr(vs, "white_balance_temperature", None),
               "tint": getattr(vs, "white_balance_tint", None)},
        "curves": {"on": bool(vs.use_curve_mapping)},
        "render": render_block(scene, role),
    }
    return out


def render_block(scene, role: str) -> dict:
    from . import engine_qcb
    engine_raw = scene.render.engine
    engine = engine_qcb.unmap_engine(engine_raw)
    mapped = engine_raw if engine_raw != engine else (engine_qcb.map_engine(engine) if role == "REPLICA" else None)
    out = {"engine": engine, "mapped_engine": mapped, "cycles": None, "eevee": None}
    cy = getattr(scene, "cycles", None)
    if cy is not None:
        out["cycles"] = {
            "device": getattr(cy, "device", None),
            "preview_samples": getattr(cy, "preview_samples", None),
            "use_preview_denoising": bool(getattr(cy, "use_preview_denoising", False)),
            "preview_denoiser": getattr(cy, "preview_denoiser", None),
            "samples": getattr(cy, "samples", None),
            "film_exposure": getattr(cy, "film_exposure", None),
        }
    ev = getattr(scene, "eevee", None)
    if ev is not None:
        out["eevee"] = {
            "taa_samples": getattr(ev, "taa_samples", None),
            "use_shadows": bool(getattr(ev, "use_shadows", False)),
            "use_raytracing": bool(getattr(ev, "use_raytracing", False)),
        }
    return out


def read_scene_cm(scene, role: str) -> dict:
    """The whole block for this end. Never raises: a failure lands as the
    fallback with the reason in `ocio.error`."""
    blender = bpy.app.version_string
    fields = _scene_fields(scene, role)
    cfg = _load_config()
    if cfg is None:
        return cs.fallback_cm(role, blender, fields, error=_IMPORT_ERROR or "no OCIO config")
    cm = cs.empty_cm(role, blender)
    for k, v in fields.items():
        cm[k].update(v) if isinstance(cm.get(k), dict) and isinstance(v, dict) else cm.__setitem__(k, v)
    try:
        cm["config"] = config_identity(cfg)
        cm["ocio"] = {"available": True, "version": OCIO.GetVersion(), "error": ""}
        cm["working"] = working_block(cfg)
        device, view = cm["display"]["device"], cm["view"]["transform"]
        cm["display"].update(display_block(cfg, device, view, cm["display"].get("emulation")))
        views = _views(cfg, device) if cm["display"]["in_config"] else []
        looks = _looks(cfg)
        cm["views_available"] = views
        cm["looks_available"] = looks
        cm["display_devices"] = _displays(cfg)
        cm["view"]["in_config"] = (view in views) if views else False
        look = cm["view"]["look"]
        cm["view"]["look_in_config"] = True if (not look or look == "None") else (look in looks)
    except Exception as exc:
        fb = cs.fallback_cm(role, blender, fields, error="OCIO read failed: %s" % exc)
        return fb
    cm["digest"] = cs.digest(cm)
    return cm


def refresh(role: str) -> bool:
    """Recompute `session.state["cm"]`; True when the digest moved."""
    from . import session
    scene = bpy.context.scene
    if scene is None:
        return False
    cm = read_scene_cm(scene, role)
    old = (session.state.get("cm") or {}).get("digest")
    session.state["cm"] = cm
    return cm.get("digest") != old
