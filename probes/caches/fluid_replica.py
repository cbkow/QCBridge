import bpy, os, sys
mode = sys.argv[-1]
sc = bpy.context.scene
def nverts(o, f):
    sc.frame_set(f); ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get()); return len(ev.data.vertices)
if mode == "append":
    with bpy.data.libraries.load("$S/partial.blend") as (f, t): t.objects = ["Domain"]
    dom = t.objects[0]; sc.collection.objects.link(dom); sc.frame_end = 12
else:
    bpy.ops.wm.open_mainfile(filepath="$S/renamed_copy.blend", load_ui=False); sc = bpy.context.scene; dom = bpy.data.objects["Domain"]
ds = dom.modifiers["Fluid"].domain_settings
print("FLUID", mode, "arrived cache_directory", repr(ds.cache_directory), "baked_data", ds.has_cache_baked_data)
print("FLUID", mode, "verts f8 as arrived", nverts(dom, 8))
os.symlink("$S/cache", "$S/alias/cache") if not os.path.exists("$S/alias/cache") else None
ds.cache_directory = "$S/alias/cache"   # the replica's mapped path (an alias of the same files)
print("FLUID", mode, "after remap: baked_data", ds.has_cache_baked_data, "verts f8", nverts(dom, 8), "f1", nverts(dom, 1))
