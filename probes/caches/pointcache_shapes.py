import bpy, sys, os, json, glob
S = os.environ["S"]; mode = sys.argv[sys.argv.index("--")+1]
R = {}
def pcinfo(pc): return {"is_baked": pc.is_baked, "is_outdated": pc.is_outdated, "info": pc.info, "disk": pc.use_disk_cache, "ext": pc.use_external, "filepath": pc.filepath}
def bake(obj, pc):
    with bpy.context.temp_override(scene=bpy.context.scene, active_object=obj, point_cache=pc):
        return bpy.ops.ptcache.bake(bake=True)
def cloth(name):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=8, y_subdivisions=8, size=2); o = bpy.context.active_object; o.name = name
    m = o.modifiers.new("Cloth", "CLOTH"); m.point_cache.frame_end = 12; return o, m.point_cache

if mode == "host":
    bpy.ops.wm.read_factory_settings(use_empty=True); sc = bpy.context.scene; sc.frame_end = 12
    # ---- P1 memory cache
    o1, pc1 = cloth("MemCloth"); r = bake(o1, pc1); R["P1_bake"] = str(r); R["P1_host"] = pcinfo(pc1)
    bpy.data.libraries.write(f"{S}/host/p1_partial.blend", {o1}, compress=True)
    # ---- P2 disk cache: needs a saved file first (blendcache dir derives from the .blend name)
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/host/scene.blend")
    o2, pc2 = cloth("DiskCloth"); pc2.use_disk_cache = True; r = bake(o2, pc2); R["P2_bake"] = str(r); R["P2_host"] = pcinfo(pc2)
    R["P2_disk_files"] = sorted(os.path.basename(p) for p in glob.glob(f"{S}/host/blendcache_scene/*"))[:3] + [f"... {len(glob.glob(f'{S}/host/blendcache_scene/*'))} files"]
    bpy.data.libraries.write(f"{S}/host/p2_partial.blend", {o2}, compress=True)
    # the bootstrap shape: a full copy under a DIFFERENT name in a DIFFERENT dir
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/replica/qcb_boot_12345.blend", copy=True, relative_remap=False)
    # and the same copy under the SAME basename, cache dir copied alongside (what shared storage would look like)
    os.makedirs(f"{S}/replica/samename", exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/replica/samename/scene.blend", copy=True, relative_remap=False)
    import shutil; shutil.copytree(f"{S}/host/blendcache_scene", f"{S}/replica/samename/blendcache_scene")
    # ---- P3 external cache at an explicit absolute path
    o3, pc3 = cloth("ExtCloth"); pc3.use_disk_cache = True; pc3.use_external = True; pc3.filepath = f"{S}/ext"
    r = bake(o3, pc3); R["P3_bake"] = str(r); R["P3_host"] = pcinfo(pc3)
    R["P3_ext_files"] = len(glob.glob(f"{S}/ext/*"))
    bpy.data.libraries.write(f"{S}/host/p3_partial.blend", {o3}, compress=True)
    bpy.ops.wm.save_mainfile()
elif mode == "replica":
    def append(path, name):
        with bpy.data.libraries.load(path, link=False) as (src, dst): dst.objects = [name]
        o = dst.objects[0]; bpy.context.scene.collection.objects.link(o); return o
    bpy.ops.wm.read_factory_settings(use_empty=True); bpy.context.scene.frame_end = 12
    # P1: memory cache via partial write, in a fresh process
    o = append(f"{S}/host/p1_partial.blend", "MemCloth"); R["P1_appended"] = pcinfo(o.modifiers[0].point_cache)
    # P2a: disk cache via partial write, into an UNSAVED file
    o = append(f"{S}/host/p2_partial.blend", "DiskCloth"); R["P2a_appended_unsaved"] = pcinfo(o.modifiers[0].point_cache)
    # P3: external cache via partial write
    o = append(f"{S}/host/p3_partial.blend", "ExtCloth"); pc = o.modifiers[0].point_cache; R["P3_appended"] = pcinfo(pc)
    # rescan behaviour: add a frame file after the fact, then re-trigger by toggling use_external
    R["P3_rescan_toggle"] = None
    if pc.use_external:
        pc.use_external = False; pc.use_external = True; R["P3_rescan_toggle"] = pcinfo(pc)
elif mode == "replica_boot":
    # P2b: the bootstrap shape — open the full copy that was saved under a different name/dir
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/qcb_boot_12345.blend", load_ui=False)
    pc = bpy.data.objects["DiskCloth"].modifiers[0].point_cache; R["P2b_boot_copy_renamed"] = pcinfo(pc)
    R["P2b_looks_for"] = f"{S}/replica/blendcache_qcb_boot_12345/ exists={os.path.isdir(S+'/replica/blendcache_qcb_boot_12345')}"
    pcm = bpy.data.objects["MemCloth"].modifiers[0].point_cache; R["P2b_memcache_in_full_copy"] = pcinfo(pcm)
elif mode == "replica_same":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/samename/scene.blend", load_ui=False)
    pc = bpy.data.objects["DiskCloth"].modifiers[0].point_cache; R["P2c_same_basename_cache_copied"] = pcinfo(pc)
print("PROBE_JSON " + json.dumps(R, indent=1))
