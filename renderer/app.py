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
DEFAULT_HA_URL = "http://homeassistant.local:8123"
QUOTE_ERROR = "Citát nebo autor nejsou dostupné"
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
    quote: str = ""
    author: str = ""


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
    base = os.environ.get("HA_URL", DEFAULT_HA_URL).rstrip("/")
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


def collect_dashboard(
    config: Configuration, now: datetime, demo: bool, empty_calendar: bool = False
) -> DashboardData:
    """Fetch quote helpers only when a successfully loaded agenda is empty."""
    events, values, errors = collect(config, now, demo)
    data = DashboardData([] if demo and empty_calendar else events, values, errors)
    if data.events or CALENDAR_ERROR in data.errors:
        return data
    if demo:
        data.quote, data.author = DEFAULT_QUOTE, DEFAULT_AUTHOR
        return data
    for attribute in ("quote", "author"):
        try:
            entity = config[f"empty_calendar_{attribute}_entity"]
            state = api("/api/states/" + quote(entity, safe=""))["state"]
            if not isinstance(state, str) or state in ("unknown", "unavailable"):
                raise ValueError("Unavailable text helper")
            setattr(data, attribute, state.strip())
        except DATA_ERRORS:
            if QUOTE_ERROR not in data.errors:
                data.errors.append(QUOTE_ERROR)
    return data


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
        (value for metric, value in pairs if metric.get("role") == "outdoor_max"),
        None,
    )
    has_outdoor = any(metric.get("role") == "outdoor" for metric, _ in pairs)
    rows = []
    for metric, value in pairs:
        if has_outdoor and metric.get("role") == "outdoor_max":
            continue
        if metric.get("role") == "outdoor" and maximum is not None:
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
    lines = _quote_lines(draw, context.data.quote or "Citát není dostupný")
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
    author = context.data.author
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
        visible_events(data.events, now),
        data.values,
        data.errors,
        data.quote,
        data.author,
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
            data = collect_dashboard(self.cfg, now, self.demo, self.empty_calendar)
            image = render(self.cfg, now, data, self.demo)
            sleep = (
                300
                if data.errors
                else next_wake(datetime.now(now.tzinfo), self.cfg, data.events)
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
    if not args.demo and not os.environ.get("HA_TOKEN"):
        parser.error("Živý provoz vyžaduje HA_TOKEN; pro ukázku použij --demo")
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
