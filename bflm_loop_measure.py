bl_info = {
    "name": "ZLM",
    "author": "Yining Zi",
    "version": (1, 11, 1),
    "blender": (4, 4, 0),
    "location": "3D View > Sidebar > ZLM",
    "description": "画线测量模型截面周长，并通过可控衰减局部调整围径",
    "category": "3D View",
}

# Extensions remove bl_info during loading; UI code must use independent metadata.
# Keep the literal bl_info version above for Blender's legacy add-on scanner.
ZLM_VERSION = (1, 11, 1)

import hashlib
import heapq
import json
import math
import os
import uuid

import bmesh
import bpy
import blf
import gpu
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.kdtree import KDTree


_INTEGRITY_STATUS = "UNCHECKED"
_INTEGRITY_MESSAGE = "尚未校验"


_RUNTIME = {
    "operator": None,
    "stroke_handler": None,
    "result_handler": None,
    "start": None,
    "current": None,
    "loops": [],
    "length": 0.0,
    "region": None,
    "label_handler": None,
    "source_obj": None,
    "plane_point": None,
    "plane_normal": None,
    "target_uid": "",
    "loop_cache": {},
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_integrity():
    global _INTEGRITY_STATUS, _INTEGRITY_MESSAGE
    root = os.path.realpath(os.path.dirname(__file__))
    manifest_path = os.path.join(root, "integrity.json")
    if not os.path.isfile(manifest_path):
        _INTEGRITY_STATUS = "UNAVAILABLE"
        _INTEGRITY_MESSAGE = "开发模式：未安装校验清单"
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        failures = []
        for relative_path, expected_hash in manifest.get("files", {}).items():
            candidate = os.path.realpath(os.path.join(root, relative_path))
            if os.path.commonpath((root, candidate)) != root:
                failures.append(relative_path)
                continue
            if not os.path.isfile(candidate) or _sha256_file(candidate) != expected_hash.lower():
                failures.append(relative_path)
        if failures:
            _INTEGRITY_STATUS = "FAILED"
            _INTEGRITY_MESSAGE = "文件异常：" + ", ".join(failures)
            print("ZLM 完整性警告：" + _INTEGRITY_MESSAGE)
            return False
        _INTEGRITY_STATUS = "OK"
        _INTEGRITY_MESSAGE = "SHA-256 完整性校验通过"
        return True
    except Exception as error:
        _INTEGRITY_STATUS = "FAILED"
        _INTEGRITY_MESSAGE = f"校验失败：{error}"
        print("ZLM 完整性警告：" + _INTEGRITY_MESSAGE)
        return False


def _screen_ray(context, coord):
    region = context.region
    rv3d = context.space_data.region_3d
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    return origin, direction.normalized()


def _surface_point(context, obj, start, end):
    """Ray cast only the selected mesh, so result curves cannot intercept a stroke."""
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    inverse = evaluated.matrix_world.inverted()
    # Sample at roughly four-pixel intervals, with the stroke midpoint first.
    steps = max(12, min(512, int((end - start).length / 4)))
    samples = sorted(range(steps + 1), key=lambda i: abs(i / steps - 0.5))
    for i in samples:
        coord = start.lerp(end, i / steps)
        origin, direction = _screen_ray(context, coord)
        local_direction = (inverse.to_3x3() @ direction).normalized()
        hit, location, _normal, _face = evaluated.ray_cast(
            inverse @ origin, local_direction
        )
        if hit:
            return evaluated.matrix_world @ location
    return None


def _stroke_plane(context, start, end, surface):
    """The projection of a screen line is a plane, including in perspective."""
    first_origin, first_ray = _screen_ray(context, start)
    last_origin, last_ray = _screen_ray(context, end)
    if context.space_data.region_3d.is_perspective:
        normal = first_ray.cross(last_ray)
    else:
        normal = (last_origin - first_origin).cross(first_ray)
    if normal.length < 1.0e-10:
        raise ValueError("无法确定截面方向，请重新画线")
    return surface, normal.normalized()


def _point_loop_distance_squared(point, loop):
    distance = math.inf
    for i, a in enumerate(loop):
        b = loop[(i + 1) % len(loop)]
        edge = b - a
        t = max(0.0, min(1.0, (point - a).dot(edge) / edge.length_squared)) if edge.length_squared else 0.0
        distance = min(distance, (point - (a + edge * t)).length_squared)
    return distance


def _extract_loops(obj, plane_point, plane_normal, anchor=None, *, use_evaluated=True, with_seeds=False):
    # Bisection is performed on a temporary copy; never write this BMesh back.
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()) if use_evaluated else None
    mesh = evaluated.to_mesh() if evaluated else obj.data
    empty = ([], 0, {}) if with_seeds else ([], 0)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if not bm.verts or not bm.faces:
            return empty
        original_vertices = {v: i for i, v in enumerate(bm.verts)} if with_seeds else {}

        world = obj.matrix_world
        min_corner = Vector((math.inf, math.inf, math.inf))
        max_corner = Vector((-math.inf, -math.inf, -math.inf))
        for vertex in bm.verts:
            coord = vertex.co
            min_corner.x = min(min_corner.x, coord.x)
            min_corner.y = min(min_corner.y, coord.y)
            min_corner.z = min(min_corner.z, coord.z)
            max_corner.x = max(max_corner.x, coord.x)
            max_corner.y = max(max_corner.y, coord.y)
            max_corner.z = max(max_corner.z, coord.z)
        local_diagonal = max((max_corner - min_corner).length, 1.0e-6)
        tolerance = local_diagonal * 1.0e-7

        inverse = world.inverted_safe()
        local_plane_point = inverse @ plane_point
        local_plane_normal = world.to_3x3().transposed() @ plane_normal
        if local_plane_normal.length < 1.0e-12:
            return empty
        local_plane_normal.normalize()

        cut = bmesh.ops.bisect_plane(
            bm,
            geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
            dist=tolerance,
            plane_co=local_plane_point,
            plane_no=local_plane_normal,
            clear_outer=False,
            clear_inner=False,
        )
        cut_edges = [
            element
            for element in cut.get("geom_cut", [])
            if isinstance(element, bmesh.types.BMEdge) and element.is_valid
        ]
        if not cut_edges:
            # Some builds omit already-coplanar edges from geom_cut. Include
            # them only when both endpoints lie on the cutting plane.
            cut_edges = [
                edge
                for edge in bm.edges
                if edge.is_valid
                and all(
                    abs((vertex.co - local_plane_point).dot(local_plane_normal)) <= tolerance * 2.0
                    for vertex in edge.verts
                )
            ]
        if not cut_edges:
            return empty

        adjacency = {}
        for edge in cut_edges:
            for vertex in edge.verts:
                adjacency.setdefault(vertex, []).append(edge)

        used = set()
        sections = []

        def choose_continuation(current, previous, candidates):
            if previous is None or len(candidates) == 1:
                return candidates[0]
            incoming = (world @ current.co) - (world @ previous.co)
            if incoming.length < 1.0e-12:
                return candidates[0]
            incoming.normalize()

            def alignment(edge):
                outgoing = (world @ edge.other_vert(current).co) - (world @ current.co)
                return incoming.dot(outgoing.normalized()) if outgoing.length > 1.0e-12 else -1.0

            return max(candidates, key=alignment)

        def walk_chain(start_vertex, first_edge):
            loop = [start_vertex]
            current = start_vertex
            previous = None
            next_edge = first_edge
            for _ in range(len(cut_edges) + 1):
                if next_edge is None or next_edge in used:
                    break
                used.add(next_edge)
                next_vertex = next_edge.other_vert(current)
                loop.append(next_vertex)
                if next_vertex == start_vertex:
                    break
                candidates = [edge for edge in adjacency.get(next_vertex, []) if edge not in used]
                previous, current = current, next_vertex
                next_edge = (
                    choose_continuation(current, previous, candidates) if candidates else None
                )
            return loop

        def add_chain(vertices):
            if len(vertices) < 3:
                return
            is_closed = vertices[-1] == vertices[0]
            if is_closed:
                vertices.pop()
            loop = [world @ vertex.co for vertex in vertices]
            if len(loop) < 3 or _loop_length(loop) <= source_diagonal * 1.0e-5:
                return
            seeds = {}
            if with_seeds:
                # Each cut vertex is on an original mesh edge. Keep only its
                # original endpoints as distance seeds, not nearby other digits.
                for vertex, point in zip(vertices, loop):
                    if vertex in original_vertices:
                        seeds[original_vertices[vertex]] = 0.0
                    else:
                        for edge in vertex.link_edges:
                            other = edge.other_vert(vertex)
                            if other in original_vertices:
                                index = original_vertices[other]
                                distance = (world @ other.co - point).length
                                seeds[index] = min(seeds.get(index, math.inf), distance)
            sections.append((loop, not is_closed, seeds))

        # Open chains must start at a degree-one endpoint; starting in their
        # middle would split a finger section into two incomplete measurements.
        source_diagonal = max(obj.dimensions.length, 1.0e-6)
        for endpoint, attached in adjacency.items():
            if len(attached) != 1 or attached[0] in used:
                continue
            add_chain(walk_chain(endpoint, attached[0]))

        # Anything left after the boundary pass is a closed loop (or a rare
        # branched component). Walk it from either endpoint.
        for edge in cut_edges:
            if edge not in used:
                add_chain(walk_chain(edge.verts[0], edge))
        if anchor is not None and sections:
            sections = [min(sections, key=lambda item: _point_loop_distance_squared(anchor, item[0]))]
        loops = [item[0] for item in sections]
        open_count = sum(item[1] for item in sections)
        if with_seeds:
            seeds = {}
            for _loop, _open, seed_map in sections:
                for index, distance in seed_map.items():
                    seeds[index] = min(seeds.get(index, math.inf), distance)
            return loops, open_count, seeds
        return loops, open_count
    finally:
        bm.free()
        if evaluated:
            evaluated.to_mesh_clear()


