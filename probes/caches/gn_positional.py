import bpy, sys, os, json, glob
S = os.environ["S"]; mode = sys.argv[sys.argv.index("--")+1]; R = {}
def z10(o):
    bpy.context.scene.frame_set(1); bpy.context.scene.frame_set(10)
    ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get()); return round(max(v.co.z for v in ev.data.vertices), 3)
def files(root): return sorted(p.replace(root, "") for p in glob.glob(f"{root}/**/*", recursive=True) if os.path.isfile(p) and "blendcache" in p or (os.path.isfile(p) and "qcbcache" in p))
def bake(o):
    with bpy.context.temp_override(scene=bpy.context.scene, active_object=o, object=o, selected_objects=[o]):
        return str(bpy.ops.object.simulation_nodes_cache_bake(selected=True))
if mode == "host":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/host/scene.blend", load_ui=False)
    R["bake_mode_items"] = [i.identifier for i in bpy.types.NodesModifierBake.bl_rna.properties["bake_mode"].enum_items]
    R["bake_target_items"] = [i.identifier for i in bpy.types.NodesModifier.bl_rna.properties["bake_target"].enum_items]
    R["host_SimPacked_z10"] = z10(bpy.data.objects["SimPacked"]); R["host_SimUnbaked_z10"] = z10(bpy.data.objects["SimUnbaked"])
    # DISK, this time with an EXPLICIT modifier bake directory (the path-mappable lever), then save and look
    o = bpy.data.objects["SimDisk"]; m = o.modifiers[0]; m.bake_target = "DISK"; m.bake_directory = f"{S}/host/qcbcache/SimDisk"
    R["disk_explicit_bake_op"] = bake(o); bpy.ops.wm.save_mainfile()
    R["disk_explicit_files"] = files(f"{S}/host")[:6]; R["disk_explicit_count"] = len(files(f"{S}/host"))
    R["host_SimDisk_z10"] = z10(o)
    bpy.data.libraries.write(f"{S}/host/disk2_partial.blend", {o}, compress=True)
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/replica/qcb_boot_888.blend", copy=True, relative_remap=False)
elif mode == "replica":
    def append(path, name):
        with bpy.data.libraries.load(path, link=False) as (src, dst): dst.objects = [name]
        o = dst.objects[0]; bpy.context.scene.collection.objects.link(o); return o
    bpy.ops.wm.read_factory_settings(use_empty=True); bpy.context.scene.frame_end = 10
    o = append(f"{S}/host/packed_partial.blend", "SimPacked"); R["appended_packed_z10"] = z10(o)
    o2 = append(f"{S}/host/unbaked_partial.blend", "SimUnbaked"); R["appended_unbaked_z10"] = z10(o2)
    o3 = append(f"{S}/host/disk2_partial.blend", "SimDisk"); R["appended_disk_explicit_dir"] = o3.modifiers[0].bake_directory; R["appended_disk_explicit_z10"] = z10(o3)
elif mode == "replica_boot":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/qcb_boot_888.blend", load_ui=False)
    o = bpy.data.objects["SimDisk"]; R["boot_disk_dir"] = o.modifiers[0].bake_directory; R["boot_disk_z10"] = z10(o)
    R["boot_packed_z10"] = z10(bpy.data.objects["SimPacked"])
print("PROBE_JSON " + json.dumps(R, indent=1))
