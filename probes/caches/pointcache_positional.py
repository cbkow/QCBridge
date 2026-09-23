import bpy, sys, os, json
S = os.environ["S"]; mode = sys.argv[sys.argv.index("--")+1]; R = {}
def z12(name):
    bpy.context.scene.frame_set(1); bpy.context.scene.frame_set(12)
    o = bpy.data.objects[name]; ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return round(min(v.co.z for v in ev.data.vertices), 4)
def pcinfo(pc): return {"is_baked": pc.is_baked, "info": pc.info}
if mode == "host":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/host/scene.blend", load_ui=False)
    for n in ("MemCloth", "DiskCloth", "ExtCloth"): R[f"host_{n}_z12"] = z12(n)
    # a genuinely unbaked cloth, jumped straight to 12: what "no cache" looks like
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=8, y_subdivisions=8, size=2); u = bpy.context.active_object; u.name = "Unbaked"; u.modifiers.new("Cloth", "CLOTH")
    R["host_Unbaked_z12_direct_jump"] = z12("Unbaked")
elif mode == "replica":
    def append(path, name):
        with bpy.data.libraries.load(path, link=False) as (src, dst): dst.objects = [name]
        o = dst.objects[0]; bpy.context.scene.collection.objects.link(o); return o
    bpy.ops.wm.read_factory_settings(use_empty=True); bpy.context.scene.frame_end = 12
    o = append(f"{S}/host/p1_partial.blend", "MemCloth"); R["appended_MemCloth"] = {**pcinfo(o.modifiers[0].point_cache), "z12": z12("MemCloth")}
    o = append(f"{S}/host/p3_partial.blend", "ExtCloth"); pc = o.modifiers[0].point_cache
    R["appended_ExtCloth_before_rescan"] = {**pcinfo(pc), "z12": z12("ExtCloth")}
    pc.use_external = False; pc.use_external = True
    R["appended_ExtCloth_after_rescan"] = {**pcinfo(pc), "z12": z12("ExtCloth")}
elif mode == "replica_boot":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/qcb_boot_12345.blend", load_ui=False)
    R["renamed_copy_DiskCloth"] = {**pcinfo(bpy.data.objects["DiskCloth"].modifiers[0].point_cache), "z12": z12("DiskCloth")}
    R["renamed_copy_MemCloth"] = {**pcinfo(bpy.data.objects["MemCloth"].modifiers[0].point_cache), "z12": z12("MemCloth")}
elif mode == "replica_same":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/samename/scene.blend", load_ui=False)
    R["same_basename_DiskCloth"] = {**pcinfo(bpy.data.objects["DiskCloth"].modifiers[0].point_cache), "z12": z12("DiskCloth")}
print("PROBE_JSON " + json.dumps(R, indent=1))