def _format_length(value):
    settings = bpy.context.scene.unit_settings
    if settings.system != "NONE":
        # Shared Blender 4.4+ API: the second argument is the unit category,
        # and world-space Blender units must be converted using scene scale.
        return bpy.utils.units.to_string(
            settings.system, "LENGTH", value * settings.scale_length, precision=3
        )
    return f"{value:.3f} BU"


def _loop_length(loop):
    if len(loop) < 2:
        return 0.0
    return sum((loop[i] - loop[(i + 1) % len(loop)]).length for i in range(len(loop)))


def _loop_center(loop):
    center = Vector((0.0, 0.0, 0.0))
    for point in loop:
        center += point
    return center / max(len(loop), 1)


def _build_loop_kdtree(loop):
    # Four samples per cut edge make nearest-point falloff fast on dense meshes
    # while keeping low-poly contours sufficiently continuous.
    sample_count = len(loop) * 4
    tree = KDTree(sample_count)
    sample_index = 0
    for index, start in enumerate(loop):
        end = loop[(index + 1) % len(loop)]
        for step in range(4):
            tree.insert(start.lerp(end, step / 4.0), sample_index)
            sample_index += 1
    tree.balance()
    return tree


def _falloff_weight(distance, radius, shape):
    if radius <= 1.0e-12:
        return 1.0 if distance <= 1.0e-8 else 0.0
    x = max(0.0, min(1.0, distance / radius))
    if x >= 1.0:
        return 0.0
    remaining = 1.0 - x
    if shape == "CONSTANT":
        return 1.0
    if shape == "LINEAR":
        return remaining
    if shape == "SHARP":
        return remaining * remaining
    if shape == "ROOT":
        return math.sqrt(remaining)
    if shape == "SPHERE":
        return math.sqrt(max(0.0, 1.0 - x * x))
    # Blender's default smooth proportional-edit profile.
    return remaining * remaining * (3.0 - 2.0 * remaining)


