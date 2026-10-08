# Kompletní zdrojový kód e-ink rámečku

Snapshot aktuálních zdrojů. Wi-Fi a tokeny doplň lokálně podle secrets.example.h.

Instalace, nákup, zapojení a ověření jsou v docs/zadani.md.


## renderer/app.py

```python
"""Build a Czech e-ink dashboard and serve authenticated images to ESP32.

Home Assistant credentials stay on the server. The device receives only the
finished image and the number of seconds until its next scheduled wake-up.
"""

import argparse
import hmac
import io
import json
import logging
import os
import struct
import threading
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 800, 480
RED = (190, 0, 0)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
MAIN_X, MAIN_WIDTH = 224, 544
RAIL_WIDTH = 192
CALENDAR_ERROR = "Kalendář není dostupný"
METRIC_ERROR = "Některé hodnoty nejsou dostupné"
DEFAULT_QUOTE = "Můžeme dělat malé věci s velkou láskou."
DEFAULT_AUTHOR = "Matka Tereza"
OUTDOOR_ENTITY = "input_number.outdoor_temperature"
MAX_TEMPERATURE_ENTITY = "input_number.outdoor_effective_temperature"
LOGGER = logging.getLogger(__name__)
MONTHS = (
    "ledna",
    "února",
    "března",
    "dubna",
    "května",
    "června",
    "července",
    "srpna",
    "září",
    "října",
    "listopadu",
    "prosince",
)
DAYS = ("Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle")

Configuration: TypeAlias = dict[str, Any]
# urllib, JSON decoding and malformed HA payloads have distinct failure types.
# Catch these at the data-source boundary; programming errors must remain visible.
DATA_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


class Event(TypedDict):
    """A calendar event whose start/end are aware datetimes in the display zone."""

    summary: str
    start: datetime
    end: datetime
    all_day: bool


@dataclass
class DashboardData:
    """Calendar entries, formatted sensor values and user-visible source errors."""

    events: list[Event]
    values: list[str]
    errors: list[str]


@dataclass
class RenderContext:
    """Shared drawing inputs so individual layout sections stay small."""

    draw: ImageDraw.ImageDraw
    config: Configuration
    now: datetime
    data: DashboardData
    demo: bool


def validate(config: Configuration) -> Configuration:
    """Validate supported schedule bounds, timezone and dashboard capacity."""
    ZoneInfo(config["timezone"])
    schedule = config["schedule"]
    if not 0 <= schedule["day_start"] < schedule["night_start"] <= 23:
        raise ValueError("day_start musí být před night_start (0–23)")
    for key in ("day_minutes", "night_minutes"):
        if not 1 <= schedule[key] <= 1440:
            raise ValueError("Interval musí být 1–1440 minut")
    if not 1 <= config["agenda_days"] <= 7 or len(config["metrics"]) > 3:
        raise ValueError("Agenda 1–7 dní, nejvýše 3 hodnoty")
    return config


def _scheduled_slots(now: datetime, schedule: dict[str, int]) -> Iterator[float]:
    """Yield real future timestamps for local slots, including both DST folds."""
    for day_offset in range(3):
        day = now.date() + timedelta(days=day_offset)
        for minute in range(1440):
            hour = minute // 60
            daytime = schedule["day_start"] <= hour < schedule["night_start"]
            interval = schedule["day_minutes"] if daytime else schedule["night_minutes"]
            boundary = minute in (
                schedule["day_start"] * 60,
                schedule["night_start"] * 60,
            )
            if minute % interval != 0 and not boundary:
                continue
            wall = datetime.combine(day, time(hour, minute % 60), now.tzinfo)
            for fold in (0, 1):
                stamp = wall.replace(fold=fold).timestamp()
                back = datetime.fromtimestamp(stamp, now.tzinfo)
                # A round trip rejects local times skipped by the spring DST jump.
                if (
                    back.hour == hour
                    and back.minute == minute % 60
                    and stamp > now.timestamp()
                ):
                    yield stamp


def next_wake(
    now: datetime, config: Configuration, events: Sequence[Event] = ()
) -> int:
    """Return seconds until a schedule slot or event boundary, clamped to 60–86400."""
    candidates = list(_scheduled_slots(now, config["schedule"]))
    for event in events:
        for key in ("start", "end"):
            stamp = event[key].timestamp()
            if stamp > now.timestamp():
                candidates.append(stamp)
    # Timestamp subtraction, rather than wall-time subtraction, handles DST.
    return max(60, min(86400, int(min(candidates) - now.timestamp())))


def parse_time(value: str, zone: tzinfo) -> datetime:
    """Interpret HA date/dateTime values in the configured local timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=zone)
        if parsed.tzinfo is None
        else parsed.astimezone(zone)
    )


def api(path: str) -> Any:
    """Fetch one HA REST resource using server-only credentials and a timeout."""
    base = os.environ["HA_URL"].rstrip("/")
    if urlparse(base).scheme not in ("http", "https"):
        raise ValueError("HA_URL musí začínat http:// nebo https://")
    request = Request(
        base + path, headers={"Authorization": "Bearer " + os.environ["HA_TOKEN"]}
    )
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def visible_events(events: Sequence[Event], now: datetime) -> list[Event]:
    """Hide events at their end instant and sort remaining events by start."""
    return sorted(
        (event for event in events if event["end"].timestamp() > now.timestamp()),
        key=lambda event: event["start"].timestamp(),
    )


def is_running(event: Event, now: datetime) -> bool:
    """Use an inclusive start and exclusive end, including for all-day events."""
    return event["start"].timestamp() <= now.timestamp() < event["end"].timestamp()


def _demo_events(now: datetime) -> list[Event]:
    """Return the example agenda used by all reproducible screen previews."""
    tomorrow = now.date() + timedelta(days=1)
    return [
        {
            "summary": "Rodinná snídaně",
            "start": now.replace(hour=8, minute=0, second=0),
            "end": now.replace(hour=9, minute=0, second=0),
            "all_day": False,
        },
        {
            "summary": "Nákup a vyzvednutí zásilky",
            "start": now.replace(hour=17, minute=30, second=0),
            "end": now.replace(hour=18, minute=0, second=0),
            "all_day": False,
        },
        {
            "summary": "Výlet s rodinou",
            "start": datetime.combine(tomorrow, time(9), now.tzinfo),
            "end": datetime.combine(tomorrow, time(15), now.tzinfo),
            "all_day": False,
        },
    ]


def _calendar_events(
    config: Configuration, now: datetime
) -> tuple[list[Event], list[str]]:
    """Load calendars independently so an unavailable source does not stop others."""
    events: list[Event] = []
    errors: list[str] = []
    start = datetime.combine(now.date(), time(), now.tzinfo)
    end = start + timedelta(days=config["agenda_days"])
    query = urlencode({"start": start.isoformat(), "end": end.isoformat()})
    for entity in config["calendar_entities"]:
        try:
            for entry in api("/api/calendars/" + quote(entity, safe="") + "?" + query):
                begin, finish = entry["start"], entry["end"]
                events.append(
                    {
                        "summary": entry.get("summary", "Událost"),
                        "start": parse_time(
                            begin.get("dateTime", begin.get("date")), now.tzinfo
                        ),
                        "end": parse_time(
                            finish.get("dateTime", finish.get("date")), now.tzinfo
                        ),
                        "all_day": "date" in begin,
                    }
                )
        except DATA_ERRORS:
            # Display a safe message rather than exposing tokens or response bodies.
            errors.append(CALENDAR_ERROR)
    return visible_events(events, now), errors


def _metric_values(config: Configuration) -> tuple[list[str], list[str]]:
    """Format HA states with configured units; unavailable states become an em dash."""
    values, errors = [], []
    for metric in config["metrics"]:
        try:
            state = api("/api/states/" + quote(metric["entity"], safe=""))
            if state["state"] in ("unavailable", "unknown"):
                raise ValueError("Unavailable")
            unit = metric.get(
                "unit", state.get("attributes", {}).get("unit_of_measurement", "")
            )
            value = metric.get("states", {}).get(state["state"], state["state"])
            values.append((value.replace(".", ",") + " " + unit).strip())
        except DATA_ERRORS:
            values.append("—")
            errors.append(METRIC_ERROR)
    return values, errors


def collect(
    config: Configuration, now: datetime, demo: bool
) -> tuple[list[Event], list[str], list[str]]:
    """Collect calendar and sensor data, keeping source failures visible to the user."""
    if demo:
        values = ["12,4 °C", "22,1 °C", "16,8 °C"][: len(config["metrics"])]
        return visible_events(_demo_events(now), now), values, []
    events, calendar_errors = _calendar_events(config, now)
    values, metric_errors = _metric_values(config)
    return events, values, sorted(set(calendar_errors + metric_errors))


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Cache Lato font instances; FONT_DIR may override the installed font directory."""
    name = "Lato-Bold.ttf" if bold else "Lato-Regular.ttf"
    directory = Path(os.environ.get("FONT_DIR", "/usr/share/fonts/truetype/lato"))
    return ImageFont.truetype(str(directory / name), size)


def fit(
    draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.FreeTypeFont, width: int
) -> str:
    """Normalize whitespace and truncate with an ellipsis to fit a pixel width."""
    text = " ".join(str(text).split())
    if draw.textlength(text, font=text_font) <= width:
        return text
    while text and draw.textlength(text + "…", font=text_font) > width:
        text = text[:-1]
    return text + "…"


def event_title_lines(
    draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    """Wrap a title onto at most two lines, truncating remaining text on the last."""
    words = str(text).split()
    if not words:
        return ["Událost"]
    first = words.pop(0)
    while words and draw.textlength(first + " " + words[0], font=text_font) <= width:
        first += " " + words.pop(0)
    lines = [fit(draw, first, text_font, width)]
    if words:
        lines.append(fit(draw, " ".join(words), text_font, width))
    return lines


def temperature_number(value: str) -> str:
    """Normalize decimals so numerically identical outdoor values compare equal."""
    text = value.removesuffix("°C").strip().replace(",", ".")
    try:
        number = Decimal(text)
        return format(number.normalize(), "f") if number.is_finite() else "—"
    except InvalidOperation:
        return text


def metric_rows(
    config: Configuration, values: Sequence[str]
) -> list[tuple[Configuration, str]]:
    """Merge outdoor temperature and its maximum into one sidebar row."""
    pairs = list(zip(config["metrics"], values))
    maximum = next(
        (
            value
            for metric, value in pairs
            if metric["entity"] == MAX_TEMPERATURE_ENTITY
        ),
        None,
    )
    has_outdoor = any(metric["entity"] == OUTDOOR_ENTITY for metric, _ in pairs)
    rows = []
    for metric, value in pairs:
        if has_outdoor and metric["entity"] == MAX_TEMPERATURE_ENTITY:
            continue
        if metric["entity"] == OUTDOOR_ENTITY and maximum is not None:
            current, top = temperature_number(value), temperature_number(maximum)
            value = (current if current == top else current + " → " + top) + " °C"
        rows.append((metric, value))
    return rows


def _draw_sidebar(context: RenderContext) -> None:
    """Draw the permanent black date rail and combined temperature rows."""
    draw, now = context.draw, context.now
    draw.rectangle((0, 0, RAIL_WIDTH - 1, HEIGHT - 1), fill="black")
    draw.text(
        (96, 42),
        DAYS[now.weekday()].upper(),
        font=font(18, True),
        fill="white",
        anchor="mt",
    )
    draw.text((96, 88), str(now.day), font=font(104, True), fill="white", anchor="mt")
    draw.text(
        (96, 195), MONTHS[now.month - 1], font=font(28), fill="white", anchor="mt"
    )
    draw.line((24, 252, 168, 252), fill="white")
    for index, (metric, value) in enumerate(
        metric_rows(context.config, context.data.values)
    ):
        position = 279 + index * 76
        draw.text(
            (24, position),
            metric["label"],
            font=font(14, True),
            fill="white",
            anchor="lt",
        )
        size = 28
        while size > 12 and draw.textlength(value, font=font(size, True)) > 152:
            size -= 1
        draw.text(
            (24, position + 26),
            fit(draw, value, font(size, True), 152),
            font=font(size, True),
            fill="white",
            anchor="lt",
        )


def _quote_lines(draw: ImageDraw.ImageDraw, text: str) -> list[str]:
    """Wrap the configurable quote onto no more than three lines."""
    words, lines = str(text).split(), []
    while words and len(lines) < 3:
        line = words.pop(0)
        while (
            words
            and draw.textlength(line + " " + words[0], font=font(44, True))
            <= MAIN_WIDTH
        ):
            line += " " + words.pop(0)
        if len(lines) == 2 and words:
            line += " " + " ".join(words)
            words = []
        lines.append(fit(draw, line, font(44, True), MAIN_WIDTH))
    return lines


def _draw_empty_calendar(context: RenderContext) -> None:
    """Show a quote for an empty agenda and a distinct message for a failed source."""
    draw = context.draw
    if CALENDAR_ERROR in context.data.errors:
        draw.text(
            (MAIN_X, 64),
            "KALENDÁŘ NENÍ DOSTUPNÝ",
            font=font(18, True),
            fill=RED,
            anchor="lt",
        )
        lines = event_title_lines(
            draw, "Údaje se nepodařilo načíst.", font(42), MAIN_WIDTH
        )
        for index, line in enumerate(lines):
            draw.text(
                (MAIN_X, 141 + index * 53),
                line,
                font=font(42),
                fill="black",
                anchor="lt",
            )
        return
    draw.text((MAIN_X - 3, 64), "“", font=font(84, True), fill=RED, anchor="lt")
    lines = _quote_lines(
        draw, context.config.get("empty_calendar_quote", DEFAULT_QUOTE)
    )
    for index, line in enumerate(lines):
        draw.text(
            (MAIN_X, 143 + index * 55),
            line,
            font=font(44, True),
            fill="black",
            anchor="lt",
        )
    bottom = 143 + len(lines) * 55
    draw.line((MAIN_X, bottom + 20, MAIN_X + 54, bottom + 20), fill="black", width=2)
    author = context.config.get("empty_calendar_author", DEFAULT_AUTHOR)
    draw.text(
        (MAIN_X, bottom + 39),
        fit(draw, author, font(17), MAIN_WIDTH),
        font=font(17),
        fill="black",
        anchor="lt",
    )


def _event_when(event: Event, now: datetime) -> tuple[str, str]:
    """Return a relative day label and either clock time or the all-day label."""
    start = event["start"]
    if start.date() == now.date():
        label = "DNES"
    elif start.date() == now.date() + timedelta(days=1):
        label = "ZÍTRA"
    else:
        label = f"{start.day}. {MONTHS[start.month - 1]}"
    return label, "CELÝ DEN" if event["all_day"] else start.strftime("%H:%M")


def _draw_primary_event(context: RenderContext, event: Event) -> None:
    """Draw the bold main event; use a red card while the event is in progress."""
    draw = context.draw
    label, when = _event_when(event, context.now)
    active = is_running(event, context.now)
    draw.text((MAIN_X, 40), "ŠKOLNÍ AGENDA", font=font(18, True), fill=RED, anchor="lt")
    if active:
        draw.rectangle((MAIN_X - 16, 69, 784, 260), fill=RED)
    heading = ("PRÁVĚ PROBÍHÁ" if active else label) + "  /  " + when
    draw.text(
        (MAIN_X, 79),
        heading,
        font=font(18, True),
        fill="white" if active else RED,
        anchor="lt",
    )
    for index, line in enumerate(
        event_title_lines(draw, event["summary"], font(63, True), MAIN_WIDTH)
    ):
        draw.text(
            (MAIN_X, 128 + index * 66),
            line,
            font=font(63, True),
            fill="white" if active else "black",
            anchor="lt",
        )


def _draw_secondary_event(context: RenderContext, event: Event, index: int) -> None:
    """Draw one compact row, including concurrent events with a red background."""
    draw = context.draw
    position = 274 + index * 56
    active = is_running(event, context.now)
    if active:
        draw.rectangle((MAIN_X - 16, position - 2, 784, position + 49), fill=RED)
    else:
        draw.line((MAIN_X, position - 8, 768, position - 8), fill="black")
    label, when = _event_when(event, context.now)
    heading = ("PRÁVĚ PROBÍHÁ" if active else label) + "  /  " + when
    draw.text(
        (MAIN_X, position + 2),
        heading,
        font=font(12, True),
        fill="white" if active else RED,
        anchor="lt",
    )
    draw.text(
        (MAIN_X, position + 22),
        fit(draw, event["summary"], font(21), MAIN_WIDTH),
        font=font(21),
        fill="white" if active else "black",
        anchor="lt",
    )


def _draw_footer(context: RenderContext) -> None:
    """Draw safe source errors/demo status and an unpadded Czech update timestamp."""
    status = "UKÁZKOVÁ DATA" if context.demo else " · ".join(context.data.errors)
    context.draw.text(
        (MAIN_X, 468),
        fit(context.draw, status, font(10), 300),
        font=font(10),
        fill=RED,
        anchor="lt",
    )
    now = context.now
    updated = f"Aktualizace {now.day}. {now.month}. {now.hour}:{now.minute:02d}"
    context.draw.text((768, 468), updated, font=font(10), fill="black", anchor="rt")


def _quantize(image: Image.Image) -> None:
    """Remove font antialiasing so preview and bitplanes use the same three colors."""
    pixels = image.load()
    for position_y in range(HEIGHT):
        for position_x in range(WIDTH):
            red, green, blue = pixels[position_x, position_y]
            if red > green * 1.4 and red > blue * 1.4 and green < 180:
                color = RED
            else:
                color = BLACK if red + green + blue < 570 else WHITE
            pixels[position_x, position_y] = color


def render(
    config: Configuration, now: datetime, data: DashboardData, demo: bool = False
) -> Image.Image:
    """Compose the sidebar, agenda/quote and footer without modifying input data."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    visible_data = DashboardData(
        visible_events(data.events, now), data.values, data.errors
    )
    context = RenderContext(ImageDraw.Draw(image), config, now, visible_data, demo)
    _draw_sidebar(context)
    if visible_data.events:
        _draw_primary_event(context, visible_data.events[0])
        for index, event in enumerate(visible_data.events[1:4]):
            _draw_secondary_event(context, event, index)
    else:
        _draw_empty_calendar(context)
    _draw_footer(context)
    _quantize(image)
    return image


def planes(image: Image.Image) -> bytes:
    """Encode black then red planes: row-major, MSB first, active pixels are zero."""
    black = bytearray([255]) * (WIDTH * HEIGHT // 8)
    red = bytearray([255]) * (WIDTH * HEIGHT // 8)
    for index, pixel in enumerate(image.getdata()):
        target = black if pixel == BLACK else red if pixel == RED else None
        if target is not None:
            target[index // 8] &= ~(0x80 >> (index % 8))
    return bytes(black + red)


def packet(image: Image.Image, sleep: int) -> bytes:
    """Prepend the 16-byte network-order EIF1 header and body CRC32 to the planes."""
    data = planes(image)
    return (
        struct.pack(">4sHHII", b"EIF1", WIDTH, HEIGHT, sleep, zlib.crc32(data)) + data
    )


@dataclass
class Dashboard:
    """Serialize builds so concurrent requests cannot exhaust memory or flood HA."""

    cfg: Configuration
    demo: bool
    empty_calendar: bool = False
    lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def build(self) -> tuple[Image.Image, bytes]:
        """Fetch data, render and calculate wake-up after slow upstream calls finish."""
        with self.lock:
            now = datetime.now(ZoneInfo(self.cfg["timezone"]))
            events, values, errors = collect(self.cfg, now, self.demo)
            if self.demo and self.empty_calendar:
                events = []
            image = render(
                self.cfg, now, DashboardData(events, values, errors), self.demo
            )
            sleep = (
                300 if errors else next_wake(datetime.now(now.tzinfo), self.cfg, events)
            )
            return image, packet(image, sleep)


def create_server(dashboard: Dashboard, host: str, port: int) -> ThreadingHTTPServer:
    """Create the authenticated LAN server; port zero is useful in integration tests."""
    token = os.environ.get("FRAME_TOKEN", "")
    if not token:
        raise ValueError("Nastav FRAME_TOKEN; chrání agendu i obraz.")

    class Handler(BaseHTTPRequestHandler):
        """Expose the binary image and PNG preview with a shared Bearer token."""

        # BaseHTTPRequestHandler dispatches requests by this exact method name.
        def do_GET(self) -> None:  # pylint: disable=invalid-name
            """Authenticate before collecting data or exposing either image endpoint."""
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self.send_error(401)
                return
            if self.path not in ("/frame.bin", "/preview.png"):
                self.send_error(404)
                return
            image, data = dashboard.build()
            mime = "application/octet-stream"
            if self.path == "/preview.png":
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                data, mime = buffer.getvalue(), "image/png"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(
            self,
            format: str,  # pylint: disable=redefined-builtin
            *args: Any,
        ) -> None:
            """Keep the stdlib signature and log status without credentials."""
            del format
            LOGGER.info("HTTP %s", args[1] if len(args) > 1 else "")

    return ThreadingHTTPServer((host, port), Handler)


def serve(dashboard: Dashboard, host: str, port: int) -> None:
    """Run the server and release its listening socket when it exits."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with create_server(dashboard, host, port) as server:
        server.serve_forever()


def _arguments() -> argparse.ArgumentParser:
    """Define the local preview and standalone-server command-line options."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.example.json"))
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--empty-calendar", action="store_true", help="Prázdný kalendář v demo režimu"
    )
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main() -> None:
    """Validate local inputs, then write a preview or start the HTTP service."""
    parser = _arguments()
    args = parser.parse_args()
    config = validate(json.loads(Path(args.config).read_text(encoding="utf-8")))
    if not args.demo and not all(os.environ.get(key) for key in ("HA_URL", "HA_TOKEN")):
        parser.error("Živý provoz vyžaduje HA_URL a HA_TOKEN; pro ukázku použij --demo")
    if args.empty_calendar and not args.demo:
        parser.error("--empty-calendar vyžaduje --demo")
    dashboard = Dashboard(config, args.demo, args.empty_calendar)
    if args.preview:
        image, data = dashboard.build()
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.preview)
        args.preview.with_suffix(".bin").write_bytes(data)
        print(args.preview)
    else:
        serve(dashboard, args.host, args.port)


if __name__ == "__main__":
    main()
```


