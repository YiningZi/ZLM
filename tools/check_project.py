"""Validate source syntax, synchronized versions and release documentation."""
import argparse
from pathlib import Path
import re

from build import ROOT, metadata

parser = argparse.ArgumentParser()
parser.add_argument('--tag', help='Require a vX.Y.Z tag matching the source')
args = parser.parse_args()
manifest = metadata()
version = manifest['version']
if args.tag and args.tag != f'v{version}':
    raise SystemExit(f'Tag {args.tag!r} does not match source v{version}')
for path in [ROOT / 'bflm_loop_measure.py', *ROOT.glob('test_*.py'), *(ROOT / 'tools').glob('*.py')]:
    compile(path.read_text(encoding='utf-8'), path.name, 'exec')
changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
if not re.search(r'^## ' + re.escape(version) + r'\b', changelog, re.MULTILINE):
    raise SystemExit('Current version is missing from CHANGELOG.md')
notes = ROOT / 'docs' / 'releases' / f'v{version}.md'
if not notes.is_file() or f'ZLM {version}' not in notes.read_text(encoding='utf-8'):
    raise SystemExit('Current release notes are missing')
if 'GNU GENERAL PUBLIC LICENSE' not in (ROOT / 'LICENSE').read_text(encoding='utf-8'):
    raise SystemExit('GPL license text is missing')
print(f'Project metadata, Python syntax and release notes: OK (v{version})')