def _apply_radial_resize(
    obj, loop, plane_normal, scale_factor, radius, shape, lock_normal=True, use_connected=False
):
    """Move existing vertices only; no faces, edges, or mesh topology are created."""
    if abs(scale_factor - 1.0) <= 1.0e-8:
        return
    world = obj.matrix_world
    inverse = world.inverted_safe()
    center = _loop_center(loop)
    if use_connected:
        distances = _connected_loop_distances(obj, loop, plane_normal, radius)
    else:
        loop_tree = _build_loop_kdtree(loop)
        distances = [loop_tree.find(world @ v.co)[2] for v in obj.data.vertices]
    if not any(_falloff_weight(d, radius, shape) > 1.0e-8 for d in distances):
        raise ValueError("衰减半径内没有可移动的顶点，请增大衰减半径")
    for vertex in obj.data.vertices:
        point = world @ vertex.co
        distance = distances[vertex.index]
        weight = _falloff_weight(distance, radius, shape)
        if weight <= 1.0e-8:
            continue
        radial = point - center
        if lock_normal:
            radial -= plane_normal * radial.dot(plane_normal)
        point += radial * ((scale_factor - 1.0) * weight)
        vertex.co = inverse @ point
    obj.data.update()


def _connected_loop_distances(obj, loop, plane_normal, radius):
    """Truncated multi-source Dijkstra along original edges, in world units.

    The source contour is cut on a disposable *base* mesh copy so the seeds
    always refer to editable vertex indices, including with subdivision enabled.
    A nearby island cannot seed itself merely by being spatially close.
    """
    _loops, _open_count, seeds = _extract_loops(
        obj, loop[0], plane_normal, anchor=loop[0],
        use_evaluated=False, with_seeds=True,
    )
    if not seeds:
        raise ValueError("截面未穿过可编辑的原始网格，无法计算相连衰减")
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    adjacency = [[] for _ in points]
    for edge in obj.data.edges:
        a, b = edge.vertices
        distance = (points[b] - points[a]).length
        adjacency[a].append((b, distance))
        adjacency[b].append((a, distance))
    distances = [math.inf] * len(points)
    queue = []
    for index, distance in seeds.items():
        if distance < radius:
            distances[index] = distance
            heapq.heappush(queue, (distance, index))
    while queue:
        distance, index = heapq.heappop(queue)
        if distance > distances[index]:
            continue
        for neighbor, step in adjacency[index]:
            candidate = distance + step
            if candidate < radius and candidate < distances[neighbor]:
                distances[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    return distances


def _result_object_for_uid(uid):
    return next(
        (
            obj
            for obj in bpy.data.objects
            if obj.get("bflm_generated") and obj.get("bflm_target_uid") == uid
        ),
        None,
    )


def _create_result_object(loops, source_obj, target_uid, target_name):
    _remove_result_object(target_uid)
    if not loops:
        return None
    object_name = f"ZLM_{target_name}_{target_uid[-6:]}"
    curve = bpy.data.curves.new(object_name, type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    diagonal = max(source_obj.dimensions.length, 1.0e-5)
    curve.bevel_depth = max(diagonal * 0.002, 0.0001)
    curve.bevel_resolution = 2
    for loop in loops:
        spline = curve.splines.new("POLY")
        spline.points.add(len(loop) - 1)
        for point, coordinate in zip(spline.points, loop):
            point.co = (*coordinate, 1.0)
        spline.use_cyclic_u = True
    result = bpy.data.objects.new(object_name, curve)
    result["bflm_generated"] = True
    result["bflm_source"] = source_obj.name
    result["bflm_target_uid"] = target_uid
    bpy.context.collection.objects.link(result)
    material = bpy.data.materials.get("ZLM_MeasurementMaterial")
    if material is None:
        material = bpy.data.materials.new("ZLM_MeasurementMaterial")
        material.diffuse_color = (1.0, 0.12, 0.02, 1.0)
        material.metallic = 0.0
        material.roughness = 0.35
    curve.materials.append(material)
    return result


def _remove_result_object(target_uid=None):
    for old in list(bpy.data.objects):
        if not old.get("bflm_generated"):
            continue
        if target_uid is None or old.get("bflm_target_uid") == target_uid:
            data = old.data
            bpy.data.objects.remove(old, do_unlink=True)
            if data and data.users == 0 and isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)


def _active_target(settings):
    if not settings.targets:
        return None
    index = max(0, min(settings.active_index, len(settings.targets) - 1))
    return settings.targets[index]


def _target_anchor(target):
    # Old targets already stored the original ray hit as plane_point.
    return Vector(target.selection_anchor if target.has_selection_anchor else target.plane_point)


def _target_sections(obj, target):
    return _extract_loops(
        obj, Vector(target.plane_point), Vector(target.plane_normal), anchor=_target_anchor(target)
    )


def _resize_target_geometry(context, obj, target):
    plane_point = Vector(target.plane_point)
    normal = Vector(target.plane_normal).normalized()
    anchor = _target_anchor(target)
    radius = target.falloff_radius
    for _iteration in range(8):
        loops, _open_count = _extract_loops(obj, plane_point, normal, anchor=anchor)
        if not loops:
            raise ValueError("未找到目标手指截面，请重新画线")
        loop = loops[0]
        length = _loop_length(loop)
        if length <= 1.0e-8:
            raise ValueError("当前截面长度无效")
        anchor = loop[0].copy()
        if radius <= 0.0:
            # Size the default from this finger, not from the whole hand.
            radius = max(length * 0.15, 1.0e-6)
        scale = target.target_length / length
        if abs(scale - 1.0) <= 1.0e-5:
            break
        _apply_radial_resize(
            obj, loop, normal, scale, radius, target.falloff_shape,
            target.lock_normal, target.use_connected,
        )
        # Follow a surface point through the radial transform. A center point
        # could accidentally select a smaller disconnected shell inside a finger.
        center = _loop_center(loop)
        anchor = center + (anchor - center) * scale
        context.view_layer.update()
    loops, _ = _extract_loops(obj, plane_point, normal, anchor=anchor)
    if not loops:
        raise ValueError("调整后无法找到目标截面，请增大衰减半径")
    if abs(_loop_length(loops[0]) - target.target_length) > max(target.target_length * 1.0e-3, 1.0e-8):
        raise ValueError("当前衰减范围无法达到目标周长，请增大衰减半径或分步调整")
    target.selection_anchor = loops[0][0]
    target.has_selection_anchor = True
    return radius


def _set_runtime_target(target, loops=None):
    if target is None:
        _RUNTIME["loops"] = []
        _RUNTIME["length"] = 0.0
        _RUNTIME["source_obj"] = None
        _RUNTIME["plane_point"] = None
        _RUNTIME["plane_normal"] = None
        _RUNTIME["target_uid"] = ""
        return
    if loops is None:
        loops = _RUNTIME["loop_cache"].get(target.uid, [])
    else:
        _RUNTIME["loop_cache"][target.uid] = loops
    _RUNTIME["loops"] = loops
    _RUNTIME["length"] = target.current_length
    _RUNTIME["source_obj"] = bpy.data.objects.get(target.source_object)
    _RUNTIME["plane_point"] = Vector(target.plane_point)
    _RUNTIME["plane_normal"] = Vector(target.plane_normal)
    _RUNTIME["target_uid"] = target.uid


def _refresh_target(target, keep_result=True):
    obj = bpy.data.objects.get(target.source_object)
    if not obj or obj.type != "MESH":
        target.valid = False
        target.length_text = "模型已丢失"
        return []
    plane_normal = Vector(target.plane_normal)
    if plane_normal.length < 1.0e-8:
        target.valid = False
        target.length_text = "截面无效"
        return []
    plane_normal.normalize()
    loops, open_count = _target_sections(obj, target)
    if not loops:
        target.valid = False
        target.current_length = 0.0
        target.length_text = "未找到截面"
        _remove_result_object(target.uid)
        return []
    target.valid = True
    target.current_length = _loop_length(loops[0])
    target.length_text = _format_length(target.current_length)
    target.loop_count = len(loops)
    target.open_chain_count = open_count
    target.selection_anchor = loops[0][0]
    target.has_selection_anchor = True
    _RUNTIME["loop_cache"][target.uid] = loops
    if keep_result:
        _create_result_object(loops, obj, target.uid, target.name)
    else:
        _remove_result_object(target.uid)
    return loops


def _draw_stroke():
    if bpy.context.region != _RUNTIME["region"]:
        return
    start, current = _RUNTIME["start"], _RUNTIME["current"]
    if not start or not current:
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": [start, current]})
    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(3.0)
    shader.bind()
    shader.uniform_float("color", (0.1, 0.75, 1.0, 1.0))
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def _draw_result():
    loops = _RUNTIME["loops"]
    if not loops:
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(4.0)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.12, 0.02, 1.0))
    for loop in loops:
        points = loop + [loop[0]]
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
        batch.draw(shader)
    gpu.state.blend_set("NONE")


