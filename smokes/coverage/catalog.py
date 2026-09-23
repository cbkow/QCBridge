"""The catalogue of user actions and what each should leave on the replica.

Every entry is (key, group, what a user did, act, probe). `act` runs on the
host and does the thing the way the UI would (RNA writes fire their update
callbacks; operators tag themselves). `probe` runs on BOTH sides and returns
a JSON-comparable value of the property that matters — the consumed one, not
a convenient proxy. The host records probe() right after act(); the replica
reports probe() continuously; equality within the settle window is "crossed".

`setup()` runs on the host before the session starts, so everything it makes
rides the bootstrap. Objects are prefixed Cov to stay out of the way.
"""
import bpy

ACTIONS: list = []  # [key, group, desc, act, probe] — probe filled by @probe


def action(key, group, desc):
    def deco(fn):
        ACTIONS.append([key, group, desc, fn, None])
        return fn
    return deco


def probe(fn):
    """Attach a probe to the action registered for `fn`."""
    def deco(p):
        for entry in ACTIONS:
            if entry[3] is fn:
                entry[4] = p
        return fn
    return deco


def r4(v):
    try:
        return [round(float(x), 4) for x in v]
    except TypeError:
        return round(float(v), 4)


def obj(name):
    return bpy.data.objects.get(name)


def ops_ctx(**extra):
    wm = bpy.context.window_manager
    win = wm.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return bpy.context.temp_override(window=win, area=area, region=region,
                                     scene=bpy.context.scene,
                                     view_layer=win.view_layer, **extra)


def obj_ctx(o, **extra):
    return ops_ctx(object=o, active_object=o, selected_objects=[o],
                   selected_editable_objects=[o], **extra)


def _fcurves(act):
    if act is None:
        return []
    try:
        fcs = list(act.fcurves)
        if fcs:
            return fcs
    except AttributeError:
        pass
    try:
        return [fc for layer in act.layers for strip in layer.strips
                for bag in strip.channelbags for fc in bag.fcurves]
    except AttributeError:
        return []


def _new_mesh_obj(name, verts=None, faces=None, link=True):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts or [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [],
                   faces or [(0, 1, 2, 3)])
    o = bpy.data.objects.new(name, me)
    if link:
        bpy.context.scene.collection.objects.link(o)
    return o


def _comp_tree(scene):
    t = getattr(scene, "compositing_node_group", None)
    if t is None:
        t = getattr(scene, "node_tree", None)
    return t


# ── setup: everything the bootstrap should carry ─────────────────────────────

def setup():
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = 1, 48
    for n in ("CovDel", "CovRen", "CovDup", "CovRelink", "CovMat", "CovMove",
              "CovVis", "CovAnim", "CovKeyed", "CovNla", "CovDrv", "CovRB",
              "CovPsys", "CovHook", "CovVParent", "CovUndo", "CovMesh",
              "CovMods", "CovLL", "CovDelta", "CovVisRay"):
        _new_mesh_obj(n)
    _new_mesh_obj("CovAltMesh", verts=[(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0), (1, 1, 1)],
                  faces=[(0, 1, 2, 3)], link=False)
    # materials
    m = bpy.data.materials.new("CovMatA")
    m.use_nodes = True
    obj("CovMat").data.materials.append(m)
    nt = m.node_tree
    math = nt.nodes.new("ShaderNodeMath"); math.name = "CovMath"; math.operation = "ADD"
    ramp = nt.nodes.new("ShaderNodeValToRGB"); ramp.name = "CovRamp"
    tex = nt.nodes.new("ShaderNodeTexImage"); tex.name = "CovTex"
    mix = nt.nodes.new("ShaderNodeMixRGB"); mix.name = "CovMix"
    # Wire them into the shader: a dangling node's property change does not
    # touch the evaluated material, so the depsgraph tells nobody (measured).
    bsdf = nt.nodes["Principled BSDF"]
    nt.links.new(tex.outputs["Color"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], mix.inputs["Color1"])
    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    bpy.data.materials.new("CovMatB")
    # image
    img = bpy.data.images.new("CovImg", 8, 8)
    img.generated_color = (0.5, 0.5, 0.5, 1)
    # light + second camera + empty
    lt = bpy.data.lights.new("CovLight", "POINT")
    lo = bpy.data.objects.new("CovLight", lt); sc.collection.objects.link(lo)
    cam = bpy.data.cameras.new("CovCam2")
    co = bpy.data.objects.new("CovCam2", cam); sc.collection.objects.link(co)
    em = bpy.data.objects.new("CovEmpty", None); sc.collection.objects.link(em)
    # collections
    c = bpy.data.collections.new("CovColl"); sc.collection.children.link(c)
    c.objects.link(_new_mesh_obj("CovCollObj", link=False))
    c2 = bpy.data.collections.new("CovColl2"); sc.collection.children.link(c2)
    src = bpy.data.collections.new("CovInstSrc"); sc.collection.children.link(src)
    src.objects.link(_new_mesh_obj("CovInstSrcObj", link=False))
    # animation baseline
    k = obj("CovKeyed")
    k.location.x = 0; k.keyframe_insert("location", frame=1)
    k.location.x = 5; k.keyframe_insert("location", frame=10)
    n = obj("CovNla")
    n.location.z = 0; n.keyframe_insert("location", frame=1)
    n.location.z = 2; n.keyframe_insert("location", frame=10)
    # second world
    w = bpy.data.worlds.new("CovWorldB")
    w.use_nodes = True
    # curve, text, metaball, lattice-free
    cu = bpy.data.curves.new("CovCurve", "CURVE"); cu.dimensions = "3D"
    sp = cu.splines.new("BEZIER"); sp.bezier_points.add(1)
    sp.bezier_points[0].co = (0, 0, 0); sp.bezier_points[1].co = (2, 0, 0)
    sc.collection.objects.link(bpy.data.objects.new("CovCurve", cu))
    tx = bpy.data.curves.new("CovText", "FONT"); tx.body = "before"
    sc.collection.objects.link(bpy.data.objects.new("CovText", tx))
    # modifier stack for reorder/remove
    mo = obj("CovMods")
    mo.modifiers.new("Sub", "SUBSURF"); mo.modifiers.new("Bev", "BEVEL")
    # a node group
    g = bpy.data.node_groups.new("CovGroup", "ShaderNodeTree")
    v = g.nodes.new("ShaderNodeValue"); v.name = "CovVal"; v.outputs[0].default_value = 0.1
    # Used by CovMatA: an unused group has no users, is not written by the
    # bootstrap save, and has no business on the replica until it is used.
    gn = nt.nodes.new("ShaderNodeGroup"); gn.name = "CovGroupNode"; gn.node_tree = g
    # vertex-parent target
    _new_mesh_obj("CovVParentTarget")
    # object to receive light linking
    _new_mesh_obj("CovLLTarget")
    # a keyed rig: scrubbing it must not resend the armature object per frame
    with ops_ctx():
        bpy.ops.object.armature_add(location=(0, 6, 0))
    rig = bpy.context.active_object; rig.name = "CovRig"
    pb = rig.pose.bones[0]
    pb.location = (0, 0, 0); pb.keyframe_insert("location", frame=1)
    pb.location = (0, 0, 2); pb.keyframe_insert("location", frame=20)
    sc.frame_set(1)


