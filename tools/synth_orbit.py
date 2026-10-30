"""Render a synthetic hand-held-style orbit video for end-to-end testing.

    blender -b -P tools/synth_orbit.py -- OUT_DIR [--frames 180] [--res 960x540]
    ffmpeg -framerate 30 -i OUT_DIR/%04d.png -pix_fmt yuv420p orbit.mp4

The subject is a textured Suzanne on a pedestal standing on a textured floor with
clutter around it, so both silhouette carving and floater removal are exercised.
The camera speed varies and pauses for a while, like a real operator walking round.
"""

import math
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
out_dir = argv[0] if argv else "/tmp/orbit"
n_frames = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else 180
res = argv[argv.index("--res") + 1] if "--res" in argv else "960x540"
res_x, res_y = map(int, res.split("x"))

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def textured_material(name, scale, color_a, color_b, kind="noise"):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"] if "Principled BSDF" in nt.nodes else nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexNoise" if kind == "noise" else "ShaderNodeTexVoronoi")
    tex.inputs["Scale"].default_value = scale
    if kind == "noise":
        tex.inputs["Detail"].default_value = 12.0
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (*color_a, 1)
    ramp.color_ramp.elements[1].color = (*color_b, 1)
    nt.links.new(tex.outputs[0 if kind == "noise" else "Distance"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.8
    if "Material Output" not in nt.nodes:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    return mat


# Subject: Suzanne on a pedestal.
bpy.ops.mesh.primitive_monkey_add(size=1.2, location=(0, 0, 1.25))
monkey = bpy.context.object
monkey.rotation_euler = (math.radians(15), 0, math.radians(20))
mod = monkey.modifiers.new("sub", "SUBSURF")
mod.levels = mod.render_levels = 2
monkey.data.materials.append(textured_material("skin", 9.0, (0.8, 0.35, 0.1), (0.15, 0.05, 0.02)))
bpy.ops.mesh.primitive_cylinder_add(radius=0.45, depth=0.6, location=(0, 0, 0.3))
bpy.context.object.data.materials.append(textured_material("stone", 25.0, (0.2, 0.3, 0.6), (0.9, 0.9, 0.85), "voronoi"))

# Background: floor + clutter (must be removed by cleanup).
bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 0, 0))
bpy.context.object.data.materials.append(textured_material("floor", 6.0, (0.1, 0.4, 0.1), (0.9, 0.85, 0.6)))
for i, (x, y) in enumerate([(3.0, 1.0), (-2.5, 2.5), (1.0, -3.2), (-3.0, -1.5)]):
    bpy.ops.mesh.primitive_cube_add(size=0.7, location=(x, y, 0.35))
    bpy.context.object.data.materials.append(textured_material(f"box{i}", 14.0, (0.7, 0.1, 0.4), (0.1, 0.1, 0.1), "voronoi"))

world = bpy.data.worlds.new("w")
world.color = (0.6, 0.6, 0.65)
scene.world = world
bpy.ops.object.light_add(type="SUN", location=(4, -4, 8))
bpy.context.object.data.energy = 3.0
bpy.context.object.rotation_euler = (math.radians(40), math.radians(20), math.radians(30))

bpy.ops.object.camera_add()
cam = bpy.context.object
cam.data.lens = 28
scene.camera = cam
target = bpy.data.objects.new("target", None)
target.location = (0, 0, 0.9)
scene.collection.objects.link(target)
track = cam.constraints.new("TRACK_TO")
track.target = target
track.track_axis = "TRACK_NEGATIVE_Z"
track.up_axis = "UP_Y"

# Non-uniform progress around the orbit with a pause, plus small hand-held wobble.
pause_start, pause_len = int(n_frames * 0.4), int(n_frames * 0.12)
weights = []
for f in range(n_frames):
    if pause_start <= f < pause_start + pause_len:
        weights.append(0.0)
    else:
        weights.append(1.0 + 0.6 * math.sin(f / n_frames * 6 * math.pi))
total = sum(weights)
angle = 0.0
for f in range(n_frames):
    angle += weights[f] / total * 2 * math.pi
    r = 4.0 + 0.15 * math.sin(f * 0.37)
    z = 1.6 + 0.4 * math.sin(angle * 2) + 0.03 * math.sin(f * 1.3)
    cam.location = (r * math.cos(angle), r * math.sin(angle), z)
    cam.keyframe_insert("location", frame=f + 1)

scene.frame_start, scene.frame_end = 1, n_frames
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 16
scene.cycles.use_denoising = True
scene.render.resolution_x, scene.render.resolution_y = res_x, res_y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = f"{out_dir}/"
bpy.ops.render.render(animation=True)
