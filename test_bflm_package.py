import importlib.util
import shutil
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import bpy


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingLayout:
    def __init__(self):
        self.labels = []

    def box(self):
        return self

    def column(self, **kwargs):
        return self

    def label(self, *, text, **kwargs):
        self.labels.append(text)

    def separator(self):
        pass

    def operator(self, *args, **kwargs):
        pass


package_root = Path(__file__).with_name("build") / "zlm"
addon = load_module(package_root / "__init__.py", "zlm_package_test")
with (package_root / "blender_manifest.toml").open("rb") as manifest_file:
    manifest = tomllib.load(manifest_file)
assert ".".join(map(str, addon.bl_info["version"])) == manifest["version"]
assert addon.ZLM_VERSION == addon.bl_info["version"]
assert addon._verify_integrity()
assert addon._INTEGRITY_STATUS == "OK"
# Blender's extension loader removes legacy metadata before registration.
del addon.bl_info
addon.register()
try:
    layout = RecordingLayout()
    addon.BFLM_PT_about.draw(SimpleNamespace(layout=layout), bpy.context)
    assert "版本 " + manifest["version"] in layout.labels
    assert "Yining Zi" in layout.labels
    assert addon._INTEGRITY_MESSAGE in layout.labels
    assert addon._INTEGRITY_STATUS == "OK"
finally:
    addon.unregister()
print("extension about panel without bl_info: ok")

with tempfile.TemporaryDirectory(prefix="zlm_integrity_test_") as temp_dir:
    tampered_root = Path(temp_dir) / "zlm"
    shutil.copytree(package_root, tampered_root)
    with (tampered_root / "__init__.py").open("ab") as source:
        source.write(b"# tampered")
    tampered = load_module(tampered_root / "__init__.py", "zlm_tamper_test")
    assert not tampered._verify_integrity()
    assert tampered._INTEGRITY_STATUS == "FAILED"
    assert "__init__.py" in tampered._INTEGRITY_MESSAGE

print("package integrity protection: ok")