## pyproject.toml

```toml
[tool.black]
line-length = 88
target-version = ["py312"]
include = '\.pyi?$'
extend-exclude = '/(output|\.venv|\.buildcache|\.pio)/'

[tool.isort]
profile = "black"
line_length = 88
known_first_party = ["renderer"]
skip_gitignore = false
extend_skip = ["output", ".venv", ".buildcache", ".pio"]

[tool.pylint.main]
py-version = "3.12"
persistent = false
recursive = true
ignore-paths = ['^output/', '^\.venv/', '^\.buildcache/', '^firmware/\.pio/']

[tool.pylint.format]
max-line-length = 88
```


## .github/workflows/python.yml

```yaml
name: Python quality
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - name: Install fonts and development tools
        run: |
          sudo apt-get update
          sudo apt-get install -y fonts-lato
          python -m pip install -r requirements-dev.txt
      - name: Check formatting
        run: |
          python -m black --check renderer addon/eink_frame/entrypoint.py tools tests
          python -m isort --check-only renderer addon/eink_frame/entrypoint.py tools tests
      - name: Run Pylint
        run: python -m pylint renderer addon/eink_frame/entrypoint.py tools tests
      - name: Run tests
        run: python -m unittest discover -s tests -v
```


## renderer/__init__.py

```python
"""Home Assistant data collection and three-color e-ink dashboard rendering."""
```