# ── objects ──────────────────────────────────────────────────────────────────

@action("obj_add", "object", "add a cube (operator)")
def obj_add():
    with ops_ctx():
        bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 3, 0))
    bpy.context.active_object.name = "CovAdded"
@probe(obj_add)
def _():
    o = obj("CovAdded"); return None if o is None else [r4(o.location), len(o.data.vertices)]


@action("obj_delete", "object", "delete an object")
def obj_delete():
    bpy.data.objects.remove(obj("CovDel"))
@probe(obj_delete)
def _(): return obj("CovDel") is None


@action("obj_rename", "object", "rename an object")
def obj_rename():
    obj("CovRen").name = "CovRenamed"
@probe(obj_rename)
def _(): return obj("CovRenamed") is not None and obj("CovRen") is None


@action("obj_duplicate", "object", "duplicate (operator)")
def obj_duplicate():
    with obj_ctx(obj("CovDup")):
        bpy.ops.object.duplicate(linked=False)
    bpy.context.active_object.name = "CovDupCopy"; bpy.context.active_object.location.y = 4
@probe(obj_duplicate)
def _():
    o = obj("CovDupCopy"); return None if o is None else [r4(o.location), o.data.name != (obj("CovDup").data.name if obj("CovDup") else "")]


@action("obj_data_relink", "object", "swap the mesh under an object")
def obj_data_relink():
    obj("CovRelink").data = bpy.data.meshes["CovAltMesh"]
@probe(obj_data_relink)
def _():
    o = obj("CovRelink"); return None if o is None else len(o.data.vertices)


@action("obj_delta_transform", "object", "delta_location")
def obj_delta():
    obj("CovDelta").delta_location = (0, 0, 1.5)
@probe(obj_delta)
def _():
    o = obj("CovDelta"); return None if o is None else r4(o.delta_location)


@action("obj_display_type", "object", "display as wire")
def obj_display_type():
    obj("CovVis").display_type = "WIRE"
@probe(obj_display_type)
def _():
    o = obj("CovVis"); return None if o is None else o.display_type


@action("obj_show_in_front", "object", "show in front")
def obj_sif():
    obj("CovVis").show_in_front = True
@probe(obj_sif)
def _():
    o = obj("CovVis"); return None if o is None else o.show_in_front


@action("obj_hide_eye", "visibility", "eye toggle (hide_set)")
def obj_hide_eye():
    obj("CovVis").hide_set(True)
@probe(obj_hide_eye)
def _():
    o = obj("CovVis")
    try: return None if o is None else o.hide_get()
    except RuntimeError: return "not-in-view-layer"


@action("obj_hide_viewport", "visibility", "hide_viewport (monitor icon)")
def obj_hv():
    obj("CovVisRay").hide_viewport = True
@probe(obj_hv)
def _():
    o = obj("CovVisRay"); return None if o is None else o.hide_viewport


@action("obj_hide_render", "visibility", "hide_render")
def obj_hr():
    obj("CovVisRay").hide_render = True
@probe(obj_hr)
def _():
    o = obj("CovVisRay"); return None if o is None else o.hide_render


@action("obj_ray_visibility", "visibility", "camera ray visibility off")
def obj_rayvis():
    obj("CovVisRay").visible_camera = False
@probe(obj_rayvis)
def _():
    o = obj("CovVisRay"); return None if o is None else o.visible_camera


@action("obj_holdout", "visibility", "holdout + shadow catcher")
def obj_holdout():
    o = obj("CovVisRay"); o.is_holdout = True; o.is_shadow_catcher = True
@probe(obj_holdout)
def _():
    o = obj("CovVisRay"); return None if o is None else [o.is_holdout, o.is_shadow_catcher]


@action("obj_color", "object", "object color")
def obj_color():
    obj("CovVis").color = (1, 0, 0, 1)
@probe(obj_color)
def _():
    o = obj("CovVis"); return None if o is None else r4(o.color)


@action("obj_empty_display", "object", "empty display type/size")
def obj_empty():
    e = obj("CovEmpty"); e.empty_display_type = "CUBE"; e.empty_display_size = 2.5
@probe(obj_empty)
def _():
    o = obj("CovEmpty"); return None if o is None else [o.empty_display_type, r4(o.empty_display_size)]


@action("obj_instance_collection", "object", "empty instancing a collection")
def obj_inst():
    e = obj("CovEmpty"); e.instance_type = "COLLECTION"; e.instance_collection = bpy.data.collections["CovInstSrc"]
@probe(obj_inst)
def _():
    o = obj("CovEmpty"); return None if o is None else [o.instance_type, o.instance_collection.name if o.instance_collection else None]


@action("obj_material_slot_assign", "material", "assign a different material to slot 0")
def obj_matslot():
    obj("CovMat").material_slots[0].material = bpy.data.materials["CovMatB"]
@probe(obj_matslot)
def _():
    o = obj("CovMat"); return None if o is None or not o.material_slots else (o.material_slots[0].material.name if o.material_slots[0].material else None)


@action("obj_vertex_parent", "object", "vertex parenting")
def obj_vparent():
    o = obj("CovVParent"); o.parent = obj("CovVParentTarget"); o.parent_type = "VERTEX"; o.parent_vertices = (1, 0, 0)
@probe(obj_vparent)
def _():
    o = obj("CovVParent"); return None if o is None else [o.parent.name if o.parent else None, o.parent_type, list(o.parent_vertices)]


