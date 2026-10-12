"""Blender regression tests for picking and resizing one finger of a hand.

Run in an isolated Blender process:
    blender --background --factory-startup --python-exit-code 1 --python test_zlm_fingers.py
"""

import importlib.util
import math
from pathlib import Path

import bpy
from mathutils import Vector


PLANE_POINT = Vector((0.0, 0.0, 2.025))
PLANE_NORMAL = Vector((0.0, 0.0, 1.0))
SMALL_ANCHOR = Vector((0.0, -0.45, 2.025))
LARGE_ANCHOR = Vector((1.22, -0.70, 2.025))


def load_addon():
    source = Path(__file__).with_name("bflm_loop_measure.py")
    spec = importlib.util.spec_from_file_location("zlm_finger_regression", source)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def make_hand(name):
    """Two fingers share a palm bridge; a third nearby tube is disconnected.

    The small and large fingers are only 0.07 units apart in space, but their
    surface paths run down to the bridge at z=0 and back up to the cut at z=2.
    A component-wide filter alone therefore cannot protect the adjacent finger.
    """
    segments = 64
    rows = 21
    vertices = []
    faces = []
    groups = []
    for center_x, radius in ((0.0, 0.45), (1.22, 0.70), (-0.70, 0.18)):
        offset = len(vertices)
        for row in range(rows):
            z = row * 0.2
            vertices.extend(
                (
                    center_x + radius * math.cos(math.tau * step / segments),
                    radius * math.sin(math.tau * step / segments),
                    z,
                )
                for step in range(segments)
            )
        for row in range(rows - 1):
            for step in range(segments):
                next_step = (step + 1) % segments
                lower = offset + row * segments
                upper = lower + segments
                faces.append((lower + step, lower + next_step, upper + next_step, upper + step))
        groups.append(tuple(range(offset, len(vertices))))

    # Join bottom boundary edges by a palm-like surface patch, not a loose edge.
    large_offset = groups[1][0]
    faces.append((0, segments - 1, large_offset + 33, large_offset + 32))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj, groups


def topology(obj):
    return (
        len(obj.data.vertices),
        tuple(tuple(edge.vertices) for edge in obj.data.edges),
        tuple(tuple(face.vertices) for face in obj.data.polygons),
    )


def coordinates(obj):
    return [vertex.co.copy() for vertex in obj.data.vertices]


def maximum_movement(obj, before, indices):
    return max((obj.data.vertices[index].co - before[index]).length for index in indices)


def assert_fixture_connections(obj, groups):
    adjacency = [[] for _vertex in obj.data.vertices]
    for edge in obj.data.edges:
        first, second = edge.vertices
        adjacency[first].append(second)
        adjacency[second].append(first)
    visited = {groups[0][0]}
    pending = list(visited)
    while pending:
        for neighbor in adjacency[pending.pop()]:
            if neighbor not in visited:
                visited.add(neighbor)
                pending.append(neighbor)
    assert set(groups[0]).issubset(visited)
    assert set(groups[1]).issubset(visited), "palm fixture must connect both fingers"
    assert not set(groups[2]).intersection(visited), "third tube must be a separate island"


def select_object(obj):
    for other in bpy.context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def add_target(addon, obj, name, anchor):
    settings = bpy.context.scene.bflm_settings
    target = settings.targets.add()
    target.uid = name
    target.name = name
    target.source_object = obj.name
    target.plane_point = tuple(PLANE_POINT)
    target.plane_normal = tuple(PLANE_NORMAL)
    target.selection_anchor = tuple(anchor)
    target.has_selection_anchor = True
    target.falloff_radius = 0.65
    target.falloff_shape = "SMOOTH"
    target.lock_normal = True
    assert target.use_connected, "new targets must protect adjacent fingers by default"
    loops = addon._refresh_target(target, True)
    assert len(loops) == 1, f"{name}: expected a single selected loop, got {len(loops)}"
    target.target_length = target.current_length
    return target


def assert_selected_center(addon, loops, expected_x):
    assert len(loops) == 1, f"one finger should yield one selected loop, got {len(loops)}"
    assert abs(addon._loop_center(loops[0]).x - expected_x) < 0.01


