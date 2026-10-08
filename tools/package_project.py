"""Export complete source listing and project archive without local credentials."""
from pathlib import Path
import zipfile
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
core = [
    'renderer/app.py', 'renderer/__init__.py', 'config.example.json',
    'firmware/src/main.cpp', 'firmware/platformio.ini',
    'firmware/include/secrets.example.h', 'addon/eink_frame/config.yaml',
    'addon/eink_frame/Dockerfile', 'addon/eink_frame/entrypoint.py',
    'requirements.txt', 'requirements-dev.txt', 'Dockerfile', 'compose.yaml',
    '.env.example', '.gitignore', '.dockerignore', 'tools/package_addon.py',
    'tools/generate_previews.py', 'tools/package_project.py', 'tests/test_renderer.py',
]
docs = ['README.md', 'docs/zadani.md', 'docs/mereni.md', 'docs/protokol.md',
        'addon/eink_frame/DOCS.md']
previews = ['output/preview-future.png', 'output/preview-active.png', 'output/preview-empty.png']
listing = ['# Kompletní zdrojový kód e-ink rámečku\n',
           'Snapshot aktuálních zdrojů. Wi-Fi a tokeny doplň lokálně podle secrets.example.h.\n',
           'Instalace, nákup, zapojení a ověření jsou v docs/zadani.md.\n']
for filename in core:
    suffix = Path(filename).suffix
    language = {'.py': 'python', '.cpp': 'cpp', '.h': 'cpp', '.json': 'json',
                '.yaml': 'yaml', '.ini': 'ini'}.get(suffix, 'text')
    listing.append(f'\n## {filename}\n\n```{language}\n{(ROOT / filename).read_text().rstrip()}\n```\n')
(OUT / 'cely-kod.md').write_text('\n'.join(listing))
with zipfile.ZipFile(OUT / 'eink-frame-komplet.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for filename in core + docs + previews + ['output/cely-kod.md']:
        archive.write(ROOT / filename, 'eink-frame/' + filename)
print(OUT / 'cely-kod.md')
print(OUT / 'eink-frame-komplet.zip')