@action("obj_light_linking", "object", "light linking receiver collection")
def obj_ll():
    lo = obj("CovLight"); lo.light_linking.receiver_collection = bpy.data.collections["CovColl"]
@probe(obj_ll)
def _():
    o = obj("CovLight"); return None if o is None else (o.light_linking.receiver_collection.name if o.light_linking.receiver_collection else None)


@action("obj_custom_prop_nested", "object", "custom prop that is a dict")
def obj_cp_nested():
    obj("CovVis")["CovNest"] = {"a": 1, "b": [1, 2]}
@probe(obj_cp_nested)
def _():
    o = obj("CovVis"); v = None if o is None else o.get("CovNest")
    return None if v is None else {"a": v["a"], "b": list(v["b"])}


# ── mesh data ────────────────────────────────────────────────────────────────

@action("mesh_vertex_move", "mesh", "move a vertex (edit-mode style data edit)")
def mesh_vmove():
    me = obj("CovMesh").data; me.vertices[0].co.z = 1.25; me.update()
@probe(mesh_vmove)
def _():
    o = obj("CovMesh"); return None if o is None else r4(o.data.vertices[0].co)


@action("mesh_vertex_group", "mesh", "vertex group with weights")
def mesh_vg():
    o = obj("CovMesh"); vg = o.vertex_groups.new(name="CovVG"); vg.add([0, 1], 0.75, "REPLACE")
@probe(mesh_vg)
def _():
    o = obj("CovMesh")
    if o is None or "CovVG" not in o.vertex_groups: return None
    try: return r4(o.vertex_groups["CovVG"].weight(0))
    except RuntimeError: return "no-weight"


@action("mesh_uv_layer", "mesh", "add a UV map")
def mesh_uv():
    obj("CovMesh").data.uv_layers.new(name="CovUV")
@probe(mesh_uv)
def _():
    o = obj("CovMesh"); return None if o is None else [u.name for u in o.data.uv_layers]


@action("mesh_color_attribute", "mesh", "add a color attribute")
def mesh_ca():
    obj("CovMesh").data.color_attributes.new("CovCol", "FLOAT_COLOR", "POINT")
@probe(mesh_ca)
def _():
    o = obj("CovMesh"); return None if o is None else [a.name for a in o.data.color_attributes]


@action("mesh_shade_smooth", "mesh", "shade smooth")
def mesh_smooth():
    me = obj("CovMesh").data
    for p in me.polygons: p.use_smooth = True
    me.update()
@probe(mesh_smooth)
def _():
    o = obj("CovMesh"); return None if o is None else [p.use_smooth for p in o.data.polygons]


@action("mesh_material_index", "mesh", "face material index")
def mesh_matidx():
    o = obj("CovMat"); o.data.materials.append(bpy.data.materials["CovMatA"])
    o.data.polygons[0].material_index = 1; o.data.update()
@probe(mesh_matidx)
def _():
    o = obj("CovMat"); return None if o is None else [o.data.polygons[0].material_index, len(o.data.materials)]


@action("mesh_custom_prop", "mesh", "custom prop on the mesh datablock")
def mesh_cp():
    obj("CovMesh").data["CovMeshProp"] = 7
@probe(mesh_cp)
def _():
    o = obj("CovMesh"); return None if o is None else o.data.get("CovMeshProp")


# ── modifiers ────────────────────────────────────────────────────────────────

@action("mod_add_subsurf", "modifier", "add subsurf, levels 2")
def mod_add():
    m = obj("CovMesh").modifiers.new("CovSub", "SUBSURF"); m.levels = 2
@probe(mod_add)
def _():
    o = obj("CovMesh"); m = None if o is None else o.modifiers.get("CovSub"); return None if m is None else m.levels


@action("mod_setting_change", "modifier", "change a modifier setting (levels 3)")
def mod_set():
    obj("CovMesh").modifiers["CovSub"].levels = 3
@probe(mod_set)
def _():
    o = obj("CovMesh"); m = None if o is None else o.modifiers.get("CovSub"); return None if m is None else m.levels


@action("mod_reorder", "modifier", "move a modifier down the stack")
def mod_reorder():
    obj("CovMods").modifiers.move(0, 1)
@probe(mod_reorder)
def _():
    o = obj("CovMods"); return None if o is None else [m.name for m in o.modifiers]


@action("mod_remove", "modifier", "remove a modifier")
def mod_remove():
    o = obj("CovMods"); o.modifiers.remove(o.modifiers["Bev"])
@probe(mod_remove)
def _():
    o = obj("CovMods"); return None if o is None else [m.name for m in o.modifiers]


@action("mod_toggle_viewport", "modifier", "modifier viewport toggle off")
def mod_tv():
    obj("CovMods").modifiers["Sub"].show_viewport = False
@probe(mod_tv)
def _():
    o = obj("CovMods"); m = None if o is None else o.modifiers.get("Sub"); return None if m is None else m.show_viewport


@action("mod_toggle_render", "modifier", "modifier render toggle off")
def mod_tr():
    obj("CovMods").modifiers["Sub"].show_render = False
@probe(mod_tr)
def _():
    o = obj("CovMods"); m = None if o is None else o.modifiers.get("Sub"); return None if m is None else m.show_render


@action("mod_hook_object", "modifier", "hook modifier pointing at an empty")
def mod_hook():
    m = obj("CovHook").modifiers.new("CovHookMod", "HOOK"); m.object = obj("CovEmpty")
@probe(mod_hook)
def _():
    o = obj("CovHook"); m = None if o is None else o.modifiers.get("CovHookMod"); return None if m is None else (m.object.name if m.object else None)


@action("mod_displace_texture", "modifier", "displace modifier with a legacy texture")
def mod_disp():
    t = bpy.data.textures.new("CovTexClouds", "CLOUDS"); t.noise_scale = 0.7
    m = obj("CovHook").modifiers.new("CovDisp", "DISPLACE"); m.texture = t; m.strength = 0.3
@probe(mod_disp)
def _():
    o = obj("CovHook"); m = None if o is None else o.modifiers.get("CovDisp")
    return None if m is None else [m.texture.name if m.texture else None, r4(m.texture.noise_scale) if m.texture else None]