def _draw_label():
    length = _RUNTIME["length"]
    if not length:
        return
    font_id = 0
    blf.size(font_id, 14)
    blf.color(font_id, 1.0, 0.3, 0.1, 1.0)
    blf.position(font_id, 20, 40, 0)
    blf.draw(font_id, f"ZLM 环线周长: {_format_length(length)}")


def _loops_from_result(target_uid):
    result = _result_object_for_uid(target_uid)
    if not result or result.type != "CURVE":
        return []
    world = result.matrix_world
    return [
        [world @ Vector(point.co[:3]) for point in spline.points]
        for spline in result.data.splines
        if len(spline.points) >= 2
    ]


def _active_target_updated(settings, context):
    target = _active_target(settings)
    loops = None
    if target:
        loops = _RUNTIME["loop_cache"].get(target.uid)
        if loops is None:
            loops = _loops_from_result(target.uid)
        if not loops:
            obj = bpy.data.objects.get(target.source_object)
            if obj:
                loops, _open_count = _target_sections(obj, target)
        if loops and len(loops) > 1:
            loops = [min(loops, key=lambda loop: _point_loop_distance_squared(_target_anchor(target), loop))]
    _set_runtime_target(target, loops)
    if context and context.area:
        context.area.tag_redraw()


def _keep_results_updated(settings, context):
    if settings.keep_result:
        for target in settings.targets:
            _refresh_target(target, True)
    else:
        _remove_result_object()


