"""Projection and selected-object picking regression; run with Blender --background."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Matrix, Vector

spec = importlib.util.spec_from_file_location("zlm_stroke_test", Path(__file__).with_name("bflm_loop_measure.py"))
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
original_ray = addon._screen_ray

try:
    # A screen line off the optical center, in an oblique camera frame.
    # Both rays and every interpolated screen sample must lie in the cut plane.
    rotation = Matrix.Rotation(0.37, 4, "Y") @ Matrix.Rotation(0.21, 4, "Z")
    camera = Vector((3.0, -2.0, 7.0))
    start, end = Vector((-0.6, 0.45)), Vector((0.8, 0.9))
    for perspective in (True, False):
        def ray(context, coordinate):
            if perspective:
                return camera, (rotation.to_3x3() @ Vector((*coordinate, -1))).normalized()
            return camera + rotation.to_3x3() @ Vector((*coordinate, 0)), rotation.to_3x3() @ Vector((0, 0, -1))

        addon._screen_ray = ray
        context = SimpleNamespace(space_data=SimpleNamespace(region_3d=SimpleNamespace(is_perspective=perspective)))
        origin, direction = ray(context, start.lerp(end, 0.5))
        surface = origin + direction * 5
        plane_point, normal = addon._stroke_plane(context, start, end, surface)
        for i in range(11):
            origin, direction = ray(context, start.lerp(end, i / 10))
            for depth in (1, 5, 12):
                assert abs((origin + direction * depth - plane_point).dot(normal)) < 2.0e-6
    print("orthographic and perspective screen-line projection: OK")

    # A generated curve/other object in front of the source must not steal hits.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, location=(0, 0, 0))
    source = bpy.context.object
    source.scale = (0.5, 0.8, 1.2)
    bpy.ops.mesh.primitive_cube_add(size=3, location=(0, 0, 3))
    occluder = bpy.context.object
    bpy.context.view_layer.update()
    context = SimpleNamespace(evaluated_depsgraph_get=bpy.context.evaluated_depsgraph_get)
    addon._screen_ray = lambda context, p: (Vector((p.x, p.y, 5)), Vector((0, 0, -1)))
    assert bpy.context.scene.ray_cast(bpy.context.evaluated_depsgraph_get(), Vector((0, 0, 5)), Vector((0, 0, -1)))[4] == occluder
    hit = addon._surface_point(context, source, Vector((-0.2, 0)), Vector((0.2, 0)))
    assert hit is not None and abs(hit.z - 1.2) < 1.0e-5, hit
    assert addon._surface_point(context, source, Vector((2, 0)), Vector((3, 0))) is None
    print("selected mesh ray cast under occlusion and finite stroke miss: OK")
finally:
    addon._screen_ray = original_ray

print("ZLM stroke regressions: ALL PASSED")