@action("mod_texture_setting", "modifier", "change the legacy texture's setting")
def mod_texset():
    bpy.data.textures["CovTexClouds"].noise_scale = 1.9
@probe(mod_texset)
def _():
    t = bpy.data.textures.get("CovTexClouds"); return None if t is None else r4(t.noise_scale)


# ── materials and nodes ──────────────────────────────────────────────────────

@action("mat_new_assign", "material", "new material appended to a slot")
def mat_new():
    m = bpy.data.materials.new("CovMatNew"); m.use_nodes = True
    obj("CovMove").data.materials.append(m)
@probe(mat_new)
def _():
    o = obj("CovMove"); return None if o is None else [s.material.name if s.material else None for s in o.material_slots]


@action("mat_socket_default", "material", "Metallic socket value")
def mat_sock():
    nt = bpy.data.materials["CovMatA"].node_tree
    nt.nodes["Principled BSDF"].inputs["Metallic"].default_value = 0.65
@probe(mat_sock)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else r4(m.node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value)


@action("mat_link_new", "material", "link math → base color")
def mat_link():
    nt = bpy.data.materials["CovMatA"].node_tree
    nt.links.new(nt.nodes["CovMath"].outputs[0], nt.nodes["Principled BSDF"].inputs["Roughness"])
@probe(mat_link)
def _():
    m = bpy.data.materials.get("CovMatA")
    if m is None: return None
    s = m.node_tree.nodes["Principled BSDF"].inputs["Roughness"]
    return s.links[0].from_node.name if s.is_linked else None


@action("mat_node_property", "material", "Math node operation ADD → MULTIPLY")
def mat_nodeprop():
    bpy.data.materials["CovMatA"].node_tree.nodes["CovMath"].operation = "MULTIPLY"
@probe(mat_nodeprop)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else m.node_tree.nodes["CovMath"].operation


@action("mat_node_mute", "material", "mute a node")
def mat_mute():
    bpy.data.materials["CovMatA"].node_tree.nodes["CovMix"].mute = True
@probe(mat_mute)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else m.node_tree.nodes["CovMix"].mute


@action("mat_colorramp_stop", "material", "move a ColorRamp stop")
def mat_ramp():
    bpy.data.materials["CovMatA"].node_tree.nodes["CovRamp"].color_ramp.elements[0].position = 0.35
@probe(mat_ramp)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else r4(m.node_tree.nodes["CovRamp"].color_ramp.elements[0].position)


@action("mat_image_assign", "material", "assign an image to an Image Texture node")
def mat_img():
    bpy.data.materials["CovMatA"].node_tree.nodes["CovTex"].image = bpy.data.images["CovImg"]
@probe(mat_img)
def _():
    m = bpy.data.materials.get("CovMatA"); n = None if m is None else m.node_tree.nodes["CovTex"]
    return None if n is None else (n.image.name if n.image else None)


@action("mat_node_add", "material", "add a node")
def mat_nodeadd():
    nt = bpy.data.materials["CovMatA"].node_tree
    n = nt.nodes.new("ShaderNodeTexNoise"); n.name = "CovNoise"
    nt.update_tag()  # the node editor tags the tree on add; nodes.new() alone does not
@probe(mat_nodeadd)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else ("CovNoise" in m.node_tree.nodes)


@action("mat_settings", "material", "render method / backface culling / displacement")
def mat_settings():
    m = bpy.data.materials["CovMatA"]
    m.surface_render_method = "BLENDED"; m.use_backface_culling = True; m.displacement_method = "BOTH"
@probe(mat_settings)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else [m.surface_render_method, m.use_backface_culling, m.displacement_method]


@action("mat_viewport_color", "material", "viewport display colour")
def mat_vcol():
    bpy.data.materials["CovMatA"].diffuse_color = (0.1, 0.9, 0.1, 1)
@probe(mat_vcol)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else r4(m.diffuse_color)


@action("mat_custom_prop", "material", "custom prop on a material")
def mat_cp():
    bpy.data.materials["CovMatA"]["CovMatProp"] = 3
@probe(mat_cp)
def _():
    m = bpy.data.materials.get("CovMatA"); return None if m is None else m.get("CovMatProp")


@action("nodegroup_value_edit", "material", "edit a value inside a node group")
def ng_edit():
    bpy.data.node_groups["CovGroup"].nodes["CovVal"].outputs[0].default_value = 0.8
@probe(ng_edit)
def _():
    g = bpy.data.node_groups.get("CovGroup"); return None if g is None else r4(g.nodes["CovVal"].outputs[0].default_value)


@action("nodegroup_node_property", "material", "node property inside a group (Value → Math op)")
def ng_prop():
    g = bpy.data.node_groups["CovGroup"]; n = g.nodes.new("ShaderNodeMath"); n.name = "CovGMath"; n.operation = "SINE"
@probe(ng_prop)
def _():
    g = bpy.data.node_groups.get("CovGroup"); n = None if g is None else g.nodes.get("CovGMath"); return None if n is None else n.operation


# ── images ───────────────────────────────────────────────────────────────────

@action("image_new_generated", "image", "new generated image")
def img_new():
    bpy.data.images.new("CovImgNew", 4, 4)
@probe(img_new)
def _():
    i = bpy.data.images.get("CovImgNew"); return None if i is None else list(i.size)


@action("image_pixels_edit", "image", "paint: change a pixel")
def img_px():
    i = bpy.data.images["CovImg"]; px = list(i.pixels); px[0] = 1.0; px[1] = 0.0; i.pixels = px; i.update()
@probe(img_px)
def _():
    i = bpy.data.images.get("CovImg"); return None if i is None else r4(list(i.pixels)[:2])


@action("image_colorspace", "image", "image colorspace")
def img_cs():
    bpy.data.images["CovImg"].colorspace_settings.name = "Non-Color"
@probe(img_cs)
def _():
    i = bpy.data.images.get("CovImg"); return None if i is None else i.colorspace_settings.name


# ── lights ───────────────────────────────────────────────────────────────────

@action("light_energy", "light", "light power")
def light_energy():
    bpy.data.lights["CovLight"].energy = 250
@probe(light_energy)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else r4(l.energy)


@action("light_color", "light", "light colour")
def light_color():
    bpy.data.lights["CovLight"].color = (1, 0.5, 0.2)
@probe(light_color)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else r4(l.color)