def test_selection_and_operators(addon):
    obj, groups = make_hand("ConnectedHandTargets")
    select_object(obj)
    assert_fixture_connections(obj, groups)
    original_topology = topology(obj)
    loops, open_count = addon._extract_loops(obj, PLANE_POINT, PLANE_NORMAL)
    assert len(loops) == 3 and open_count == 0, (len(loops), open_count)
    small_loops, open_count = addon._extract_loops(
        obj, PLANE_POINT, PLANE_NORMAL, anchor=SMALL_ANCHOR
    )
    assert_selected_center(addon, small_loops, 0.0)
    assert open_count == 0
    small_perimeter = addon._loop_length(small_loops[0])
    expected_polygon_perimeter = 2.0 * 64 * 0.45 * math.sin(math.pi / 64)
    assert abs(small_perimeter - expected_polygon_perimeter) < 1.0e-5
    assert small_perimeter < max(addon._loop_length(loop) for loop in loops) * 0.7

    settings = bpy.context.scene.bflm_settings
    small = add_target(addon, obj, "SmallFinger", SMALL_ANCHOR)
    large = add_target(addon, obj, "LargeFinger", LARGE_ANCHOR)
    assert abs(small.current_length - small_perimeter) < 1.0e-5
    large_length_before = large.current_length
    settings.active_index = 0
    small.target_length = small.current_length * 1.10
    desired_small_length = small.target_length
    before = coordinates(obj)
    result = bpy.ops.bflm.resize_loop()
    assert result == {"FINISHED"}, result
    assert abs(small.current_length - desired_small_length) < 0.002, (
        small.current_length, desired_small_length
    )
    assert maximum_movement(obj, before, groups[0]) > 0.01
    assert maximum_movement(obj, before, groups[1]) < 1.0e-7, "adjacent connected finger moved"
    assert maximum_movement(obj, before, groups[2]) < 1.0e-7, "disconnected nearby island moved"
    assert abs(large.current_length - large_length_before) < 1.0e-5
    assert_selected_center(addon, addon._RUNTIME["loop_cache"][small.uid], 0.0)
    assert_selected_center(addon, addon._RUNTIME["loop_cache"][large.uid], 1.22)
    for target in (small, large):
        result_object = addon._result_object_for_uid(target.uid)
        assert result_object is not None
        assert len(result_object.data.splines) == 1, "result curve includes a neighboring finger"

    # Selecting from saved result curves and reconstructing without curves/cache
    # must preserve the target's finger, even though both use the same cut plane.
    addon._RUNTIME["loop_cache"].clear()
    settings.active_index = 1
    assert_selected_center(addon, addon._RUNTIME["loops"], 1.22)
    settings.keep_result = False
    addon._RUNTIME["loop_cache"].clear()
    settings.active_index = 0
    assert_selected_center(addon, addon._RUNTIME["loops"], 0.0)
    addon._RUNTIME["loop_cache"].clear()
    settings.active_index = 1
    assert_selected_center(addon, addon._RUNTIME["loops"], 1.22)

    before = coordinates(obj)
    previous_small_length = small.current_length
    large.target_length = large.current_length * 0.92
    assert bpy.ops.bflm.resize_loop() == {"FINISHED"}
    assert abs(large.current_length - large.target_length) < 0.002
    assert abs(small.current_length - previous_small_length) < 1.0e-5
    assert maximum_movement(obj, before, groups[0]) < 1.0e-7
    assert maximum_movement(obj, before, groups[1]) > 0.01
    assert maximum_movement(obj, before, groups[2]) < 1.0e-7
    assert topology(obj) == original_topology, "resize changed mesh connectivity"
    assert bpy.ops.bflm.clear_all_targets() == {"FINISHED"}
    print("finger selection, two independent targets, cache recovery, topology: OK")


