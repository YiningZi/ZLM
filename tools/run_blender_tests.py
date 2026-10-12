"""Run each regression in a fresh factory-default Blender process."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TESTS = ('test_bflm_blender.py', 'test_zlm_fingers.py', 'test_zlm_stroke.py',
         'test_zlm_compatibility.py', 'test_bflm_package.py')
parser = argparse.ArgumentParser()
parser.add_argument('--blender', default='blender', help='Blender executable path')
args = parser.parse_args()
if not (ROOT / 'build/zlm/integrity.json').is_file():
    raise SystemExit('Run python tools/build.py before running Blender tests')
for script in TESTS:
    print(f'Running {script}', flush=True)
    subprocess.run([args.blender, '--background', '--factory-startup',
                    '--python-exit-code', '1', '--python', str(ROOT / script)],
                   cwd=ROOT, check=True, timeout=300)
print(f'All {len(TESTS)} Blender regression suites passed.', flush=True)
