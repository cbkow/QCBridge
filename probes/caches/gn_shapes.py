import bpy, sys, os, json, glob
S = os.environ["S"]; mode = sys.argv[sys.argv.index("--")+1]; R = {}
def bakes(mod): return [{"id": b.bake_id, "target": b.bake_target, "mode": b.bake_mode, "custom_path": b.use_custom_path, "dir": b.directory, "node": b.node.name if b.node else None} for b in mod.bakes]
def build(name):
    bpy.ops.mesh.primitive_cube_add(); o = bpy.context.active_object; o.name = name
    ng = bpy.data.node_groups.new(name + "_tree", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
    si = ng.nodes.new("GeometryNodeSimulationInput"); so = ng.nodes.new("GeometryNodeSimulationOutput")
    si.pair_with_output(so); so.state_items.new("GEOMETRY", "Geometry")
    sp = ng.nodes.new("GeometryNodeSetPosition"); off = ng.nodes.new("FunctionNodeInputVector"); off.vector = (0.0, 0.0, 0.1)
    L = ng.links.new
    L(gi.outputs["Geometry"], si.inputs["Geometry"]); L(si.outputs["Geometry"], sp.inputs["Geometry"]); L(off.outputs["Vector"], sp.inputs["Offset"])
    L(sp.outputs["Geometry"], so.inputs["Geometry"]); L(so.outputs["Geometry"], go.inputs["Geometry"])
    m = o.modifiers.new("Sim", "NODES"); m.node_group = ng; return o, m
def zpos(o):
    ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get()); return round(max(v.co.z for v in ev.data.vertices), 3)
if mode == "host":
    bpy.ops.wm.read_factory_settings(use_empty=True); sc = bpy.context.scene; sc.frame_end = 10
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/host/scene.blend")
    o, m = build("SimPacked"); R["packed_bakes_before"] = bakes(m); R["mod_bake_target"] = m.bake_target; R["mod_bake_dir"] = m.bake_directory
    sc.frame_set(10); R["z_at_10_unbaked"] = zpos(o)
    with bpy.context.temp_override(scene=sc, active_object=o, object=o, selected_objects=[o]):
        r = bpy.ops.object.simulation_nodes_cache_bake(selected=True)
    R["packed_bake_op"] = str(r); R["packed_bakes_after"] = bakes(m)
    R["disk_dir_created_for_packed"] = sorted(glob.glob(f"{S}/host/blendcache_scene/**", recursive=True))[:4]
    bpy.data.libraries.write(f"{S}/host/packed_partial.blend", {o}, compress=True)
    R["packed_partial_size"] = os.path.getsize(f"{S}/host/packed_partial.blend")
    # unbaked twin for a size comparison
    o2, m2 = build("SimUnbaked"); bpy.data.libraries.write(f"{S}/host/unbaked_partial.blend", {o2}, compress=True)
    R["unbaked_partial_size"] = os.path.getsize(f"{S}/host/unbaked_partial.blend")
    # DISK target
    o3, m3 = build("SimDisk"); m3.bake_target = "DISK"
    with bpy.context.temp_override(scene=sc, active_object=o3, object=o3, selected_objects=[o3]):
        r = bpy.ops.object.simulation_nodes_cache_bake(selected=True)
    R["disk_bake_op"] = str(r); R["disk_bakes_after"] = bakes(m3); R["disk_mod_dir"] = m3.bake_directory
    files = sorted(glob.glob(f"{S}/host/**/*", recursive=True)); R["disk_files"] = [f.replace(S, "") for f in files if os.path.isfile(f) and "blendcache" in f][:6]
    R["disk_file_count"] = len([f for f in files if os.path.isfile(f) and "blendcache" in f])
    bpy.data.libraries.write(f"{S}/host/disk_partial.blend", {o3}, compress=True)
    bpy.ops.wm.save_as_mainfile(filepath=f"{S}/replica/qcb_boot_777.blend", copy=True, relative_remap=False)
    bpy.ops.wm.save_mainfile()
elif mode == "replica":
    def append(path, name):
        with bpy.data.libraries.load(path, link=False) as (src, dst): dst.objects = [name]
        o = dst.objects[0]; bpy.context.scene.collection.objects.link(o); return o
    bpy.ops.wm.read_factory_settings(use_empty=True); sc = bpy.context.scene; sc.frame_end = 10
    o = append(f"{S}/host/packed_partial.blend", "SimPacked"); m = o.modifiers[0]; R["packed_appended_bakes"] = bakes(m)
    R["packed_appended_bake_data_blocks"] = [len(b.data_blocks) for b in m.bakes]
    o3 = append(f"{S}/host/disk_partial.blend", "SimDisk"); m3 = o3.modifiers[0]; R["disk_appended_bakes"] = bakes(m3); R["disk_appended_mod_dir"] = m3.bake_directory
elif mode == "replica_boot":
    bpy.ops.wm.open_mainfile(filepath=f"{S}/replica/qcb_boot_777.blend", load_ui=False)
    m3 = bpy.data.objects["SimDisk"].modifiers[0]; R["boot_disk_mod_dir"] = m3.bake_directory; R["boot_disk_bakes"] = bakes(m3)
    R["boot_dir_it_would_use"] = bpy.path.abspath(m3.bake_directory) if m3.bake_directory else "(default: //blendcache_<blend>/…)"
    R["boot_blendcache_dirs_here"] = glob.glob(f"{S}/replica/blendcache_*")
print("PROBE_JSON " + json.dumps(R, indent=1))
