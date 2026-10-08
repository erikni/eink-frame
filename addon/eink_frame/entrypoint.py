"""Translate HA OS app options and authenticate with the Supervisor API proxy."""

import json
import os
from pathlib import Path
from typing import Any

from renderer.app import Dashboard, serve, validate


def config(options: dict[str, Any]) -> dict[str, Any]:
    """Build the same renderer configuration used by standalone installations."""
    return validate(
        {
            "timezone": options["timezone"],
            "calendar_entities": options["calendar_entities"],
            "agenda_days": options["agenda_days"],
            "empty_calendar_quote_entity": options["empty_calendar_quote_entity"],
            "empty_calendar_author_entity": options["empty_calendar_author_entity"],
            "metrics": [
                {
                    "entity": options["outdoor_entity"],
                    "role": "outdoor",
                    "label": "VENKU",
                    "unit": "°C",
                },
                {
                    "entity": options["indoor_entity"],
                    "role": "indoor",
                    "label": "DOMA",
                    "unit": "°C",
                },
                {
                    "entity": options["max_temperature_entity"],
                    "role": "outdoor_max",
                    "label": "MAX DNES",
                    "unit": "°C",
                },
            ],
            "schedule": {
                key: options[key]
                for key in ("day_start", "night_start", "day_minutes", "night_minutes")
            },
        }
    )


def main() -> None:
    """Read Supervisor-managed options, keep HA credentials local and serve images."""
    options = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
    token = options["frame_token"]
    if len(token) < 24:
        raise ValueError("frame_token musí mít alespoň 24 znaků")
    os.environ["FRAME_TOKEN"] = token
    os.environ["HA_URL"] = "http://supervisor/core"
    # ESP32 receives only FRAME_TOKEN; this token must stay inside the HA OS app.
    os.environ["HA_TOKEN"] = os.environ["SUPERVISOR_TOKEN"]
    dashboard = Dashboard(
        config(options), options["demo"], options.get("demo_empty_calendar", False)
    )
    serve(dashboard, "0.0.0.0", 8080)


if __name__ == "__main__":
    main()
