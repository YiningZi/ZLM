"""Version, registration, UI API and scene-unit checks on Blender 4.4+."""
import ast
import importlib.util
from pathlib import Path

import bpy

source = Path(__file__).with_name("bflm_loop_measure.py")
spec = importlib.util.spec_from_file_location("zlm_compatibility_test", source)
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
assert addon.bl_info["blender"] == (4, 4, 0)
addon.register()
try:
    assert bpy.context.scene.bflm_settings.targets.add().use_connected
    units = bpy.context.scene.unit_settings
    units.system = "METRIC"
    units.scale_length = 0.001
    label = addon._format_length(50.0)
    assert "BU" not in label
    assert abs(bpy.utils.units.to_value("METRIC", "LENGTH", label) - 0.05) < 1e-6, label
    units.scale_length = 0.01
    label = addon._format_length(50.0)
    assert abs(bpy.utils.units.to_value("METRIC", "LENGTH", label) - 0.5) < 1e-6, label
    units.system = "IMPERIAL"
    units.scale_length = 0.0254
    label = addon._format_length(2.0)
    assert abs(bpy.utils.units.to_value("IMPERIAL", "LENGTH", label) - 0.0508) < 1e-5, label
    units.system = "NONE"
    assert addon._format_length(50.0) == "50.000 BU"

    # Check every literal sidebar icon against this Blender build's RNA enum.
    tree = ast.parse(source.read_text(encoding="utf-8"))
    known_icons = set(bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "icon" and isinstance(node.value, ast.Constant):
            assert node.value.value in known_icons, node.value.value
    for name in ("label", "prop", "operator", "template_list", "box", "row", "column"):
        assert name in bpy.types.UILayout.bl_rna.functions
    assert hasattr(bpy.context, "temp_override")
    assert hasattr(bpy.types, "BFLM_PT_about")
    assert not hasattr(addon, "_AUTHOR_IMAGE")
finally:
    addon.unregister()
assert not hasattr(bpy.types.Scene, "bflm_settings")
print("ZLM compatibility, units and UI API: PASSED on", bpy.app.version_string)
