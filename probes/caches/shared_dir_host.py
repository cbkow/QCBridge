import bpy, os
bpy.ops.mesh.primitive_plane_add(size=10); g=bpy.context.active_object; g.modifiers.new("Collision","COLLISION")
bpy.ops.mesh.primitive_grid_add(x_subdivisions=12,y_subdivisions=12,size=2,location=(0,0,3)); s=bpy.context.active_object; s.name="Sheet"
c=s.modifiers.new("Cloth","CLOTH"); pc=c.point_cache; pc.frame_start=1; pc.frame_end=24
bpy.context.scene.frame_end=24
bpy.ops.wm.save_as_mainfile(filepath="$S/host.blend")
pc.use_disk_cache=True; pc.use_external=True; pc.filepath="$S/ext"
with bpy.context.temp_override(scene=bpy.context.scene, active_object=s, point_cache=pc): bpy.ops.ptcache.bake(bake=True)
bpy.context.scene.frame_set(20); deps=bpy.context.evaluated_depsgraph_get(); me=s.evaluated_get(deps).to_mesh()
print("HOST baked", pc.is_baked, "files", len(os.listdir("$S/ext")), "z20", round(sum((s.matrix_world@v.co).z for v in me.vertices)/len(me.vertices),6))
bpy.ops.wm.save_as_mainfile(filepath="$S/host_after.blend", copy=True)