class BFLM_Target(PropertyGroup):
    uid: StringProperty(default="")
    name: StringProperty(name="名称", default="测量目标")
    source_object: StringProperty(default="")
    current_length: FloatProperty(name="当前周长", default=0.0, precision=6, unit="LENGTH")
    length_text: StringProperty(default="尚未测量")
    loop_count: IntProperty(default=0)
    open_chain_count: IntProperty(default=0)
    valid: BoolProperty(default=True)
    target_length: FloatProperty(
        name="目标周长",
        description="调整后希望得到的截面周长",
        default=0.0,
        min=0.0,
        precision=6,
        unit="LENGTH",
    )
    falloff_radius: FloatProperty(
        name="衰减半径",
        description="围绕当前测量环线的局部影响范围；设为 0 时自动估算",
        default=0.25,
        min=0.0,
        soft_max=5.0,
        precision=4,
        unit="LENGTH",
    )
    falloff_shape: EnumProperty(
        name="衰减类型",
        items=[
            ("SMOOTH", "平滑", "自然过渡"),
            ("SPHERE", "球形", "球状衰减"),
            ("ROOT", "平方根", "较宽的过渡"),
            ("SHARP", "锐利", "较集中的过渡"),
            ("LINEAR", "线性", "线性衰减"),
            ("CONSTANT", "恒定", "半径内均匀影响"),
        ],
        default="SMOOTH",
    )
    lock_normal: BoolProperty(
        name="锁定截面法线",
        description="只在测量截面平面内放大/缩小围径，不沿手指轴向拉伸",
        default=True,
    )
    use_connected: BoolProperty(
        name="仅影响相连项",
        description="沿原网格边的路径距离计算衰减，避免跨指缝影响邻指；半径过大仍可经掌部传播",
        default=True,
    )
    selection_anchor: FloatVectorProperty(size=3, subtype="XYZ")
    has_selection_anchor: BoolProperty(default=False)
    plane_point: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0), subtype="XYZ")
    plane_normal: FloatVectorProperty(size=3, default=(0.0, 0.0, 1.0), subtype="DIRECTION")


class BFLM_Settings(PropertyGroup):
    targets: CollectionProperty(type=BFLM_Target)
    active_index: IntProperty(default=0, update=_active_target_updated)
    keep_result: BoolProperty(name="保留结果对象", default=True, update=_keep_results_updated)