@action("light_radius", "light", "point radius (shadow_soft_size)")
def light_radius():
    bpy.data.lights["CovLight"].shadow_soft_size = 0.9
@probe(light_radius)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else r4(l.shadow_soft_size)


@action("light_type_area", "light", "point → area, size and shape")
def light_type():
    bpy.data.lights["CovLight"].type = "AREA"
    l = bpy.data.lights["CovLight"]; l.shape = "DISK"; l.size = 3.0  # re-fetch: the type switch swaps the RNA class
@probe(light_type)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else [l.type, getattr(l, "shape", None), r4(getattr(l, "size", 0))]


@action("light_shadow_toggle", "light", "cast shadow off")
def light_shadow():
    bpy.data.lights["CovLight"].use_shadow = False
@probe(light_shadow)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else l.use_shadow


@action("light_spread", "light", "area spread")
def light_spread():
    bpy.data.lights["CovLight"].spread = 1.0
@probe(light_spread)
def _():
    l = bpy.data.lights.get("CovLight"); return None if l is None else r4(getattr(l, "spread", 0))


@action("light_add_sun", "light", "add a sun (operator)")
def light_add():
    with ops_ctx():
        bpy.ops.object.light_add(type="SUN", location=(0, 0, 6))
    bpy.context.active_object.name = "CovSun"; bpy.context.active_object.data.energy = 3.3
@probe(light_add)
def _():
    o = obj("CovSun"); return None if o is None else [o.data.type, r4(o.data.energy)]


# ── cameras ──────────────────────────────────────────────────────────────────

@action("cam_lens", "camera", "focal length")
def cam_lens():
    bpy.data.cameras["CovCam2"].lens = 85
@probe(cam_lens)
def _():
    c = bpy.data.cameras.get("CovCam2"); return None if c is None else r4(c.lens)


@action("cam_dof_focus_object", "camera", "DoF focus object")
def cam_dof():
    c = bpy.data.cameras["CovCam2"]; c.dof.use_dof = True; c.dof.focus_object = obj("CovEmpty")
@probe(cam_dof)
def _():
    c = bpy.data.cameras.get("CovCam2"); return None if c is None else [c.dof.use_dof, c.dof.focus_object.name if c.dof.focus_object else None]


@action("cam_sensor", "camera", "sensor fit + height")
def cam_sensor():
    c = bpy.data.cameras["CovCam2"]; c.sensor_fit = "VERTICAL"; c.sensor_height = 30
@probe(cam_sensor)
def _():
    c = bpy.data.cameras.get("CovCam2"); return None if c is None else [c.sensor_fit, r4(c.sensor_height)]


@action("cam_ortho", "camera", "orthographic + scale")
def cam_ortho():
    c = bpy.data.cameras["CovCam2"]; c.type = "ORTHO"; c.ortho_scale = 12
@probe(cam_ortho)
def _():
    c = bpy.data.cameras.get("CovCam2"); return None if c is None else [c.type, r4(c.ortho_scale)]


@action("cam_background_image", "camera", "camera background image")
def cam_bg():
    c = bpy.data.cameras["CovCam2"]; bg = c.background_images.new(); bg.image = bpy.data.images["CovImg"]; c.show_background_images = True
@probe(cam_bg)
def _():
    c = bpy.data.cameras.get("CovCam2"); return None if c is None else [len(c.background_images), c.background_images[0].image.name if c.background_images and c.background_images[0].image else None]


@action("scene_camera_switch", "camera", "make CovCam2 the scene camera")
def scene_cam():
    bpy.context.scene.camera = obj("CovCam2")
@probe(scene_cam)
def _():
    c = bpy.context.scene.camera; return None if c is None else c.name


@action("marker_camera_bind", "camera", "timeline marker bound to a camera")
def marker_cam():
    m = bpy.context.scene.timeline_markers.new("CovMarker", frame=5); m.camera = obj("Camera")
@probe(marker_cam)
def _():
    m = bpy.context.scene.timeline_markers.get("CovMarker"); return None if m is None else [m.frame, m.camera.name if m.camera else None]


# ── world ────────────────────────────────────────────────────────────────────

@action("world_node_socket", "world", "background strength")
def world_sock():
    bpy.context.scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 2.5
@probe(world_sock)
def _():
    w = bpy.context.scene.world; return None if w is None or not w.use_nodes else r4(w.node_tree.nodes["Background"].inputs["Strength"].default_value)


@action("world_swap", "world", "scene.world → another world")
def world_swap():
    bpy.context.scene.world = bpy.data.worlds["CovWorldB"]
@probe(world_swap)
def _():
    w = bpy.context.scene.world; return None if w is None else w.name


@action("world_color_no_nodes", "world", "world colour with nodes off")
def world_color():
    w = bpy.context.scene.world; w.use_nodes = False; w.color = (0.2, 0.3, 0.4)
@probe(world_color)
def _():
    w = bpy.context.scene.world; return None if w is None else [w.use_nodes, r4(w.color)]


# ── scene ────────────────────────────────────────────────────────────────────

@action("scene_frame_range", "scene", "frame start/end")
def scene_range():
    s = bpy.context.scene; s.frame_start = 5; s.frame_end = 60
@probe(scene_range)
def _():
    s = bpy.context.scene; return [s.frame_start, s.frame_end]


@action("scene_fps", "scene", "frame rate")
def scene_fps():
    bpy.context.scene.render.fps = 25
@probe(scene_fps)
def _(): return bpy.context.scene.render.fps


@action("scene_resolution", "scene", "render resolution")
def scene_res():
    r = bpy.context.scene.render; r.resolution_x = 1280; r.resolution_y = 544
@probe(scene_res)
def _():
    r = bpy.context.scene.render; return [r.resolution_x, r.resolution_y]


@action("scene_view_transform", "scene", "view transform + look")
def scene_vt():
    v = bpy.context.scene.view_settings; v.view_transform = "Standard"; v.exposure = 0.5
@probe(scene_vt)
def _():
    v = bpy.context.scene.view_settings; return [v.view_transform, r4(v.exposure)]


@action("scene_engine", "scene", "render engine → Cycles")
def scene_engine():
    bpy.context.scene.render.engine = "CYCLES"
@probe(scene_engine)
def _(): return bpy.context.scene.render.engine


@action("scene_cycles_samples", "scene", "Cycles render samples (not preview)")
def scene_samples():
    bpy.context.scene.cycles.samples = 77