## config.example.json

```json
{
  "timezone": "Europe/Prague",
  "calendar_entities": [
    "calendar.school"
  ],
  "agenda_days": 2,
  "metrics": [
    {
      "entity": "input_number.outdoor_temperature",
      "label": "VENKU",
      "unit": "°C"
    },
    {
      "entity": "input_number.natroom_temperature",
      "label": "DOMA",
      "unit": "°C"
    },
    {
      "entity": "input_number.outdoor_effective_temperature",
      "label": "MAX DNES",
      "unit": "°C"
    }
  ],
  "schedule": {
    "day_start": 7,
    "night_start": 22,
    "day_minutes": 30,
    "night_minutes": 120
  },
  "empty_calendar_quote": "Můžeme dělat malé věci s velkou láskou.",
  "empty_calendar_author": "Matka Tereza"
}
```


## firmware/src/main.cpp

```cpp
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>
#include <epd3c/GxEPD2_750c_Z08.h>
#include "secrets.h"

// Driver for GDEW075Z08, 800x480 B/W/red. Verify panel marking before uploading.
GxEPD2_750c_Z08 panel(21, 22, 25, 26); // CS, DC, RST, BUSY
constexpr size_t PLANE = 800 * 480 / 8;
constexpr size_t BODY = PLANE * 2;
RTC_DATA_ATTR uint32_t savedMagic = 0;
RTC_DATA_ATTR uint32_t savedCRC = 0;
uint32_t be32(const uint8_t* p) {
  return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
}
uint32_t crc32(const uint8_t* p, size_t n) {
  uint32_t crc = 0xffffffff;
  while (n--) {
    crc ^= *p++;
    for (int i = 0; i < 8; ++i) crc = (crc >> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc;
}
bool readExact(WiFiClient& stream, uint8_t* dst, size_t count) {
  uint32_t start = millis();
  size_t done = 0;
  while (done < count && millis() - start < 20000) {
    int available = stream.available();
    if (available > 0) {
      int got = stream.read(dst + done, min(size_t(available), count - done));
      if (got > 0) done += got;
    } else {
      if (!stream.connected()) break;
      delay(2);
    }
  }
  return done == count;
}
void sleepFor(uint32_t seconds) {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.printf("Sleep: %lu s\n", (unsigned long)seconds);
  Serial.flush();
  esp_sleep_enable_timer_wakeup(uint64_t(seconds) * 1000000ULL);
  esp_deep_sleep_start();
}
void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(100);
  if (WiFi.status() != WL_CONNECTED) sleepFor(300);
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(60000); // server may wait for upstream HA calls
  if (!http.begin(client, FRAME_URL)) sleepFor(300);
  http.addHeader("Authorization", String("Bearer ") + FRAME_TOKEN);
  int status = http.GET();
  uint8_t header[16];
  uint8_t* body = nullptr;
  uint32_t seconds = 300;
  uint32_t receivedAt = millis();
  bool valid = status == 200 && http.getSize() == int(BODY + 16);
  if (valid) valid = readExact(*http.getStreamPtr(), header, sizeof(header));
  if (valid) {
    seconds = be32(header + 8);
    valid = memcmp(header, "EIF1", 4) == 0 && header[4] == 3 && header[5] == 32
      && header[6] == 1 && header[7] == 224 && seconds >= 60 && seconds <= 86400;
  }
  if (valid) {
    body = static_cast<uint8_t*>(heap_caps_malloc(BODY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!body) body = static_cast<uint8_t*>(malloc(BODY));
    valid = body && readExact(*http.getStreamPtr(), body, BODY);
  }
  if (valid) valid = crc32(body, BODY) == be32(header + 12);
  http.end();
  if (!valid) {
    Serial.println("Download invalid; keep previous screen.");
    free(body);
    sleepFor(300);
  }
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  uint32_t crc = be32(header + 12);
  if (savedMagic != 0xe1f18004 || savedCRC != crc) {
    SPI.begin(18, -1, 23, 21);
    panel.init(115200, true, 2, false);
    panel.writeImage(body, body + PLANE, 0, 0, 800, 480);
    panel.refresh(false);
    panel.hibernate();
    savedCRC = crc;
    savedMagic = 0xe1f18004;
  }
  free(body);
  uint32_t elapsed = (millis() - receivedAt) / 1000;
  sleepFor(seconds > elapsed ? seconds - elapsed : 1);
}
void loop() {}
```


