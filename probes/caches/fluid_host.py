import bpy, os, json, time
sc = bpy.context.scene; sc.frame_end = 12
bpy.ops.mesh.primitive_cube_add(size=4); dom = bpy.context.active_object; dom.name = "Domain"
m = dom.modifiers.new("Fluid", "FLUID"); m.fluid_type = "DOMAIN"
ds = m.domain_settings; ds.domain_type = "LIQUID"; ds.resolution_max = 24; ds.cache_type = "ALL"
ds.cache_directory = "$S/cache"; ds.cache_frame_end = 12
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.6, location=(0, 0, 1)); flow = bpy.context.active_object; flow.name = "Flow"
fm = flow.modifiers.new("Fluid", "FLUID"); fm.fluid_type = "FLOW"; fm.flow_settings.flow_type = "LIQUID"; fm.flow_settings.flow_behavior = "GEOMETRY"
bpy.ops.wm.save_as_mainfile(filepath="$S/host.blend")
t = time.perf_counter()
with bpy.context.temp_override(scene=sc, object=dom, active_object=dom, selected_objects=[dom]):
    r = bpy.ops.fluid.bake_all()
print("FLUID bake", r, f"{time.perf_counter()-t:.1f}s")
def nverts(o, f):
    sc.frame_set(f); ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get()); return len(ev.data.vertices)
print("FLUID host verts f1/f8", nverts(dom, 1), nverts(dom, 8), "cache files", sum(len(fs) for _, _, fs in os.walk("$S/cache")), "is_baked_data", ds.has_cache_baked_data)
bpy.data.libraries.write("$S/partial.blend", {dom}, compress=False)
bpy.ops.wm.save_as_mainfile(filepath="$S/renamed_copy.blend", copy=True, relative_remap=False)