@probe(scene_samples)
def _(): return bpy.context.scene.cycles.samples


@action("scene_film_transparent", "scene", "film transparent")
def scene_film():
    bpy.context.scene.render.film_transparent = True
@probe(scene_film)
def _(): return bpy.context.scene.render.film_transparent


@action("scene_motion_blur", "scene", "motion blur on")
def scene_mb():
    bpy.context.scene.render.use_motion_blur = True
@probe(scene_mb)
def _(): return bpy.context.scene.render.use_motion_blur


@action("scene_units", "scene", "unit system + scale")
def scene_units():
    u = bpy.context.scene.unit_settings; u.system = "IMPERIAL"; u.scale_length = 0.5
@probe(scene_units)
def _():
    u = bpy.context.scene.unit_settings; return [u.system, r4(u.scale_length)]


@action("scene_gravity", "scene", "gravity")
def scene_gravity():
    bpy.context.scene.gravity = (0, 0, -3)
@probe(scene_gravity)
def _(): return r4(bpy.context.scene.gravity)


@action("scene_border", "scene", "render region")
def scene_border():
    r = bpy.context.scene.render; r.use_border = True; r.border_min_x = 0.25
@probe(scene_border)
def _():
    r = bpy.context.scene.render; return [r.use_border, r4(r.border_min_x)]


@action("scene_custom_prop", "scene", "custom prop on the scene")
def scene_cp():
    bpy.context.scene["CovSceneProp"] = 11
@probe(scene_cp)
def _(): return bpy.context.scene.get("CovSceneProp")


@action("scene_tracked_edit_plus_scene_keyframe", "scene", "resolution % edit in the same window as a Scene keyframe (structural)")
def scene_a2():
    s = bpy.context.scene
    s.render.resolution_percentage = 33          # tracked tier-1 path
    s.keyframe_insert("gravity", frame=3)        # flips ~anim on the Scene → tier-2 → unsupported
@probe(scene_a2)
def _(): return bpy.context.scene.render.resolution_percentage


@action("scene_tracked_edit_after", "scene", "the same tracked edit alone, afterwards")
def scene_a2b():
    bpy.context.scene.render.resolution_percentage = 44
@probe(scene_a2b)
def _(): return bpy.context.scene.render.resolution_percentage


@action("viewlayer_add", "scene", "add a view layer")
def vl_add():
    bpy.context.scene.view_layers.new("CovVL")
@probe(vl_add)
def _(): return [v.name for v in bpy.context.scene.view_layers]


@action("viewlayer_pass", "scene", "enable a render pass")
def vl_pass():
    bpy.context.view_layer.use_pass_z = True
@probe(vl_pass)
def _(): return bpy.context.scene.view_layers[0].use_pass_z


@action("compositor_node_add", "scene", "add a compositor node")
def comp_add():
    s = bpy.context.scene
    t = _comp_tree(s)
    if t is None:
        s.use_nodes = True; t = _comp_tree(s)
    if t is None:
        g = bpy.data.node_groups.new("CovComp", "CompositorNodeTree"); s.compositing_node_group = g; t = g
    n = t.nodes.new("CompositorNodeBlur"); n.name = "CovBlur"
@probe(comp_add)
def _():
    t = _comp_tree(bpy.context.scene); return None if t is None else ("CovBlur" in t.nodes)


# ── collections ──────────────────────────────────────────────────────────────

@action("coll_new_and_link", "collection", "new collection, object moved into it")
def coll_new():
    c = bpy.data.collections.new("CovCollNew"); bpy.context.scene.collection.children.link(c)
    o = obj("CovMove"); c.objects.link(o); bpy.context.scene.collection.objects.unlink(o)
@probe(coll_new)
def _():
    c = bpy.data.collections.get("CovCollNew"); o = obj("CovMove")
    return None if c is None or o is None else [o.name in c.objects, o.name in bpy.context.scene.collection.objects]


@action("coll_exclude", "collection", "exclude a collection from the view layer")
def coll_exclude():
    bpy.context.view_layer.layer_collection.children["CovColl"].exclude = True
@probe(coll_exclude)
def _():
    lc = bpy.context.view_layer.layer_collection.children.get("CovColl"); return None if lc is None else lc.exclude


@action("coll_hide_viewport_layer", "collection", "collection eye (layer)")
def coll_hide_layer():
    bpy.context.view_layer.layer_collection.children["CovColl2"].hide_viewport = True
@probe(coll_hide_layer)
def _():
    lc = bpy.context.view_layer.layer_collection.children.get("CovColl2"); return None if lc is None else lc.hide_viewport


@action("coll_hide_render_data", "collection", "collection hide_render (data)")
def coll_hr():
    bpy.data.collections["CovColl2"].hide_render = True
@probe(coll_hr)
def _():
    c = bpy.data.collections.get("CovColl2"); return None if c is None else c.hide_render


@action("coll_instance_offset", "collection", "instance offset")
def coll_off():
    bpy.data.collections["CovInstSrc"].instance_offset = (1, 2, 3)
@probe(coll_off)
def _():
    c = bpy.data.collections.get("CovInstSrc"); return None if c is None else r4(c.instance_offset)


@action("coll_custom_prop", "collection", "custom prop on a collection")
def coll_cp():
    bpy.data.collections["CovColl"]["CovCollProp"] = 5
@probe(coll_cp)
def _():
    c = bpy.data.collections.get("CovColl"); return None if c is None else c.get("CovCollProp")


@action("coll_move_between", "collection", "move an object between two collections")
def coll_move():
    o = obj("CovCollObj"); bpy.data.collections["CovColl2"].objects.link(o); bpy.data.collections["CovColl"].objects.unlink(o)
@probe(coll_move)
def _():
    o = obj("CovCollObj"); return None if o is None else sorted(c.name for c in o.users_collection)


# ── animation ────────────────────────────────────────────────────────────────

@action("anim_keyframe_insert", "animation", "keyframe a fresh object")
def anim_key():
    o = obj("CovAnim"); o.location.z = 0; o.keyframe_insert("location", frame=1); o.location.z = 4; o.keyframe_insert("location", frame=20)
@probe(anim_key)
def _():
    o = obj("CovAnim"); ad = None if o is None else o.animation_data
    fcs = [fc for fc in _fcurves(ad.action if ad else None) if fc.data_path == "location" and fc.array_index == 2]
    return None if not fcs else r4(fcs[0].evaluate(20))