## firmware/platformio.ini

```ini
[env:firebeetle]
platform = espressif32@6.10.0
board = firebeetle32
framework = arduino
monitor_speed = 115200
board_build.flash_size = 16MB
board_upload.flash_size = 16MB
build_flags = -DBOARD_HAS_PSRAM
lib_deps = zinggjm/GxEPD2@1.6.4
```


## firmware/include/secrets.example.h

```cpp
#pragma once
#define WIFI_SSID "your-wifi"
#define WIFI_PASSWORD "your-password"
#define FRAME_URL "http://192.168.1.10:8080/frame.bin"
#define FRAME_TOKEN "replace-with-long-random-token"
```


## addon/eink_frame/config.yaml

```yaml
name: E-ink Frame
version: "0.3.1"
slug: eink_frame
description: Datum, školní agenda a domácí údaje pro ESP32 e-ink rámeček
arch:
  - aarch64
  - amd64
startup: application
boot: auto
homeassistant_api: true
ports:
  8080/tcp: 8080
ports_description:
  8080/tcp: Obraz pro ESP32 (vyžaduje FRAME_TOKEN)
options:
  frame_token: ""
  demo: true
  demo_empty_calendar: false
  empty_calendar_quote: "Můžeme dělat malé věci s velkou láskou."
  empty_calendar_author: "Matka Tereza"
  timezone: Europe/Prague
  calendar_entities:
    - calendar.school
  outdoor_entity: input_number.outdoor_temperature
  indoor_entity: input_number.natroom_temperature
  max_temperature_entity: input_number.outdoor_effective_temperature
  agenda_days: 2
  day_start: 7
  night_start: 22
  day_minutes: 30
  night_minutes: 120
schema:
  frame_token: password
  demo: bool
  demo_empty_calendar: bool
  empty_calendar_quote: str
  empty_calendar_author: str
  timezone: str
  calendar_entities:
    - str
  outdoor_entity: str
  indoor_entity: str
  max_temperature_entity: str
  agenda_days: int(1,7)
  day_start: int(0,23)
  night_start: int(0,23)
  day_minutes: int(1,1440)
  night_minutes: int(1,1440)
```


