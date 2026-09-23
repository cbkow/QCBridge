"""Replica side of the tier-2 path against a shared external cache.

Run shared_dir_host.py first (it leaves $S/host.blend), then for each MODE:
    Blender -b --factory-startup -noaudio --python shared_dir_append.py -- MODE
MODE: seeks      appended object untouched, seek around → files/z
      toggle     use_external off→on, then seeks
      same_path  assign filepath = same path, then seeks
      alias_path assign filepath = a copy of the directory, then seeks
      wipe       re-set use_disk_cache/use_external + reseek (the destructive
                 sequence: 24 files → 2)
Each prints one line per step; the host's z at frame 20 is 1.540838.
"""
import os, shutil, subprocess, sys
import bpy

S = os.environ["S"]; MODE = sys.argv[-1]
BL = "/Applications/Blender.app/Contents/MacOS/Blender"
D = f"{S}/ext_append"; A = f"{S}/ext_append_alias"
shutil.rmtree(D, ignore_errors=True); shutil.rmtree(A, ignore_errors=True); os.makedirs(D)
subprocess.run([BL, "-b", "--factory-startup", "-noaudio", "--python-expr", f'''
import bpy
bpy.ops.wm.open_mainfile(filepath="{S}/host.blend", load_ui=False)
s=bpy.data.objects["Sheet"]; pc=s.modifiers["Cloth"].point_cache
pc.use_disk_cache=True; pc.use_external=True; pc.filepath="{D}"
with bpy.context.temp_override(scene=bpy.context.scene, active_object=s, point_cache=pc): bpy.ops.ptcache.bake(bake=True)
bpy.data.libraries.write("{S}/partial_append.blend", {{s}}, compress=True)
'''], capture_output=True)
shutil.copytree(D, A)
def files(): return (len(os.listdir(D)), len(os.listdir(A)))
bpy.ops.wm.open_mainfile(filepath=f"{S}/host.blend", load_ui=False)
sc = bpy.context.scene; old = bpy.data.objects["Sheet"]; sc.frame_set(1); sc.frame_set(20)
with bpy.data.libraries.load(f"{S}/partial_append.blend") as (f, t):
    t.objects = ["Sheet"]
new = t.objects[0]; old.user_remap(new); bpy.data.objects.remove(old); new.name = "Sheet"; sc.collection.objects.link(new)
pc = new.modifiers["Cloth"].point_cache
def mz():
    deps = bpy.context.evaluated_depsgraph_get(); me = new.evaluated_get(deps).to_mesh()
    return round(sum((new.matrix_world @ v.co).z for v in me.vertices) / len(me.vertices), 6)
print("APPEND", MODE, "arrived: baked", pc.is_baked, "z", mz(), "files", files())
if MODE == "toggle":
    pc.use_external = False; pc.use_external = True
elif MODE == "same_path":
    pc.filepath = D
elif MODE == "alias_path":
    pc.filepath = A
elif MODE == "wipe":
    pc.use_disk_cache = True; pc.use_external = True; pc.filepath = D; sc.frame_set(19)
print("APPEND", MODE, "after step: baked", pc.is_baked, pc.info, "files", files())
for fr in (20, 19, 21, 5, 20):
    sc.frame_set(fr); print("APPEND", MODE, "frame", fr, "z", mz(), "files", files(), "baked", pc.is_baked)
