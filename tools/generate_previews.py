"""Reproducible examples of all three dashboard states (illustrative data)."""
import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from renderer.app import collect, render, validate

cfg = validate(json.loads((ROOT / 'config.example.json').read_text()))
for name, hour, minute, empty in [
    ('preview-future', 7, 30, False),
    ('preview-active', 8, 30, False),
    ('preview-empty', 9, 30, True),
]:
    now = datetime(2026, 10, 8, hour, minute, tzinfo=ZoneInfo(cfg['timezone']))
    events, values, errors = collect(cfg, now, True)
    image = render(cfg, now, [] if empty else events, values, errors, True)
    destination = ROOT / 'output' / (name + '.png')
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    print(destination)