## addon/eink_frame/Dockerfile

```text
FROM python:3.12-slim
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
RUN apt-get update && apt-get install -y --no-install-recommends fonts-lato tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY renderer ./renderer
COPY entrypoint.py .
CMD ["python", "entrypoint.py"]
```


## addon/eink_frame/entrypoint.py

```python
"""Translate HA OS app options and authenticate with the Supervisor API proxy."""

import json
import os
from pathlib import Path
from typing import Any

from renderer.app import DEFAULT_AUTHOR, DEFAULT_QUOTE, Dashboard, serve, validate


def config(options: dict[str, Any]) -> dict[str, Any]:
    """Build the same renderer configuration used by standalone installations."""
    return validate(
        {
            "timezone": options["timezone"],
            "calendar_entities": options["calendar_entities"],
            "agenda_days": options["agenda_days"],
            "empty_calendar_quote": options.get("empty_calendar_quote", DEFAULT_QUOTE),
            "empty_calendar_author": options.get(
                "empty_calendar_author", DEFAULT_AUTHOR
            ),
            "metrics": [
                {"entity": options["outdoor_entity"], "label": "VENKU", "unit": "°C"},
                {"entity": options["indoor_entity"], "label": "DOMA", "unit": "°C"},
                {
                    "entity": options["max_temperature_entity"],
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
```


