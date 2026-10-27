"""Colour harness, the ground truth: the harness scene rendered by Cycles in
a background Blender at the stream size, saved three ways —
  ref_raw16.png   scene-linear in the working space (Raw view, 16-bit, clipped 0..1)
  ref_shown.png   through the host's Display Device + View Transform + Look:
                  the codes Blender writes into its window (8-bit)
  ref_srgb.png    the same view for the sRGB display (what an sRGB host sees)
and ref.json with the settings used.

  blender -b --factory-startup --python smokes/harness_colour_ref.py -- <out dir>
  (same QCB_HARNESS_* environment as harness_colour_host.py; OCIO as the host)
"""
import json
import os
import sys

import bpy

OUT = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness_colour_scene import build_scene  # noqa: E402

scene = bpy.context.scene
build_scene(scene)
scene.render.engine = "CYCLES"
scene.cycles.samples = 128
scene.cycles.use_denoising = True
scene.render.resolution_x, scene.render.resolution_y = 1280, 720
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"

device = scene.display_settings.display_device
view = scene.view_settings.view_transform
look = scene.view_settings.look
outputs = (("ref_raw16", "Raw", "None", "16", None),
           ("ref_shown", view, look, "8", device),
           ("ref_srgb", view, look, "8", "sRGB"))
written = {}
for name, v, lk, depth, dev in outputs:
    if dev:
        scene.display_settings.display_device = dev
    scene.view_settings.view_transform = v
    scene.view_settings.look = lk
    scene.render.image_settings.color_depth = depth
    scene.render.filepath = os.path.join(OUT, name + ".png")
    bpy.ops.render.render(write_still=True)
    written[name] = scene.render.filepath
    print("REF wrote", scene.render.filepath)
json.dump({"display": device, "view": view, "look": look, "engine": "CYCLES", "written": written,
           "ocio": os.path.basename(os.environ.get("OCIO", ""))},
          open(os.path.join(OUT, "ref.json"), "w"), indent=1)
