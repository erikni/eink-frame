"""Generate reproducible future, ongoing and empty-calendar screen previews."""

import json
import sys
from datetime import datetime
from importlib import import_module
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = (
    ("preview-future", 7, 30, False),
    ("preview-active", 8, 30, False),
    ("preview-empty", 9, 30, True),
)


def main() -> None:
    """Render fixed demonstration times so layout changes can be compared exactly."""
    # Direct script execution adds tools/, not the repository root, to sys.path.
    # Import after adding the root so this also works without an installed package.
    sys.path.insert(0, str(ROOT))
    renderer = import_module("renderer.app")
    config = renderer.validate(
        json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    )
    for name, hour, minute, empty in SCENARIOS:
        now = datetime(2026, 10, 8, hour, minute, tzinfo=ZoneInfo(config["timezone"]))
        data = renderer.collect_dashboard(config, now, True, empty)
        image = renderer.render(config, now, data, True)
        destination = ROOT / "output" / (name + ".png")
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        print(destination)


if __name__ == "__main__":
    main()