## requirements.txt

```text
Pillow>=11.1,<13
```


## requirements-dev.txt

```text
-r requirements.txt
PyYAML>=6,<7
black>=26,<27
isort>=6,<7
pylint>=3.3,<4
```


## Dockerfile

```text
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-lato && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY renderer ./renderer
COPY config.example.json .
USER 65534:65534
CMD ["python", "-m", "renderer.app", "--config", "/app/config.json", "--host", "0.0.0.0"]
```


## compose.yaml

```yaml
services:
  eink:
    build: .
    restart: unless-stopped
    env_file: .env
    ports:
      - "8080:8080"
    volumes:
      - ./config.json:/app/config.json:ro
```


## .env.example

```text
HA_URL=http://homeassistant.local:8123
HA_TOKEN=replace-with-home-assistant-long-lived-token
FRAME_TOKEN=replace-with-separate-long-random-token
```


## .gitignore

```text
# Byte-compiled / optimized / DLL files
__pycache__/
*.py[codz]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
share/python-wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# PyInstaller
#   Usually these files are written by a python script from a template
#   before PyInstaller builds the exe, so as to inject date/other infos into it.
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.nox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.py.cover
*.lcov
.hypothesis/
.pytest_cache/
cover/

# Translations
*.mo
*.pot

# Django stuff:
*.log
local_settings.py
db.sqlite3
db.sqlite3-journal

# Flask stuff:
instance/
.webassets-cache

# Scrapy stuff:
.scrapy

# Sphinx documentation
docs/_build/

# PyBuilder
.pybuilder/
target/

# Jupyter Notebook
.ipynb_checkpoints

# IPython
profile_default/
ipython_config.py

# pyenv
#   For a library or package, you might want to ignore these files since the code is
#   intended to run in multiple environments; otherwise, check them in:
# .python-version

# pipenv
#   According to pypa/pipenv#598, it is recommended to include Pipfile.lock in version control.
#   However, in case of collaboration, if having platform-specific dependencies or dependencies
#   having no cross-platform support, pipenv may install dependencies that don't work, or not
#   install all needed dependencies.
# Pipfile.lock

# uv
#   Similar to Pipfile.lock, it is generally recommended to include uv.lock in version control.
#   This is especially recommended for binary packages to ensure reproducibility, and is more
#   commonly ignored for libraries.
# uv.lock

# poetry
#   Similar to Pipfile.lock, it is generally recommended to include poetry.lock in version control.
#   This is especially recommended for binary packages to ensure reproducibility, and is more
#   commonly ignored for libraries.
#   https://python-poetry.org/docs/basic-usage/#commit-your-poetrylock-file-to-version-control
# poetry.lock
# poetry.toml

# pdm
#   Similar to Pipfile.lock, it is generally recommended to include pdm.lock in version control.
#   pdm recommends including project-wide configuration in pdm.toml, but excluding .pdm-python.
#   https://pdm-project.org/en/latest/usage/project/#working-with-version-control
# pdm.lock
# pdm.toml
.pdm-python
.pdm-build/

# pixi
#   Similar to Pipfile.lock, it is generally recommended to include pixi.lock in version control.
# pixi.lock
#   Pixi creates a virtual environment in the .pixi directory, just like venv module creates one
#   in the .venv directory. It is recommended not to include this directory in version control.
.pixi/*
!.pixi/config.toml

# PEP 582; used by e.g. github.com/David-OConnor/pyflow and github.com/pdm-project/pdm
__pypackages__/

# Celery stuff
celerybeat-schedule*
celerybeat.pid

# Redis
*.rdb
*.aof
*.pid

# RabbitMQ
mnesia/
rabbitmq/
rabbitmq-data/

# ActiveMQ
activemq-data/

# SageMath parsed files
*.sage.py

# Environments
.env
.envrc
.venv
env/
venv/
ENV/
env.bak/
venv.bak/

# Spyder project settings
.spyderproject
.spyproject

# Rope project settings
.ropeproject

# mkdocs/Zensical documentation
/site

# mypy
.mypy_cache/
.dmypy.json
dmypy.json

# Pyre type checker
.pyre/

# pytype static type analyzer
.pytype/

# Cython debug symbols
cython_debug/

# PyCharm
#   JetBrains specific template is maintained in a separate JetBrains.gitignore that can
#   be found at https://github.com/github/gitignore/blob/main/Global/JetBrains.gitignore
#   and can be added to the global gitignore or merged into this file.  For a more nuclear
#   option (not recommended) you can uncomment the following to ignore the entire idea folder.
# .idea/

# Abstra
#   Abstra is an AI-powered process automation framework.
#   Ignore directories containing user credentials, local state, and settings.
#   Learn more at https://abstra.io/docs
.abstra/

# Visual Studio Code
#   Visual Studio Code specific template is maintained in a separate VisualStudioCode.gitignore that
#   can be found at https://github.com/github/gitignore/blob/main/Global/VisualStudioCode.gitignore
#   and can be added to the global gitignore or merged into this file. However, if you prefer, you
#   could uncomment the following to ignore the entire vscode folder
# .vscode/
# Temporary file for partial code execution
tempCodeRunnerFile.py

# Ruff stuff:
.ruff_cache/

# PyPI configuration file
.pypirc

# Marimo
marimo/_static/
marimo/_lsp/
__marimo__/

# Streamlit
.streamlit/secrets.toml

# E-ink frame local configuration and build artifacts
.env
config.json
firmware/include/secrets.h
firmware/.pio/
__pycache__/
output/*.bin
.venv/
output/local-addon/
.buildcache/
```