class BFLM_OT_measure(Operator):
    bl_idname = "bflm.measure_loop"
    bl_label = "开始环线测量"
    bl_description = "在视图中拖拽一条穿过模型的屏幕线段"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == "VIEW_3D" and context.mode == "OBJECT"
                and context.active_object and context.active_object.type == "MESH"
                and _RUNTIME["operator"] is None)

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or obj.type != "MESH":
            self.report({"ERROR"}, "请先选中一个网格模型")
            return {"CANCELLED"}
        self.obj = obj
        self.start = None
        self.current = None
        self.area = context.area
        self.region = context.region if context.region.type == "WINDOW" else next(
            (region for region in context.area.regions if region.type == "WINDOW"), None
        )
        if self.region is None:
            self.report({"ERROR"}, "请在 3D 视图中启动测量")
            return {"CANCELLED"}
        _RUNTIME["operator"] = self
        _RUNTIME["region"] = self.region
        _RUNTIME["start"] = None
        _RUNTIME["current"] = None
        if _RUNTIME["stroke_handler"] is None:
            _RUNTIME["stroke_handler"] = bpy.types.SpaceView3D.draw_handler_add(
                _draw_stroke, (), "WINDOW", "POST_PIXEL"
            )
        if _RUNTIME["result_handler"] is None:
            _RUNTIME["result_handler"] = bpy.types.SpaceView3D.draw_handler_add(
                _draw_result, (), "WINDOW", "POST_VIEW"
            )
            _RUNTIME["label_handler"] = bpy.types.SpaceView3D.draw_handler_add(
                _draw_label, (), "WINDOW", "POST_PIXEL"
            )
        context.window_manager.modal_handler_add(self)
        self.area.header_text_set("ZLM：横划目标手指，让线段中点落在该手指上；右键 / Esc 取消")
        self.report({"INFO"}, "横划目标手指，让线段中点落在该手指上")
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        if not self.start or not self.current:
            return False
        if (self.current - self.start).length < 8.0:
            self.report({"WARNING"}, "线段太短，请拖拽一条穿过模型的线段")
            return False
        surface = _surface_point(context, self.obj, self.start, self.current)
        if surface is None:
            self.report({"ERROR"}, "线段没有投射到当前模型")
            return False
        _plane_point, plane_normal = _stroke_plane(context, self.start, self.current, surface)
        loops, open_chain_count = _extract_loops(self.obj, surface, plane_normal, anchor=surface)
        if not loops:
            self.report({"ERROR"}, "没有找到可测量截面，请让线段穿过模型表面")
            return False
        length = _loop_length(loops[0])
        settings = context.scene.bflm_settings
        target = settings.targets.add()
        target.uid = uuid.uuid4().hex
        target.name = f"测量 {len(settings.targets):02d}"
        target.source_object = self.obj.name
        target.current_length = length
        target.length_text = _format_length(length)
        target.target_length = length
        target.loop_count = len(loops)
        target.open_chain_count = open_chain_count
        target.plane_point = tuple(surface)
        target.plane_normal = tuple(plane_normal)
        target.selection_anchor = loops[0][0]
        target.has_selection_anchor = True
        target.use_connected = True
        target.valid = True
        _RUNTIME["loop_cache"][target.uid] = loops
        settings.active_index = len(settings.targets) - 1
        _set_runtime_target(target, loops)
        if settings.keep_result:
            _create_result_object(loops, self.obj, target.uid, target.name)
        if open_chain_count:
            self.report(
                {"INFO"},
                f"已新增 {target.name}：{target.length_text}（含 {open_chain_count} 条开口交线）",
            )
        else:
            self.report({"INFO"}, f"已新增 {target.name}：{target.length_text}")
        return True

    def _stop(self):
        self.start = self.current = None
        _RUNTIME["start"] = _RUNTIME["current"] = None
        _RUNTIME["operator"] = None
        self.area.header_text_set(None)
        self.area.tag_redraw()

    def cancel(self, context):
        self._stop()

    def modal(self, context, event):
        coord = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not (0 <= coord.x < self.region.width and 0 <= coord.y < self.region.height):
                return {"RUNNING_MODAL"}
            self.start = coord
            self.current = self.start.copy()
            _RUNTIME["start"] = self.start
            _RUNTIME["current"] = self.current
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE" and self.start:
            self.current = coord
            _RUNTIME["current"] = self.current
            self.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self.start:
            self.current = coord
            _RUNTIME["current"] = self.current
            try:
                with context.temp_override(area=self.area, region=self.region):
                    success = self._finish(context)
            except (ValueError, RuntimeError) as error:
                self.report({"ERROR"}, str(error))
                success = False
            finally:
                self._stop()
            return {"FINISHED"} if success else {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._stop()
            return {"CANCELLED"}
        if not self.start and event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}


