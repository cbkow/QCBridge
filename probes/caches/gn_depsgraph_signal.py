import bpy, json, os
P = os.environ["P"]; R = {}
bpy.ops.wm.open_mainfile(filepath=f"{P}/gn/host/scene.blend", load_ui=False)
sc = bpy.context.scene; o = bpy.data.objects["SimUnbaked"]
with bpy.context.temp_override(scene=sc, active_object=o, object=o, selected_objects=[o]):
    bpy.ops.object.simulation_nodes_cache_delete(selected=True)   # make sure it is unbaked
log = []
def h(scene, depsgraph): log.append([(u.id.name, u.is_updated_geometry, u.is_updated_transform) for u in depsgraph.updates])
bpy.app.handlers.depsgraph_update_post.append(h)
bpy.context.view_layer.update(); log.clear()
for f in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 5, 1): sc.frame_set(f)
R["updates_during_12_frame_sets_on_unbaked_zone"] = len(log)
log.clear()
o.location.x += 0.001; bpy.context.view_layer.update()
R["updates_from_a_transform_edit"] = log[:2]; log.clear()
with bpy.context.temp_override(scene=sc, active_object=o, object=o, selected_objects=[o]):
    bpy.ops.object.simulation_nodes_cache_bake(selected=True)
R["updates_from_bake"] = log[:3]; log.clear()
with bpy.context.temp_override(scene=sc, active_object=o, object=o, selected_objects=[o]):
    bpy.ops.object.simulation_nodes_cache_delete(selected=True)
R["updates_from_bake_delete"] = log[:3]; log.clear()
# and a legacy point-cache bake for comparison
c = bpy.data.objects.new("Cmp", bpy.data.meshes.new("m")); sc.collection.objects.link(c)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=1); g = bpy.context.active_object; g.name = "PcCloth"
m = g.modifiers.new("Cloth", "CLOTH"); m.point_cache.frame_end = 6; bpy.context.view_layer.update(); log.clear()
with bpy.context.temp_override(scene=sc, active_object=g, point_cache=m.point_cache):
    bpy.ops.ptcache.bake(bake=True)
R["updates_from_ptcache_bake"] = [len(x) for x in log][:5]; R["ptcache_bake_named_object"] = any(any(n == "PcCloth" for n, *_ in x) for x in log)
bpy.app.handlers.depsgraph_update_post.remove(h)
print("PROBE_JSON " + json.dumps(R, indent=1))