def test_distance_modes(addon):
    # Use a fresh fixture for each mode so deformations cannot hide the behavior.
    for connected, radius in ((True, 0.65), (False, 0.65), (True, 8.0)):
        obj, groups = make_hand(f"DistanceMode_{connected}_{radius}")
        original_topology = topology(obj)
        before = coordinates(obj)
        loops, _ = addon._extract_loops(obj, PLANE_POINT, PLANE_NORMAL, anchor=SMALL_ANCHOR)
        addon._apply_radial_resize(
            obj, loops[0], PLANE_NORMAL, 1.05, radius, "SMOOTH",
            lock_normal=True, use_connected=connected,
        )
        assert maximum_movement(obj, before, groups[0]) > 0.005
        large_movement = maximum_movement(obj, before, groups[1])
        island_movement = maximum_movement(obj, before, groups[2])
        if connected and radius < 1.0:
            assert large_movement < 1.0e-7, "surface-distance mode leaked across finger gap"
        else:
            assert large_movement > 1.0e-4, "expected radius to reach adjacent finger"
        if connected:
            assert island_movement < 1.0e-7, "disconnected island has no surface path"
        else:
            assert island_movement > 1.0e-4, "disabled option should use spatial falloff"
        assert topology(obj) == original_topology
        assert all(abs(vertex.co.z - before[vertex.index].z) < 1.0e-7 for vertex in obj.data.vertices)
        print(f"distance mode connected={connected}, radius={radius}: OK")


def test_transformed_hand(addon):
    obj, groups = make_hand("TransformedHand")
    obj.location = (5.0, -3.0, 1.0)
    obj.rotation_euler = (0.4, 0.7, 0.2)
    obj.scale = (0.7, 1.8, 0.9)
    bpy.context.view_layer.update()
    world = obj.matrix_world.copy()
    point = world @ PLANE_POINT
    normal = (world.to_3x3().inverted().transposed() @ PLANE_NORMAL).normalized()
    anchor = world @ SMALL_ANCHOR
    loops, _ = addon._extract_loops(obj, point, normal, anchor=anchor)
    assert len(loops) == 1
    center_local = world.inverted() @ addon._loop_center(loops[0])
    assert abs(center_local.x) < 0.01 and abs(center_local.y) < 0.01
    original_topology = topology(obj)
    before = coordinates(obj)
    addon._apply_radial_resize(
        obj, loops[0], normal, 1.05, 0.45, "SMOOTH", use_connected=True,
    )
    assert maximum_movement(obj, before, groups[0]) > 0.005
    assert maximum_movement(obj, before, groups[1]) < 1.0e-7
    assert maximum_movement(obj, before, groups[2]) < 1.0e-7
    assert topology(obj) == original_topology
    assert all(abs(vertex.co.z - before[vertex.index].z) < 2.0e-6 for vertex in obj.data.vertices)
    print("rotated, translated, nonuniformly scaled hand: OK")


def test_coplanar_seeds(addon):
    obj, groups = make_hand("CoplanarFinger")
    cut_point = Vector((0.0, 0.0, 2.0))
    cut_anchor = Vector((0.0, -0.45, 2.0))
    loops, open_count, seeds = addon._extract_loops(
        obj, cut_point, PLANE_NORMAL, anchor=cut_anchor,
        use_evaluated=False, with_seeds=True,
    )
    assert len(loops) == 1 and open_count == 0
    assert len(seeds) == 64, f"expected one full original ring, got {len(seeds)} seeds"
    assert set(seeds).issubset(groups[0]), "coplanar seeds include an adjacent finger"
    assert all(abs(distance) < 1.0e-7 for distance in seeds.values())
    before = coordinates(obj)
    original_topology = topology(obj)
    addon._apply_radial_resize(
        obj, loops[0], PLANE_NORMAL, 1.10, 0.05, "SMOOTH", use_connected=True,
    )
    selected_ring = {index for index in groups[0] if abs(before[index].z - 2.0) < 1.0e-7}
    assert maximum_movement(obj, before, selected_ring) > 0.04
    others = set(range(len(obj.data.vertices))) - selected_ring
    assert maximum_movement(obj, before, others) < 1.0e-7
    assert topology(obj) == original_topology
    print("coplanar cut uses only original selected-ring seeds: OK")


