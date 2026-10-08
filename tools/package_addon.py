"""Prepare a self-contained local HA app without maintaining duplicate source."""
from pathlib import Path
import shutil
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'output' / 'local-addon' / 'eink_frame'
DEST.mkdir(parents=True, exist_ok=True)
for name in ('config.yaml', 'Dockerfile', 'entrypoint.py', 'DOCS.md'):
    shutil.copy2(ROOT / 'addon' / 'eink_frame' / name, DEST / name)
shutil.copy2(ROOT / 'requirements.txt', DEST / 'requirements.txt')
shutil.copytree(ROOT / 'renderer', DEST / 'renderer', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__'))
print(DEST)
