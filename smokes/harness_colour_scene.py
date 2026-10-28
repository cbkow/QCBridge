"""The colour harness scene (bpy only, no session): a still orange cube — on
material slot 0, the one the faces use — under a blue sky, the camera as
the factory file has it, the host's Display Device / View Transform / Look
from the environment. Shared by harness_colour_host.py (the streaming host)
and harness_colour_ref.py (the reference render) so both see one scene.

  QCB_HARNESS_DISPLAY  (default sRGB)      QCB_HARNESS_VIEW  (default AgX)
  QCB_HARNESS_LOOK     (default None)      QCB_SMOKE_ENGINE  (default CYCLES)
"""
import os

import bpy


def set_working_space(name: str) -> str:
    """The file's Working Space (Blender 5.2, `wm.set_working_color_space`):
    the harness runs on Linear Rec.709 and, with QCB_HARNESS_WORKING, on
    another working space the config defines (Linear Rec.2020, ACEScg) —
    the case that shipped Rec.2020 pixels through the Rec.709 matrix on
    2026-10-27. Returns what Blender reports afterwards."""
    if not name:
        return ""
    try:
        bpy.ops.wm.set_working_color_space(working_space=name, convert_colors=False)
    except Exception as exc:  # noqa: BLE001
        print("harness: set_working_color_space failed:", exc, flush=True)
    try:
        return str(bpy.data.colorspace.working_space)
    except Exception:
        return ""


def build_scene(scene):
    engine = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")
    scene.render.engine = engine
    ws = set_working_space(os.environ.get("QCB_HARNESS_WORKING", ""))
    if ws:
        print("harness: working space", ws, flush=True)
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    if engine == "CYCLES":
        # Converged and denoised at both ends: the comparison is of colour,
        # not of sampling noise (16 undenoised samples cost ~6 dB).
        scene.cycles.preview_samples = 128
        scene.cycles.use_preview_denoising = True
        scene.cycles.samples = 128
        scene.cycles.use_denoising = True
    cube = bpy.data.objects["Cube"]
    mat = bpy.data.materials.new("Orange")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1.0, 0.35, 0.05, 1.0)
    if cube.data.materials:
        cube.data.materials[0] = mat
    else:
        cube.data.materials.append(mat)
    world = bpy.data.worlds[0] if bpy.data.worlds else bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.25, 0.6, 1.0)
    scene.camera = bpy.data.objects["Camera"]
    scene.display_settings.display_device = os.environ.get("QCB_HARNESS_DISPLAY", "sRGB")
    scene.view_settings.view_transform = os.environ.get("QCB_HARNESS_VIEW", "AgX")
    scene.view_settings.look = os.environ.get("QCB_HARNESS_LOOK", "None")