def test_cancel_restores_coordinates(addon):
    settings = bpy.context.scene.bflm_settings
    for shared, radius in ((False, 0.001), (True, 0.001), (False, 0.026)):
        obj, _groups = make_hand(f"CancelledEdit_{shared}_{radius}")
        select_object(obj)
        original_mesh = obj.data
        if shared:
            clone = obj.copy()
            clone.name = "LinkedHandCancellation"
            bpy.context.collection.objects.link(clone)
            assert clone.data == original_mesh
        target = add_target(addon, obj, f"CancelTarget_{shared}", SMALL_ANCHOR)
        settings.active_index = len(settings.targets) - 1
        target.falloff_radius = radius
        target.target_length = target.current_length * 1.20
        original_coordinates = tuple(tuple(vertex.co) for vertex in obj.data.vertices)
        original_topology = topology(obj)
        original_length = target.current_length
        original_anchor = tuple(target.selection_anchor)
        original_mesh_count = len(bpy.data.meshes)
        assert bpy.ops.bflm.resize_loop() == {"CANCELLED"}
        assert obj.data == original_mesh, "cancelled edit left a different mesh datablock"
        assert len(bpy.data.meshes) == original_mesh_count, "cancelled edit leaked a temporary mesh"
        assert tuple(tuple(vertex.co) for vertex in obj.data.vertices) == original_coordinates
        assert topology(obj) == original_topology
        assert target.current_length == original_length
        assert tuple(target.selection_anchor) == original_anchor
        if shared:
            assert clone.data == original_mesh
        assert bpy.ops.bflm.clear_all_targets() == {"FINISHED"}
    print("too-small radius cancellation restores exact coordinates/shared mesh: OK")


def test_nested_island_seeding(addon):
    # An interior disconnected shell must not take over merely because the
    # selected outer contour's arithmetic center is nearer to the inner shell.
    obj, groups = make_hand("NestedFingerShell")
    for index in groups[2]:
        obj.data.vertices[index].co.x += 0.70
    obj.data.update()
    bpy.context.view_layer.update()
    loops, _ = addon._extract_loops(obj, PLANE_POINT, PLANE_NORMAL, anchor=SMALL_ANCHOR)
    assert len(loops) == 1
    assert abs(addon._loop_length(loops[0]) - 2.0 * 64 * 0.45 * math.sin(math.pi / 64)) < 1.0e-5
    before = coordinates(obj)
    addon._apply_radial_resize(
        obj, loops[0], PLANE_NORMAL, 1.10, 0.65, "SMOOTH", use_connected=True,
    )
    assert maximum_movement(obj, before, groups[0]) > 0.01, "outer target shell did not move"
    assert maximum_movement(obj, before, groups[1]) < 1.0e-7
    assert maximum_movement(obj, before, groups[2]) < 1.0e-7, "inner disconnected shell was seeded"
    select_object(obj)
    target = add_target(addon, obj, "OuterShellTarget", SMALL_ANCHOR)
    first_length = target.current_length
    addon._refresh_target(target, False)
    assert abs(target.current_length - first_length) < 1.0e-5, "refresh switched to inner shell"
    settings = bpy.context.scene.bflm_settings
    settings.active_index = 0
    target.target_length = first_length * 1.05
    before = coordinates(obj)
    assert bpy.ops.bflm.resize_loop() == {"FINISHED"}
    assert abs(target.current_length - target.target_length) < 0.002
    assert maximum_movement(obj, before, groups[0]) > 0.005
    assert maximum_movement(obj, before, groups[2]) < 1.0e-7
    assert bpy.ops.bflm.clear_all_targets() == {"FINISHED"}
    print("nested disconnected shell cannot steal selected contour seeds: OK")


addon = load_addon()
addon.register()
try:
    test_selection_and_operators(addon)
    test_distance_modes(addon)
    test_transformed_hand(addon)
    test_coplanar_seeds(addon)
    test_cancel_restores_coordinates(addon)
    test_nested_island_seeding(addon)
finally:
    addon.unregister()
print("ZLM hand/finger regressions: ALL PASSED")
