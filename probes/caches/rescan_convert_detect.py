import bpy, sys, os, json, glob, shutil
P = os.environ["P"]; mode = sys.argv[sys.argv.index("--")+1]; R = {}
def z12(name):
    bpy.context.scene.frame_set(1); bpy.context.scene.frame_set(12)
    ev = bpy.data.objects[name].evaluated_get(bpy.context.evaluated_depsgraph_get()); return round(min(v.co.z for v in ev.data.vertices), 4)
def z10(o):
    bpy.context.scene.frame_set(1); bpy.context.scene.frame_set(10)
    ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get()); return round(max(v.co.z for v in ev.data.vertices), 3)
def pcinfo(pc): return {"is_baked": pc.is_baked, "info": pc.info}
def append(path, name):
    with bpy.data.libraries.load(path, link=False) as (src, dst): dst.objects = [name]
    o = dst.objects[0]; bpy.context.scene.collection.objects.link(o); return o
if mode == "rescan_alternatives":
    bpy.ops.wm.read_factory_settings(use_empty=True); bpy.context.scene.frame_end = 12
    o = append(f"{P}/pc/host/p3_partial.blend", "ExtCloth"); pc = o.modifiers[0].point_cache
    R["before"] = pcinfo(pc)
    pc.filepath = pc.filepath; R["after_filepath_reassign"] = pcinfo(pc)
    pc.frame_end = pc.frame_end; R["after_frame_end_reassign"] = pcinfo(pc)
    pc.name = pc.name; R["after_name_reassign"] = pcinfo(pc)
    R["z12_regardless"] = z12("ExtCloth")
    R["ext_files_still"] = len(glob.glob(f"{P}/pc/ext/*"))
elif mode == "convert":
    # Converting an ALREADY BAKED cache to external: do the frames migrate, or is a re-bake required?
    shutil.copytree(f"{P}/pc/host", f"{P}/pc/convert", dirs_exist_ok=True)
    bpy.ops.wm.open_mainfile(filepath=f"{P}/pc/convert/scene.blend", load_ui=False)
    os.makedirs(f"{P}/pc/ext_disk", exist_ok=True); os.makedirs(f"{P}/pc/ext_mem", exist_ok=True)
    pc = bpy.data.objects["DiskCloth"].modifiers[0].point_cache
    pc.use_external = True; pc.filepath = f"{P}/pc/ext_disk"
    R["disk_to_external"] = {**pcinfo(pc), "files_at_new_path": len(glob.glob(f"{P}/pc/ext_disk/*")), "old_files_kept": len(glob.glob(f"{P}/pc/convert/blendcache_scene/*")), "z12": z12("DiskCloth")}
    pm = bpy.data.objects["MemCloth"].modifiers[0].point_cache
    pm.use_disk_cache = True; pm.use_external = True; pm.filepath = f"{P}/pc/ext_mem"
    R["memory_to_external"] = {**pcinfo(pm), "files_at_new_path": len(glob.glob(f"{P}/pc/ext_mem/*")), "z12": z12("MemCloth")}
    # and does turning external OFF on a cache that was external delete the external files? (the memory note's warning)
    pc.use_external = False
    R["external_off_deletes_ext_files"] = {"ext_disk_files_now": len(glob.glob(f"{P}/pc/ext_disk/*")), "orig_ext_files_now": len(glob.glob(f"{P}/pc/ext/*"))}
elif mode == "gn_detect_and_dir":
    bpy.ops.wm.open_mainfile(filepath=f"{P}/gn/host/scene.blend", load_ui=False)
    calls = []
    def h(scene, depsgraph): calls.append(len(depsgraph.updates))
    bpy.app.handlers.depsgraph_update_post.append(h)
    o = bpy.data.objects["SimUnbaked"]; bpy.context.view_layer.update(); n0 = len(calls)
    with bpy.context.temp_override(scene=bpy.context.scene, active_object=o, object=o, selected_objects=[o]):
        bpy.ops.object.simulation_nodes_cache_bake(selected=True)
    R["depsgraph_update_post_calls_during_gn_bake"] = len(calls) - n0
    n1 = len(calls)
    with bpy.context.temp_override(scene=bpy.context.scene, active_object=o, object=o, selected_objects=[o]):
        bpy.ops.object.simulation_nodes_cache_delete(selected=True)
    R["calls_during_gn_bake_delete"] = len(calls) - n1
    bpy.app.handlers.depsgraph_update_post.remove(h)
    # likewise for a point-cache bake/free (what the sweep currently covers)
    calls.clear(); bpy.app.handlers.depsgraph_update_post.append(h)
    c = bpy.data.objects["MemCloth"] if "MemCloth" in bpy.data.objects else None
    R["pc_probe_skipped"] = c is None
    bpy.app.handlers.depsgraph_update_post.remove(h)
elif mode == "gn_dir_on_replica":
    bpy.ops.wm.read_factory_settings(use_empty=True); bpy.context.scene.frame_end = 10
    o = append(f"{P}/gn/host/disk2_partial.blend", "SimDisk"); m = o.modifiers[0]
    R["appended_dir"] = m.bake_directory; R["z10_before"] = z10(o)
    m.bake_directory = f"{P}/gn/host/qcbcache/SimDisk"; R["z10_after_setting_dir"] = z10(o)
    m.bake_directory = f"{P}/gn/host/qcbcache/SimDisk"; bpy.context.view_layer.update(); R["z10_after_update"] = z10(o)
print("PROBE_JSON " + json.dumps(R, indent=1))