@action("anim_keyframe_move", "animation", "move an existing key (10 → 30)")
def anim_move():
    o = obj("CovKeyed"); fc = [f for f in _fcurves(o.animation_data.action) if f.array_index == 0][0]
    fc.keyframe_points[1].co.x = 30; fc.update()
@probe(anim_move)
def _():
    o = obj("CovKeyed"); ad = None if o is None else o.animation_data
    fcs = [fc for fc in _fcurves(ad.action if ad else None) if fc.array_index == 0]
    return None if not fcs else r4(fcs[0].keyframe_points[1].co.x)


@action("anim_interpolation", "animation", "key interpolation → CONSTANT")
def anim_interp():
    o = obj("CovKeyed"); fc = [f for f in _fcurves(o.animation_data.action) if f.array_index == 0][0]
    fc.keyframe_points[0].interpolation = "CONSTANT"
@probe(anim_interp)
def _():
    o = obj("CovKeyed"); ad = None if o is None else o.animation_data
    fcs = [fc for fc in _fcurves(ad.action if ad else None) if fc.array_index == 0]
    return None if not fcs else fcs[0].keyframe_points[0].interpolation


@action("anim_action_swap", "animation", "assign a different action")
def anim_swap():
    o = obj("CovKeyed"); a = bpy.data.actions.new("CovActB"); o.animation_data.action = a
    o.location.y = 1; o.keyframe_insert("location", frame=1); o.location.y = 9; o.keyframe_insert("location", frame=10)
@probe(anim_swap)
def _():
    o = obj("CovKeyed"); ad = None if o is None else o.animation_data
    return None if ad is None or ad.action is None else [ad.action.name, len(_fcurves(ad.action))]


@action("anim_nla_strip", "animation", "push action to an NLA strip")
def anim_nla():
    o = obj("CovNla"); ad = o.animation_data; act = ad.action
    t = ad.nla_tracks.new(); t.name = "CovTrack"; t.strips.new("CovStrip", 1, act); ad.action = None
@probe(anim_nla)
def _():
    o = obj("CovNla"); ad = None if o is None else o.animation_data
    return None if ad is None else [[t.name, [s.name for s in t.strips]] for t in ad.nla_tracks]


@action("anim_nla_mute", "animation", "mute an NLA track")
def anim_nla_mute():
    obj("CovNla").animation_data.nla_tracks["CovTrack"].mute = True
@probe(anim_nla_mute)
def _():
    o = obj("CovNla"); ad = None if o is None else o.animation_data; t = None if ad is None else ad.nla_tracks.get("CovTrack")
    return None if t is None else t.mute


@action("anim_driver_add", "animation", "driver on location.z")
def anim_drv():
    d = obj("CovDrv").driver_add("location", 2).driver; d.type = "SCRIPTED"; d.expression = "frame / 10"
@probe(anim_drv)
def _():
    o = obj("CovDrv"); ad = None if o is None else o.animation_data
    return None if ad is None or not ad.drivers else ad.drivers[0].driver.expression


@action("anim_driver_edit", "animation", "edit the driver expression")
def anim_drv_edit():
    obj("CovDrv").animation_data.drivers[0].driver.expression = "frame / 4"
@probe(anim_drv_edit)
def _():
    o = obj("CovDrv"); ad = None if o is None else o.animation_data
    return None if ad is None or not ad.drivers else ad.drivers[0].driver.expression


@action("anim_rig_scrub", "animation", "scrub 12 frames of a keyed rig (D1: no rig blob per frame)")
def anim_rig_scrub():
    for f in range(1, 13):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
@probe(anim_rig_scrub)
def _():
    o = obj("CovRig")
    if o is None or bpy.context.scene.frame_current != 12:
        return None
    return r4(o.pose.bones[0].location)


# ── physics ──────────────────────────────────────────────────────────────────

@action("phys_rigid_body", "physics", "add rigid body (operator)")
def phys_rb():
    o = obj("CovRB")
    with obj_ctx(o):
        bpy.ops.rigidbody.object_add(type="ACTIVE")
    o.rigid_body.mass = 4.5
@probe(phys_rb)
def _():
    o = obj("CovRB"); return None if o is None or o.rigid_body is None else [o.rigid_body.type, r4(o.rigid_body.mass)]


@action("phys_rigid_body_world", "physics", "rigid body world settings")
def phys_rbw():
    bpy.context.scene.rigidbody_world.substeps_per_frame = 22
@probe(phys_rbw)
def _():
    w = bpy.context.scene.rigidbody_world; return None if w is None else w.substeps_per_frame


@action("phys_force_field", "physics", "force field on an empty")
def phys_ff():
    e = obj("CovEmpty")
    with obj_ctx(e):
        bpy.ops.object.forcefield_toggle()
    e.field.type = "WIND"; e.field.strength = 7
@probe(phys_ff)
def _():
    o = obj("CovEmpty"); return None if o is None or o.field is None else [o.field.type, r4(o.field.strength)]


@action("phys_particles_add", "physics", "particle system")
def phys_ps():
    o = obj("CovPsys"); o.modifiers.new("CovPS", "PARTICLE_SYSTEM"); o.particle_systems[0].settings.count = 123
@probe(phys_ps)
def _():
    o = obj("CovPsys"); return None if o is None or not o.particle_systems else o.particle_systems[0].settings.count


@action("phys_particles_setting", "physics", "particle settings change (count)")
def phys_ps_set():
    obj("CovPsys").particle_systems[0].settings.count = 321
@probe(phys_ps_set)
def _():
    o = obj("CovPsys"); return None if o is None or not o.particle_systems else o.particle_systems[0].settings.count


@action("phys_cloth_setting", "physics", "cloth mass")
def phys_cloth():
    m = obj("CovPsys").modifiers.new("CovCloth", "CLOTH"); m.settings.mass = 1.7
@probe(phys_cloth)
def _():
    o = obj("CovPsys"); m = None if o is None else o.modifiers.get("CovCloth"); return None if m is None else r4(m.settings.mass)


@action("phys_fluid_domain", "physics", "fluid domain")
def phys_fluid():
    m = obj("CovRB").modifiers.new("CovFluid", "FLUID"); m.fluid_type = "DOMAIN"; m.domain_settings.resolution_max = 48
