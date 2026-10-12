"""Build a reproducible Blender extension using Python 3.11+, no dependencies."""
import ast
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def metadata():
    manifest = tomllib.loads((ROOT / 'blender_manifest.toml').read_text(encoding='utf-8'))
    tree = ast.parse((ROOT / 'bflm_loop_measure.py').read_text(encoding='utf-8'))
    values = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in ('bl_info', 'ZLM_VERSION'):
                    values[target.id] = ast.literal_eval(statement.value)
    version = tuple(map(int, manifest['version'].split('.')))
    if values.get('ZLM_VERSION') != version or values['bl_info']['version'] != version:
        raise ValueError('Version mismatch: manifest, bl_info and ZLM_VERSION must agree')
    if values['bl_info']['blender'] != tuple(map(int, manifest['blender_version_min'].split('.'))):
        raise ValueError('Minimum Blender versions must agree')
    if manifest['id'] != 'zlm' or manifest['license'] != ['SPDX:GPL-3.0-or-later']:
        raise ValueError('Unexpected extension ID or license')
    return manifest


def build():
    manifest = metadata()
    package = ROOT / 'build' / 'zlm'
    package.mkdir(parents=True, exist_ok=True)
    payloads = {}
    for source, target in [('bflm_loop_measure.py', '__init__.py'),
                           ('blender_manifest.toml', 'blender_manifest.toml'),
                           ('LICENSE', 'LICENSE')]:
        # Normalize newlines so Windows and Linux build the same source bytes.
        payloads[target] = (ROOT / source).read_text(encoding='utf-8').encode('utf-8')
    integrity = {'schema_version': 1, 'algorithm': 'SHA-256',
                 'files': {name: hashlib.sha256(data).hexdigest()
                           for name, data in sorted(payloads.items())}}
    payloads['integrity.json'] = (json.dumps(integrity, indent=2, sort_keys=True) + '\n').encode()
    for name, data in payloads.items():
        (package / name).write_bytes(data)
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    archive = destination / f"ZLM-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    with zipfile.ZipFile(archive) as check:
        if set(check.namelist()) != set(payloads) or check.testzip() is not None:
            raise ValueError('Invalid package structure or CRC')
        for name, expected in integrity['files'].items():
            if hashlib.sha256(check.read(name)).hexdigest() != expected:
                raise ValueError(f'Integrity mismatch: {name}')
    print(archive)
    print(f'SHA256 {digest}')
    return archive


if __name__ == '__main__':
    build()