## .dockerignore

```text
.env
config.json
.git
firmware
output
__pycache__
tests
```


## tools/package_addon.py

```python
"""Prepare a self-contained HA OS app from the shared renderer source."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "output" / "local-addon" / "eink_frame"


def main() -> None:
    """Copy app metadata and renderer, excluding disposable Python bytecode."""
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "Dockerfile", "entrypoint.py", "DOCS.md"):
        shutil.copy2(ROOT / "addon" / "eink_frame" / name, DESTINATION / name)
    shutil.copy2(ROOT / "requirements.txt", DESTINATION / "requirements.txt")
    shutil.copytree(
        ROOT / "renderer",
        DESTINATION / "renderer",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    print(DESTINATION)


if __name__ == "__main__":
    main()
```


## tools/generate_previews.py

```python
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
        events, values, errors = renderer.collect(config, now, True)
        data = renderer.DashboardData([] if empty else events, values, errors)
        image = renderer.render(config, now, data, True)
        destination = ROOT / "output" / (name + ".png")
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        print(destination)


if __name__ == "__main__":
    main()
```


## tools/package_project.py

```python
"""Export a readable source listing and project archive without local credentials."""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
CORE_FILES = [
    "renderer/app.py",
    "pyproject.toml",
    ".github/workflows/python.yml",
    "renderer/__init__.py",
    "config.example.json",
    "firmware/src/main.cpp",
    "firmware/platformio.ini",
    "firmware/include/secrets.example.h",
    "addon/eink_frame/config.yaml",
    "addon/eink_frame/Dockerfile",
    "addon/eink_frame/entrypoint.py",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "compose.yaml",
    ".env.example",
    ".gitignore",
    ".dockerignore",
    "tools/package_addon.py",
    "tools/generate_previews.py",
    "tools/package_project.py",
    "tests/test_renderer.py",
]
DOCUMENTATION_FILES = [
    "README.md",
    "docs/zadani.md",
    "docs/mereni.md",
    "docs/protokol.md",
    "addon/eink_frame/DOCS.md",
]
PREVIEW_FILES = [
    "output/preview-future.png",
    "output/preview-active.png",
    "output/preview-empty.png",
]


def source_listing() -> str:
    """Create fenced code blocks for an explicit allowlist of project source files."""
    sections = [
        "# Kompletní zdrojový kód e-ink rámečku\n",
        "Snapshot aktuálních zdrojů. Wi-Fi a tokeny doplň lokálně "
        "podle secrets.example.h.\n",
        "Instalace, nákup, zapojení a ověření jsou v docs/zadani.md.\n",
    ]
    languages = {
        ".py": "python",
        ".cpp": "cpp",
        ".h": "cpp",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".ini": "ini",
        ".toml": "toml",
    }
    for filename in CORE_FILES:
        language = languages.get(Path(filename).suffix, "text")
        contents = (ROOT / filename).read_text(encoding="utf-8").rstrip()
        sections.append(f"\n## {filename}\n\n```{language}\n{contents}\n```\n")
    return "\n".join(sections)


def main() -> None:
    """Write source documentation and archive only explicitly approved files."""
    OUTPUT.mkdir(exist_ok=True)
    listing_path = OUTPUT / "cely-kod.md"
    listing_path.write_text(source_listing(), encoding="utf-8")
    filenames = (
        CORE_FILES + DOCUMENTATION_FILES + PREVIEW_FILES + ["output/cely-kod.md"]
    )
    with zipfile.ZipFile(
        OUTPUT / "eink-frame-komplet.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for filename in filenames:
            archive.write(ROOT / filename, "eink-frame/" + filename)
    print(listing_path)
    print(OUTPUT / "eink-frame-komplet.zip")


if __name__ == "__main__":
    main()
```


## tests/test_renderer.py