class BFLM_OT_resize_loop(Operator):
    bl_idname = "bflm.resize_loop"
    bl_label = "按目标周长调整"
    bl_description = "只移动现有顶点，按衰减范围局部调整最近一次测量环线的围径"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "bflm_settings", None)
        target = _active_target(settings) if settings else None
        obj = bpy.data.objects.get(target.source_object) if target else None
        return context.mode == "OBJECT" and target is not None and obj is not None and target.valid

    def execute(self, context):
        settings = context.scene.bflm_settings
        measurement = _active_target(settings)
        if measurement is None:
            self.report({"ERROR"}, "请先新增一个测量目标")
            return {"CANCELLED"}
        obj = bpy.data.objects.get(measurement.source_object)
        if not obj or obj.type != "MESH":
            self.report({"ERROR"}, "当前测量目标的模型已不存在")
            return {"CANCELLED"}
        if obj.mode != "OBJECT":
            self.report({"ERROR"}, "请切换到对象模式后再调整围径")
            return {"CANCELLED"}
        desired_length = measurement.target_length
        if desired_length <= 0.0:
            self.report({"ERROR"}, "目标周长必须大于 0")
            return {"CANCELLED"}

        plane_normal = Vector(measurement.plane_normal)
        if plane_normal.length < 1.0e-8:
            self.report({"ERROR"}, "测量截面方向无效，请重新画线")
            return {"CANCELLED"}
        plane_normal.normalize()

        # A rejected connected edit must leave the mesh exactly as it was.
        original_mesh = obj.data
        before = [vertex.co.copy() for vertex in original_mesh.vertices]
        if original_mesh.users > 1:
            obj.data = original_mesh.copy()
        try:
            effective_radius = _resize_target_geometry(context, obj, measurement)
        except (ValueError, RuntimeError) as error:
            if obj.data != original_mesh:
                copied = obj.data
                obj.data = original_mesh
                bpy.data.meshes.remove(copied)
            else:
                for vertex, coordinate in zip(obj.data.vertices, before):
                    vertex.co = coordinate
                obj.data.update()
            context.view_layer.update()
            self.report({"WARNING"}, f"调整已取消并恢复：{error}")
            return {"CANCELLED"}

        active_loops = []
        for item in settings.targets:
            if item.source_object != obj.name:
                continue
            refreshed = _refresh_target(item, settings.keep_result)
            if item.uid == measurement.uid:
                active_loops = refreshed
        if not active_loops:
            self.report({"ERROR"}, "调整后无法重新计算截面")
            return {"CANCELLED"}
        _set_runtime_target(measurement, active_loops)
        self.report(
            {"INFO"},
            f"{measurement.name} 已调整为 {measurement.length_text}（衰减半径 {effective_radius:.4g}）",
        )
        return {"FINISHED"}


class BFLM_OT_remove_target(Operator):
    bl_idname = "bflm.remove_target"
    bl_label = "删除当前测量目标"
    bl_description = "删除列表中当前选中的测量目标及其结果曲线"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "bflm_settings", None)
        return settings is not None and len(settings.targets) > 0

    def execute(self, context):
        settings = context.scene.bflm_settings
        index = max(0, min(settings.active_index, len(settings.targets) - 1))
        target_uid = settings.targets[index].uid
        _remove_result_object(target_uid)
        _RUNTIME["loop_cache"].pop(target_uid, None)
        settings.targets.remove(index)
        settings.active_index = min(index, len(settings.targets) - 1) if settings.targets else 0
        target = _active_target(settings)
        _set_runtime_target(target, _loops_from_result(target.uid) if target else None)
        return {"FINISHED"}


class BFLM_OT_clear_all(Operator):
    bl_idname = "bflm.clear_all_targets"
    bl_label = "清空全部测量目标"
    bl_description = "删除全部测量目标及其结果曲线"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "bflm_settings", None)
        return settings is not None and len(settings.targets) > 0

    def execute(self, context):
        settings = context.scene.bflm_settings
        _remove_result_object()
        settings.targets.clear()
        _RUNTIME["loop_cache"].clear()
        settings.active_index = 0
        _set_runtime_target(None)
        return {"FINISHED"}


class BFLM_OT_select_source(Operator):
    bl_idname = "bflm.select_target_source"
    bl_label = "选择目标模型"
    bl_description = "在场景中选中当前测量目标对应的模型"

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "bflm_settings", None)
        target = _active_target(settings) if settings else None
        return (
            context.mode == "OBJECT"
            and target is not None
            and bpy.data.objects.get(target.source_object) is not None
        )

    def execute(self, context):
        target = _active_target(context.scene.bflm_settings)
        obj = bpy.data.objects.get(target.source_object)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class BFLM_UL_targets(UIList):
    def draw_item(
        self, context, layout, data, item, icon, active_data, active_propname, index
    ):
        target = item
        row = layout.row(align=True)
        status_icon = "CURVE_DATA" if target.valid else "ERROR"
        row.prop(target, "name", text="", emboss=False, icon=status_icon)
        value = target.length_text if target.valid else "失效"
        row.label(text=value)


class BFLM_OT_verify_integrity(Operator):
    bl_idname = "bflm.verify_integrity"
    bl_label = "重新校验插件完整性"
    bl_description = "重新计算插件主程序和扩展清单的 SHA-256 摘要"

    def execute(self, context):
        valid = _verify_integrity()
        if valid:
            self.report({"INFO"}, _INTEGRITY_MESSAGE)
        else:
            self.report({"WARNING"}, _INTEGRITY_MESSAGE)
        for area in context.screen.areas if context.screen else ():
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"FINISHED"}