@probe(phys_fluid)
def _():
    o = obj("CovRB"); m = None if o is None else o.modifiers.get("CovFluid")
    return None if m is None else [m.fluid_type, m.domain_settings.resolution_max if m.domain_settings else None]


# ── other object types ───────────────────────────────────────────────────────

@action("curve_bevel", "othertypes", "curve bevel depth")
def curve_bevel():
    bpy.data.curves["CovCurve"].bevel_depth = 0.2
@probe(curve_bevel)
def _():
    c = bpy.data.curves.get("CovCurve"); return None if c is None else r4(c.bevel_depth)


@action("curve_point_move", "othertypes", "move a bezier point")
def curve_point():
    bpy.data.curves["CovCurve"].splines[0].bezier_points[1].co = (2, 3, 1)
@probe(curve_point)
def _():
    c = bpy.data.curves.get("CovCurve"); return None if c is None else r4(c.splines[0].bezier_points[1].co)


@action("text_body", "othertypes", "text object body")
def text_body():
    bpy.data.curves["CovText"].body = "after"
@probe(text_body)
def _():
    c = bpy.data.curves.get("CovText"); return None if c is None else c.body


@action("metaball_add", "othertypes", "metaball object")
def mball_add():
    mb = bpy.data.metaballs.new("CovMball"); el = mb.elements.new(); el.radius = 1.7
    bpy.context.scene.collection.objects.link(bpy.data.objects.new("CovMball", mb))
@probe(mball_add)
def _():
    o = obj("CovMball"); return None if o is None else r4(o.data.elements[0].radius)


@action("grease_pencil_add", "othertypes", "grease pencil object with a layer")
def gp_add():
    with ops_ctx():
        bpy.ops.object.grease_pencil_add(type="STROKE", location=(0, -4, 0))
    o = bpy.context.active_object; o.name = "CovGP"; o.data.layers.new("CovLayer")
@probe(gp_add)
def _():
    o = obj("CovGP"); return None if o is None else [o.type, [l.name for l in o.data.layers]]


@action("metaball_edit_later", "othertypes", "edit the metaball after it exists")
def mball_edit():
    bpy.data.metaballs["CovMball"].elements[0].radius = 0.6
@probe(mball_edit)
def _():
    o = obj("CovMball"); return None if o is None else r4(o.data.elements[0].radius)


@action("gp_layer_later", "othertypes", "add a grease pencil layer after it exists")
def gp_layer():
    gp = obj("CovGP").data; gp.layers.new("CovLayer2")
    gp.update_tag()  # the GP editor tags on layer add; layers.new() alone does not
@probe(gp_layer)
def _():
    o = obj("CovGP"); return None if o is None else [l.name for l in o.data.layers]


@action("volume_add", "othertypes", "volume object")
def vol_add():
    v = bpy.data.volumes.new("CovVol"); bpy.context.scene.collection.objects.link(bpy.data.objects.new("CovVol", v))
@probe(vol_add)
def _():
    o = obj("CovVol"); return None if o is None else o.type


@action("hair_curves_add", "othertypes", "hair curves object")
def hair_add():
    h = bpy.data.hair_curves.new("CovHair"); bpy.context.scene.collection.objects.link(bpy.data.objects.new("CovHair", h))
@probe(hair_add)
def _():
    o = obj("CovHair"); return None if o is None else o.type


@action("pointcloud_add", "othertypes", "point cloud object")
def pc_add():
    p = bpy.data.pointclouds.new("CovPC"); bpy.context.scene.collection.objects.link(bpy.data.objects.new("CovPC", p))
@probe(pc_add)
def _():
    o = obj("CovPC"); return None if o is None else o.type


@action("lightprobe_add", "othertypes", "light probe")
def probe_add():
    lp = bpy.data.lightprobes.new("CovProbe", "SPHERE"); lp.influence_distance = 4.2
    bpy.context.scene.collection.objects.link(bpy.data.objects.new("CovProbe", lp))
@probe(probe_add)
def _():
    o = obj("CovProbe"); return None if o is None else r4(o.data.influence_distance)


@action("lightprobe_edit_later", "othertypes", "edit the light probe after it exists")
def probe_edit():
    bpy.data.lightprobes["CovProbe"].influence_distance = 1.1
@probe(probe_edit)
def _():
    o = obj("CovProbe"); return None if o is None else r4(o.data.influence_distance)


@action("empty_image", "othertypes", "image empty")
def empty_img():
    e = bpy.data.objects.new("CovImgEmpty", None); bpy.context.scene.collection.objects.link(e)
    e.empty_display_type = "IMAGE"; e.data = bpy.data.images["CovImg"]
@probe(empty_img)
def _():
    o = obj("CovImgEmpty"); return None if o is None else [o.empty_display_type, o.data.name if o.data else None]


# ── files, undo ──────────────────────────────────────────────────────────────

@action("library_link", "files", "link an object from another .blend")
def lib_link():
    import os, tempfile
    path = os.path.join(tempfile.gettempdir(), "qcb-cov-lib.blend")
    src = _new_mesh_obj("CovLinked", link=False)
    bpy.data.libraries.write(path, {src}, fake_user=True)
    bpy.data.objects.remove(src)
    with bpy.data.libraries.load(path, link=True) as (f, t):
        t.objects = ["CovLinked"]
    bpy.context.scene.collection.objects.link(t.objects[0])
@probe(lib_link)
def _():
    o = obj("CovLinked"); return None if o is None else o.library is not None


def _undo_enabled():
    import os
    return bool(os.environ.get("QCB_COV_UNDO"))


# ed.undo() segfaults a background Blender and rewinds the whole session in
# a GUI one, so it runs only when asked for, and last.
@action("undo_after_move", "files", "move then Ctrl-Z (QCB_COV_UNDO=1 only)")
def undo_move():
    if not _undo_enabled():
        raise RuntimeError("skipped: set QCB_COV_UNDO=1")
    o = obj("CovUndo")
    with ops_ctx():
        bpy.ops.ed.undo_push(message="cov")
    o.location.x = 8; bpy.context.view_layer.update()
    with ops_ctx():
        bpy.ops.ed.undo_push(message="cov-move")
        bpy.ops.ed.undo()
@probe(undo_move)
def _():
    o = obj("CovUndo"); return None if o is None else r4(o.location.x)
