import bpy, os, sys, subprocess
BL="/Applications/Blender.app/Contents/MacOS/Blender"
mode=sys.argv[-1]; D="$S/ext2" if mode=="bug" else "$S/ext3"
bpy.ops.wm.open_mainfile(filepath="$S/host.blend", load_ui=False)
s=bpy.data.objects["Sheet"]; pc=s.modifiers["Cloth"].point_cache; sc=bpy.context.scene
def mz():
    deps=bpy.context.evaluated_depsgraph_get(); me=s.evaluated_get(deps).to_mesh(); return round(sum((s.matrix_world@v.co).z for v in me.vertices)/len(me.vertices),6)
def files(): return sorted(os.listdir(D))
# replica receives external settings while the dir is empty
pc.use_disk_cache=True; pc.use_external=True; pc.filepath=D
if mode=="fix":
    if not any(f.endswith(".bphys") for f in os.listdir(D)):
        pc.use_external=False   # keep it in memory until frames exist
sc.frame_set(1); sc.frame_set(2); print("SEQ", mode, "replica evaluated f1,f2 → files", files(), "baked", pc.is_baked)
# host bakes 24 frames into the shared dir (separate process)
subprocess.run([BL,"-b","--factory-startup","-noaudio","--python-expr",f'''
import bpy
bpy.ops.wm.open_mainfile(filepath="$S/host.blend", load_ui=False)
s=bpy.data.objects["Sheet"]; pc=s.modifiers["Cloth"].point_cache
pc.use_disk_cache=True; pc.use_external=True; pc.filepath="{D}"
with bpy.context.temp_override(scene=bpy.context.scene, active_object=s, point_cache=pc): bpy.ops.ptcache.bake(bake=True)
'''], capture_output=True)
print("SEQ", mode, "host baked → files", len(files()))
# replica jumps to 20 before the settings blob (is_baked) arrives
sc.frame_set(20); print("SEQ", mode, "replica jump 20 (still unbaked state) → files", len(files()), "z", mz(), "baked", pc.is_baked)
# settings blob arrives: external + baked; replica localizes (rescan) and re-seeks
pc.use_external=True; pc.filepath=D; sc.frame_set(19); sc.frame_set(20)
print("SEQ", mode, "after blob+rescan → files", len(files()), "z", mz(), "baked", pc.is_baked, pc.info)
