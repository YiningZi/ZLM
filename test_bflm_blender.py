import importlib.util
import math
from pathlib import Path

import bpy
from mathutils import Vector


def load_addon():
    path = Path(__file__).with_name("bflm_loop_measure.py")
    spec = importlib.util.spec_from_file_location("bflm_loop_measure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def perimeter(loop):
    return sum((loop[i] - loop[(i + 1) % len(loop)]).length for i in range(len(loop)))


addon = load_addon()

bpy.ops.mesh.primitive_uv_sphere_add(segments=256, ring_count=128, radius=1.0)
sphere = bpy.context.object
loops, open_count = addon._extract_loops(sphere, Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0)))
assert len(loops) == 1, f"sphere loops: {len(loops)}"
assert open_count == 0, f"sphere open chains: {open_count}"
assert abs(perimeter(loops[0]) - math.tau) < 0.002, perimeter(loops[0])
print("high-poly sphere:", len(loops[0]), perimeter(loops[0]))

# A dense cylindrical sheet with a 36-degree longitudinal opening.
steps = 256
span = math.tau * 0.9
vertices = []
for z in (-1.0, 1.0):
    vertices.extend((math.cos(span * i / steps), math.sin(span * i / steps), z) for i in range(steps + 1))
faces = []
row = steps + 1
for i in range(steps):
    faces.append((i, i + 1, row + i + 1, row + i))
mesh = bpy.data.meshes.new("OpenFingerSheet")
mesh.from_pydata(vertices, [], faces)
mesh.update()
sheet = bpy.data.objects.new("OpenFingerSheet", mesh)
bpy.context.collection.objects.link(sheet)
loops, open_count = addon._extract_loops(sheet, Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0)))
expected = span + 2.0 * math.sin((math.tau - span) * 0.5)
assert len(loops) == 1, f"sheet loops: {len(loops)}"
assert open_count == 1, f"sheet open chains: {open_count}"
assert abs(perimeter(loops[0]) - expected) < 0.002, (perimeter(loops[0]), expected)
print("open high-poly sheet:", len(loops[0]), perimeter(loops[0]), "expected:", expected)

addon.register()
assert hasattr(bpy.types, "BFLM_PT_about")
for scene_object in bpy.context.scene.objects:
    scene_object.select_set(False)
sphere.select_set(True)
bpy.context.view_layer.objects.active = sphere
settings = bpy.context.scene.bflm_settings
target = settings.targets.add()
target.uid = "test_center"
target.name = "指节 A"
target.current_length = math.tau
target.length_text = str(math.tau)
target.loop_count = 1
target.target_length = math.tau * 1.15
target.falloff_radius = 0.35
target.falloff_shape = "SMOOTH"
target.lock_normal = True
target.plane_point = (0.0, 0.0, 0.0)
target.plane_normal = (0.0, 0.0, 1.0)
target.source_object = sphere.name
second = settings.targets.add()
second.uid = "test_upper"
second.name = "指节 B"
second_loops, _ = addon._extract_loops(
    sphere, Vector((0.0, 0.0, 0.55)), Vector((0.0, 0.0, 1.0))
)
second.current_length = perimeter(max(second_loops, key=perimeter))
second.length_text = str(second.current_length)
second.target_length = second.current_length
second.plane_point = (0.0, 0.0, 0.55)
second.plane_normal = (0.0, 0.0, 1.0)
second.source_object = sphere.name
settings.active_index = 0
topology_before = (len(sphere.data.vertices), len(sphere.data.edges), len(sphere.data.polygons))
connectivity_before = (
    tuple(tuple(edge.vertices) for edge in sphere.data.edges),
    tuple(tuple(polygon.vertices) for polygon in sphere.data.polygons),
)
result = bpy.ops.bflm.resize_loop()
assert result == {"FINISHED"}, result
resized_loops, _ = addon._extract_loops(
    sphere, Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))
)
resized_length = perimeter(max(resized_loops, key=perimeter))
topology_after = (len(sphere.data.vertices), len(sphere.data.edges), len(sphere.data.polygons))
connectivity_after = (
    tuple(tuple(edge.vertices) for edge in sphere.data.edges),
    tuple(tuple(polygon.vertices) for polygon in sphere.data.polygons),
)
assert abs(resized_length - target.target_length) < 0.002, resized_length
assert topology_after == topology_before, (topology_before, topology_after)
assert connectivity_after == connectivity_before, "mesh connectivity changed"
updated_second_loops, _ = addon._extract_loops(
    sphere, Vector((0.0, 0.0, 0.55)), Vector((0.0, 0.0, 1.0))
)
updated_second_length = perimeter(max(updated_second_loops, key=perimeter))
assert abs(second.current_length - updated_second_length) < 0.002
assert len([obj for obj in bpy.data.objects if obj.get("bflm_generated")]) == 2
print("radial resize:", resized_length, "topology:", topology_after)
bpy.ops.bflm.remove_target()
assert len(settings.targets) == 1
bpy.ops.bflm.clear_all_targets()
assert len(settings.targets) == 0
addon.unregister()
print("addon register/unregister: ok")
