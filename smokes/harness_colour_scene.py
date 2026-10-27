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


def build_scene(scene):
    engine = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")
    scene.render.engine = engine
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