```python
"""Behavioral checks for calendar lifecycle, scheduling, bitplanes and HTTP auth."""

import importlib.util
import json
import struct
import threading
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

import yaml
from PIL import Image

from renderer import app

CFG = app.validate(json.loads(Path("config.example.json").read_text(encoding="utf-8")))
ZONE = ZoneInfo("Europe/Prague")


class RendererTests(unittest.TestCase):
    """Exercise externally visible dashboard behavior and protocol boundaries."""

    def test_bitplanes_and_crc(self):
        """Verify active-low bit order, both planes and CRC over the complete body."""
        image = Image.new("RGB", (800, 480), "white")
        image.putpixel((0, 0), (0, 0, 0))
        image.putpixel((7, 0), app.RED)
        data = app.packet(image, 1800)
        self.assertEqual(len(data), 96016)
        magic, width, height, sleep, crc = struct.unpack(">4sHHII", data[:16])
        self.assertEqual((magic, width, height, sleep), (b"EIF1", 800, 480, 1800))
        self.assertEqual(data[16], 0x7F)
        self.assertEqual(data[16 + 48000], 0xFE)
        self.assertEqual(crc, zlib.crc32(data[16:]))
        self.assertNotEqual(crc, zlib.crc32(data[17:]))

    def test_schedule_boundaries_and_event(self):
        """Wake at day/night boundaries and nearer calendar transitions."""
        now = datetime(2026, 10, 8, 21, 45, tzinfo=ZONE)
        self.assertEqual(app.next_wake(now, CFG), 900)
        now = now.replace(hour=22, minute=1)
        self.assertEqual(app.next_wake(now, CFG), 7140)
        event = {"start": now.replace(minute=12), "end": now.replace(minute=20)}
        self.assertEqual(app.next_wake(now, CFG, [event]), 660)

    def test_dst_spring_and_fall(self):
        """Reject skipped spring slots and consider both repeated autumn hours."""
        # Czech spring jump: 01:59 -> 03:00. Next night slot 04:00.
        now = datetime(2026, 3, 29, 1, 59, tzinfo=ZONE)
        self.assertEqual(app.next_wake(now, CFG), 3660)
        # Both occurrences of 02:00 are legal slots during autumn switch.
        now = datetime(2026, 10, 25, 2, 1, tzinfo=ZONE, fold=0)
        self.assertEqual(app.next_wake(now, CFG), 3540)

    def test_calendar_and_missing_metric(self):
        """Retain all-day events, hide expired entries and mark unavailable sensors."""
        now = datetime(2026, 10, 8, 12, tzinfo=ZONE)

        def fake(path):
            """Return controlled HA responses without contacting a live server."""
            if path.startswith("/api/calendars/"):
                return [
                    {
                        "summary": "Celý den",
                        "start": {"date": "2026-10-08"},
                        "end": {"date": "2026-10-09"},
                    },
                    {
                        "summary": "Minulost",
                        "start": {"dateTime": "2026-10-08T08:00:00+02:00"},
                        "end": {"dateTime": "2026-10-08T09:00:00+02:00"},
                    },
                ]
            if "outdoor_temperature" in path:
                return {"state": "12.4", "attributes": {}}
            return {"state": "unavailable"}

        with patch.object(app, "api", side_effect=fake):
            events, values, errors = app.collect(CFG, now, False)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["all_day"])
        self.assertEqual(values, ["12,4 °C", "—", "—"])
        self.assertTrue(errors)

    def test_calendar_lifecycle_and_background(self):
        """Use inclusive starts, exclusive ends and red ongoing-event backgrounds."""
        now = datetime(2026, 10, 8, 8, 30, tzinfo=ZONE)
        events, values, errors = app.collect(CFG, now, True)
        current = events[0]
        self.assertTrue(app.is_running(current, now))
        self.assertTrue(app.is_running(current, current["start"]))
        self.assertFalse(app.is_running(current, current["end"]))
        self.assertNotIn(current, app.visible_events(events, current["end"]))
        image = app.render(CFG, now, app.DashboardData(events, values, errors), True)
        self.assertEqual(image.getpixel((210, 72)), app.RED)
        self.assertEqual(image.getpixel((1, 479)), (0, 0, 0))
        future = app.render(
            CFG,
            current["start"].replace(hour=7),
            app.DashboardData(events, values, errors),
            True,
        )
        self.assertEqual(future.getpixel((210, 72)), (255, 255, 255))
        all_day = {
            "summary": "Celý den",
            "all_day": True,
            "start": datetime(2026, 10, 8, tzinfo=ZONE),
            "end": datetime(2026, 10, 9, tzinfo=ZONE),
        }
        self.assertTrue(app.is_running(all_day, now))
        self.assertEqual(app.visible_events([all_day], all_day["end"]), [])

    def test_max_temperature_entity(self):
        """Read the configured forecast/maximum helper rather than computing it."""
        now = datetime(2026, 10, 8, 12, tzinfo=ZONE)

        def fake(path):
            """Return controlled HA responses without contacting a live server."""
            if path.startswith("/api/calendars/"):
                return []
            if "outdoor_effective_temperature" in path:
                return {"state": "16.8", "attributes": {}}
            return {"state": "12.4", "attributes": {}}

        with patch.object(app, "api", side_effect=fake):
            _, values, errors = app.collect(CFG, now, False)
        self.assertEqual(values[-1], "16,8 °C")
        self.assertEqual(errors, [])

    def test_addon_config_matches_entities(self):
        """Keep app option translation consistent with the standalone example."""
        spec = importlib.util.spec_from_file_location(
            "entrypoint", "addon/eink_frame/entrypoint.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        options = yaml.safe_load(
            Path("addon/eink_frame/config.yaml").read_text(encoding="utf-8")
        )["options"]
        self.assertEqual(module.config(options), CFG)

    def test_http_auth_and_payload(self):
        """Require a token and serve complete CRC-valid binary and PNG images."""
        with patch.dict(app.os.environ, {"FRAME_TOKEN": "test-token"}):
            server = app.create_server(app.Dashboard(CFG, True), "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        urlopen = build_opener(ProxyHandler({})).open
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(url + "/frame.bin")
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            req = Request(
                url + "/frame.bin", headers={"Authorization": "Bearer test-token"}
            )
            with urlopen(req) as response:
                data = response.read()
                self.assertEqual(len(data), 96016)
                self.assertEqual(
                    zlib.crc32(data[16:]), struct.unpack(">I", data[12:16])[0]
                )
            req = Request(
                url + "/preview.png", headers={"Authorization": "Bearer test-token"}
            )
            with urlopen(req) as response:
                self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_render_palette_and_size(self):
        """Render only the three colors supported by the selected display."""
        now = datetime(2026, 10, 8, 7, tzinfo=ZONE)
        events, values, errors = app.collect(CFG, now, True)
        image = app.render(CFG, now, app.DashboardData(events, values, errors), True)
        self.assertEqual(image.size, (800, 480))
        self.assertEqual(set(image.getdata()), {(255, 255, 255), (0, 0, 0), app.RED})


if __name__ == "__main__":
    unittest.main()
```