class BFLM_PT_panel(Panel):
    bl_label = "ZLM"
    bl_idname = "BFLM_PT_loop_measure"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ZLM"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bflm_settings
        layout.use_property_split = True
        layout.use_property_decorate = False

        if _INTEGRITY_STATUS == "FAILED":
            warning = layout.box()
            warning.alert = True
            warning.label(text="插件文件完整性异常", icon="ERROR")
            warning.operator("bflm.verify_integrity", text="立即复检", icon="FILE_REFRESH")

        measure_row = layout.row()
        measure_row.scale_y = 1.35
        measure_row.operator("bflm.measure_loop", text="新增测量目标", icon="DRIVER_DISTANCE")

        list_box = layout.box()
        header = list_box.row()
        header.label(text=f"测量目标  {len(settings.targets)}", icon="PRESET")
        list_row = list_box.row()
        list_row.template_list(
            "BFLM_UL_targets",
            "",
            settings,
            "targets",
            settings,
            "active_index",
            rows=3,
        )
        list_tools = list_row.column(align=True)
        list_tools.operator("bflm.select_target_source", text="", icon="RESTRICT_SELECT_OFF")
        list_tools.operator("bflm.remove_target", text="", icon="REMOVE")
        list_tools.separator()
        list_tools.operator("bflm.clear_all_targets", text="", icon="TRASH")

        target = _active_target(settings)
        if target is None:
            empty_box = layout.box()
            empty_box.label(text="在模型上画线以新增第一个目标", icon="INFO")
            options_row = layout.row()
            options_row.prop(settings, "keep_result", text="保留结果曲线")
            return

        source = bpy.data.objects.get(target.source_object)
        status_box = layout.box()
        status_header = status_box.row(align=True)
        status_header.prop(target, "name", text="", icon="CURVE_DATA")
        status_header.label(text=target.length_text)
        if source:
            status_box.label(text=source.name, icon="OBJECT_DATA")
        else:
            status_box.label(text="原模型已不存在", icon="ERROR")
        meta_row = status_box.row(align=True)
        meta_row.label(text=f"{target.loop_count} 个截面环")
        if target.open_chain_count:
            meta_row.label(text=f"{target.open_chain_count} 个开口已补齐", icon="LINKED")

        edit_box = layout.box()
        edit_header = edit_box.row()
        edit_header.label(text="局部围径调整", icon="MODIFIER")
        edit_box.enabled = target.valid and source is not None
        edit_box.prop(target, "target_length")
        edit_box.prop(target, "falloff_radius", slider=True)
        edit_box.prop(target, "falloff_shape", text="衰减")
        edit_box.prop(target, "use_connected")
        if target.use_connected:
            edit_box.label(text="沿网格连接衰减", icon="LINKED")
        edit_box.prop(target, "lock_normal")
        resize_row = edit_box.row()
        resize_row.scale_y = 1.25
        resize_row.operator("bflm.resize_loop", text="调整当前目标", icon="ARROW_LEFTRIGHT")

        options_box = layout.box()
        options_box.label(text="结果", icon="OUTLINER_OB_CURVE")
        options_box.prop(settings, "keep_result", text="保留结果曲线")


class BFLM_PT_about(Panel):
    bl_label = "关于与版权"
    bl_idname = "BFLM_PT_about"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ZLM"
    bl_parent_id = "BFLM_PT_loop_measure"

    def draw(self, context):
        layout = self.layout
        identity = layout.box()
        info = identity.column(align=True)
        info.label(text="ZLM")
        info.label(text="围径测量与局部调整")
        info.label(text="Yining Zi", icon="USER")
        info.label(text="版本 " + ".".join(map(str, ZLM_VERSION)), icon="INFO")

        legal = layout.box()
        legal.label(text="知识产权", icon="FILE_TEXT")
        legal.label(text="版权所有 © 2026 Yining Zi")
        legal.label(text="插件著作权归 Yining Zi 所有")
        legal.separator()
        legal.label(text="许可证：GPL-3.0-or-later")
        legal.label(text="使用与分发须遵守许可证")
        legal.label(text="并保留本版权声明")

        integrity = layout.box()
        integrity.label(text="完整性保护", icon="LOCKED")
        status_icon = "CHECKMARK" if _INTEGRITY_STATUS == "OK" else "ERROR"
        integrity.label(text=_INTEGRITY_MESSAGE, icon=status_icon)
        integrity.operator("bflm.verify_integrity", text="重新校验", icon="FILE_REFRESH")


classes = (
    BFLM_Target,
    BFLM_Settings,
    BFLM_OT_measure,
    BFLM_OT_resize_loop,
    BFLM_OT_remove_target,
    BFLM_OT_clear_all,
    BFLM_OT_select_source,
    BFLM_OT_verify_integrity,
    BFLM_UL_targets,
    BFLM_PT_panel,
    BFLM_PT_about,
)


def register():
    _verify_integrity()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bflm_settings = bpy.props.PointerProperty(type=BFLM_Settings)


def unregister():
    for key in ("stroke_handler", "result_handler", "label_handler"):
        handler = _RUNTIME.get(key)
        if handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handler, "WINDOW")
            _RUNTIME[key] = None
    _remove_result_object()
    del bpy.types.Scene.bflm_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
