# Kompletní zdrojový kód e-ink rámečku

Snapshot aktuálních zdrojů. Wi-Fi a tokeny doplň lokálně podle secrets.example.h.

Instalace, nákup, zapojení a ověření jsou v docs/zadani.md.


## renderer/app.py

```python
"""800x480 B/W/red dashboard and authenticated pull endpoint for ESP32."""
import argparse
from functools import lru_cache
from decimal import Decimal, InvalidOperation
import hmac
import io
import json
import os
from pathlib import Path
import struct
import threading
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zlib
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 800, 480
RED = (190, 0, 0)
MONTHS = ['ledna', 'února', 'března', 'dubna', 'května', 'června',
          'července', 'srpna', 'září', 'října', 'listopadu', 'prosince']
DAYS = ['Pondělí', 'Úterý', 'Středa', 'Čtvrtek', 'Pátek', 'Sobota', 'Neděle']

def validate(cfg):
    ZoneInfo(cfg['timezone'])
    s = cfg['schedule']
    if not (0 <= s['day_start'] < s['night_start'] <= 23):
        raise ValueError('day_start musí být před night_start (0–23)')
    for key in ('day_minutes', 'night_minutes'):
        if not 1 <= s[key] <= 1440:
            raise ValueError('Interval musí být 1–1440 minut')
    if not 1 <= cfg['agenda_days'] <= 7 or len(cfg['metrics']) > 3:
        raise ValueError('Agenda 1–7 dní, nejvýše 3 hodnoty')
    return cfg

def next_wake(now, cfg, events=()):
    """Local wall-clock slots, plus upcoming event start/end; UTC delta handles DST."""
    s = cfg['schedule']
    zone = now.tzinfo
    candidates = []
    for day_offset in range(3):
        day = now.date() + timedelta(days=day_offset)
        for minute in range(0, 1440):
            hour = minute // 60
            interval = s['day_minutes'] if s['day_start'] <= hour < s['night_start'] else s['night_minutes']
            if minute % interval == 0 or minute in (s['day_start']*60, s['night_start']*60):
                wall = datetime.combine(day, time(hour, minute % 60), zone)
                for fold in (0, 1):
                    dt = wall.replace(fold=fold)
                    stamp = dt.timestamp()
                    back = datetime.fromtimestamp(stamp, zone)
                    if back.hour == hour and back.minute == minute % 60 and stamp > now.timestamp():
                        candidates.append(stamp)
    for event in events:
        for key in ('start', 'end'):
            stamp = event[key].timestamp()
            if stamp > now.timestamp():
                candidates.append(stamp)
    return max(60, min(86400, int(min(candidates) - now.timestamp())))

def parse_time(value, zone):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)

def api(path):
    base = os.environ['HA_URL'].rstrip('/')
    if urlparse(base).scheme not in ('http', 'https'):
        raise ValueError('HA_URL musí začínat http:// nebo https://')
    req = Request(base + path, headers={'Authorization': 'Bearer ' + os.environ['HA_TOKEN']})
    with urlopen(req, timeout=8) as response:
        return json.load(response)

def visible_events(events, now):
    return sorted((event for event in events if event['end'].timestamp() > now.timestamp()),
                  key=lambda event: event['start'].timestamp())

def is_running(event, now):
    return event['start'].timestamp() <= now.timestamp() < event['end'].timestamp()

def collect(cfg, now, demo):
    if demo:
        tomorrow = now.date() + timedelta(days=1)
        events = [
            {'summary': 'Rodinná snídaně', 'start': now.replace(hour=8, minute=0, second=0), 'end': now.replace(hour=9, minute=0, second=0), 'all_day': False},
            {'summary': 'Nákup a vyzvednutí zásilky', 'start': now.replace(hour=17, minute=30, second=0), 'end': now.replace(hour=18, minute=0, second=0), 'all_day': False},
            {'summary': 'Výlet s rodinou', 'start': datetime.combine(tomorrow, time(9), now.tzinfo), 'end': datetime.combine(tomorrow, time(15), now.tzinfo), 'all_day': False}
        ]
        return visible_events(events, now), ['12,4 °C', '22,1 °C', '16,8 °C'][:len(cfg['metrics'])], []
    events, values, errors = [], [], []
    start = datetime.combine(now.date(), time(), now.tzinfo)
    end = start + timedelta(days=cfg['agenda_days'])
    for entity in cfg['calendar_entities']:
        try:
            query = urlencode({'start': start.isoformat(), 'end': end.isoformat()})
            for e in api('/api/calendars/' + quote(entity, safe='') + '?' + query):
                st, en = e['start'], e['end']
                all_day = 'date' in st
                events.append({'summary': e.get('summary', 'Událost'),
                    'start': parse_time(st.get('dateTime', st.get('date')), now.tzinfo),
                    'end': parse_time(en.get('dateTime', en.get('date')), now.tzinfo), 'all_day': all_day})
        except Exception:
            errors.append('Kalendář není dostupný')
    for metric in cfg['metrics']:
        try:
            state = api('/api/states/' + quote(metric['entity'], safe=''))
            if state['state'] in ('unavailable', 'unknown'):
                raise ValueError('Unavailable')
            unit = metric.get('unit', state.get('attributes', {}).get('unit_of_measurement', ''))
            value = metric.get('states', {}).get(state['state'], state['state'])
            values.append((value.replace('.', ',') + ' ' + unit).strip())
        except Exception:
            values.append('—')
            errors.append('Některé hodnoty nejsou dostupné')
    events = visible_events(events, now)
    return events, values, sorted(set(errors))

@lru_cache(maxsize=64)
def font(size, bold=False):
    name = 'Lato-Bold.ttf' if bold else 'Lato-Regular.ttf'
    return ImageFont.truetype(os.environ.get('FONT_DIR', '/usr/share/fonts/truetype/lato') + '/' + name, size)


def fit(draw, text, f, width):
    text = ' '.join(str(text).split())
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + '…', font=f) > width:
        text = text[:-1]
    return text + '…'

def event_title_lines(draw, text, f, width):
    words = str(text).split()
    if not words:
        return ['Událost']
    first = words.pop(0)
    while words and draw.textlength(first + ' ' + words[0], font=f) <= width:
        first += ' ' + words.pop(0)
    lines = [fit(draw, first, f, width)]
    if words:
        lines.append(fit(draw, ' '.join(words), f, width))
    return lines

def temperature_number(value):
    text = value.removesuffix('°C').strip().replace(',', '.')
    try:
        number = Decimal(text)
        return format(number.normalize(), 'f') if number.is_finite() else '—'
    except InvalidOperation:
        return text

def metric_rows(cfg, values):
    pairs = list(zip(cfg['metrics'], values))
    maximum = next((value for metric, value in pairs
                    if metric['entity'] == 'input_number.outdoor_effective_temperature'), None)
    has_outdoor = any(metric['entity'] == 'input_number.outdoor_temperature' for metric, _ in pairs)
    rows = []
    for metric, value in pairs:
        if has_outdoor and metric['entity'] == 'input_number.outdoor_effective_temperature':
            continue
        if metric['entity'] == 'input_number.outdoor_temperature' and maximum is not None:
            current, top = temperature_number(value), temperature_number(maximum)
            value = (current if current == top else current + ' → ' + top) + ' °C'
        rows.append((metric, value))
    return rows

def render(cfg, now, events, values, errors, demo=False):
    events = visible_events(events, now)
    image = Image.new('RGB', (WIDTH, HEIGHT), 'white')
    d = ImageDraw.Draw(image)
    rail_width, main_x, main_width = 192, 224, 544
    d.rectangle((0, 0, rail_width-1, HEIGHT-1), fill='black')
    d.text((96, 42), DAYS[now.weekday()].upper(), font=font(18, True), fill='white', anchor='mt')
    d.text((96, 88), str(now.day), font=font(104, True), fill='white', anchor='mt')
    d.text((96, 195), MONTHS[now.month-1], font=font(28), fill='white', anchor='mt')

    if not events:
        if 'Kalendář není dostupný' in errors:
            d.text((main_x, 64), 'KALENDÁŘ NENÍ DOSTUPNÝ', font=font(18, True), fill=RED, anchor='lt')
            message = 'Údaje se nepodařilo načíst.'
            for i, line in enumerate(event_title_lines(d, message, font(42), main_width)):
                d.text((main_x, 141+i*53), line, font=font(42), fill='black', anchor='lt')
        else:
            d.text((main_x-3, 64), '“', font=font(84, True), fill=RED, anchor='lt')
            quote = cfg.get('empty_calendar_quote', 'Můžeme dělat malé věci s velkou láskou.')
            # Quotes get up to three lines rather than event-title truncation.
            words, lines = str(quote).split(), []
            while words and len(lines) < 3:
                line = words.pop(0)
                while words and d.textlength(line+' '+words[0],font=font(44,True)) <= main_width:
                    line += ' '+words.pop(0)
                if len(lines) == 2 and words:
                    line += ' '+' '.join(words)
                    words = []
                lines.append(fit(d,line,font(44,True),main_width))
            for i,line in enumerate(lines):
                d.text((main_x, 143+i*55),line,font=font(44,True),fill='black',anchor='lt')
            bottom = 143+len(lines)*55
            d.line((main_x,bottom+20,main_x+54,bottom+20),fill='black',width=2)
            d.text((main_x,bottom+39),fit(d,cfg.get('empty_calendar_author','Matka Tereza'),font(17),main_width),font=font(17),fill='black',anchor='lt')
    else:
        def event_when(event):
            st=event['start']
            label='DNES' if st.date()==now.date() else ('ZÍTRA' if st.date()==now.date()+timedelta(days=1) else f'{st.day}. {MONTHS[st.month-1]}')
            return label, 'CELÝ DEN' if event['all_day'] else st.strftime('%H:%M')
        first=events[0]
        label,when=event_when(first)
        active=is_running(first,now)
        d.text((main_x,40),'ŠKOLNÍ AGENDA',font=font(18,True),fill=RED,anchor='lt')
        if active:
            d.rectangle((main_x-16,69,784,260),fill=RED)
        d.text((main_x,79),('PRÁVĚ PROBÍHÁ' if active else label)+'  /  '+when,
               font=font(18,True),fill='white' if active else RED,anchor='lt')
        for i,line in enumerate(event_title_lines(d,first['summary'],font(63,True),main_width)):
            d.text((main_x,128+i*66),line,font=font(63,True),fill='white' if active else 'black',anchor='lt')
        for i,event in enumerate(events[1:4]):
            y=274+i*56
            active=is_running(event,now)
            if active:
                d.rectangle((main_x-16,y-2,784,y+49),fill=RED)
            else:
                d.line((main_x,y-8,768,y-8),fill='black')
            label,when=event_when(event)
            d.text((main_x,y+2),('PRÁVĚ PROBÍHÁ' if active else label)+'  /  '+when,
                   font=font(12,True),fill='white' if active else RED,anchor='lt')
            d.text((main_x,y+22),fit(d,event['summary'],font(21),main_width),font=font(21),
                   fill='white' if active else 'black',anchor='lt')
    d.line((24,252,168,252),fill='white')
    for i,(metric,value) in enumerate(metric_rows(cfg,values)):
        y = 279+i*76
        d.text((24,y),metric['label'],font=font(14,True),fill='white',anchor='lt')
        size=28
        while size>12 and d.textlength(value,font=font(size,True))>152: size-=1
        d.text((24,y+26),fit(d,value,font(size,True),152),font=font(size,True),fill='white',anchor='lt')
    status='UKÁZKOVÁ DATA' if demo else (' · '.join(errors) if errors else '')
    d.text((main_x,468),fit(d,status,font(10),300),font=font(10),fill=RED,anchor='lt')
    d.text((768,468),'Aktualizace '+f'{now.day}. {now.month}. {now.hour}:{now.minute:02d}',font=font(10),fill='black',anchor='rt')
    # Disable antialiasing: only exact white/black/red are sent and previewed.
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r,g,b = pixels[x,y]
            pixels[x,y] = RED if r > g*1.4 and r > b*1.4 and g < 180 else ((0,0,0) if (r+g+b) < 570 else (255,255,255))
    return image

def planes(image):
    black, red = bytearray([255])*(WIDTH*HEIGHT//8), bytearray([255])*(WIDTH*HEIGHT//8)
    for i, pixel in enumerate(image.getdata()):
        target = black if pixel == (0,0,0) else red if pixel == RED else None
        if target is not None:
            target[i//8] &= ~(0x80 >> (i%8))
    return bytes(black + red)

def packet(image, sleep):
    data = planes(image)
    # network byte order: magic, dimensions, seconds to wake, body CRC32
    return struct.pack('>4sHHII', b'EIF1', WIDTH, HEIGHT, sleep, zlib.crc32(data)) + data

class Dashboard:
    def __init__(self, cfg, demo, empty_calendar=False):
        self.cfg, self.demo, self.empty_calendar = cfg, demo, empty_calendar
        self.lock = threading.Lock()
    def build(self):
        with self.lock:
            now = datetime.now(ZoneInfo(self.cfg['timezone']))
            events, values, errors = collect(self.cfg, now, self.demo)
            if self.demo and self.empty_calendar:
                events = []
            image = render(self.cfg, now, events, values, errors, self.demo)
            sleep = 300 if errors else next_wake(datetime.now(now.tzinfo), self.cfg, events)
            return image, packet(image, sleep)

def create_server(dashboard, host, port):
    token = os.environ.get('FRAME_TOKEN', '')
    if not token:
        raise ValueError('Nastav FRAME_TOKEN; chrání agendu i obraz.')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token):
                self.send_error(401); return
            if self.path not in ('/frame.bin', '/preview.png'):
                self.send_error(404); return
            image, data = dashboard.build()
            mime = 'application/octet-stream'
            if self.path == '/preview.png':
                buf = io.BytesIO(); image.save(buf, format='PNG'); data = buf.getvalue(); mime = 'image/png'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(data)
        def log_message(self, fmt, *args):
            print('HTTP', args[1] if len(args)>1 else '', flush=True)
    return ThreadingHTTPServer((host, port), Handler)

def serve(dashboard, host, port):
    create_server(dashboard, host, port).serve_forever()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', default=str(ROOT/'config.example.json'))
    p.add_argument('--demo', action='store_true')
    p.add_argument('--empty-calendar', action='store_true', help='Prázdný kalendář v demo režimu')
    p.add_argument('--preview', type=Path)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8080)
    args = p.parse_args()
    cfg = validate(json.loads(Path(args.config).read_text()))
    if not args.demo and not all(os.environ.get(k) for k in ('HA_URL','HA_TOKEN')):
        p.error('Živý provoz vyžaduje HA_URL a HA_TOKEN; pro ukázku použij --demo')
    if args.empty_calendar and not args.demo:
        p.error('--empty-calendar vyžaduje --demo')
    dashboard = Dashboard(cfg, args.demo, args.empty_calendar)
    if args.preview:
        image, data = dashboard.build(); args.preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.preview); args.preview.with_suffix('.bin').write_bytes(data)
        print(args.preview)
    else:
        serve(dashboard, args.host, args.port)

if __name__ == '__main__':
    main()
```


## renderer/__init__.py

```python

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
version: "0.3.0"
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
import json
import os
from pathlib import Path
from renderer.app import Dashboard, serve, validate

def config(options):
    return validate({
        'timezone': options['timezone'],
        'calendar_entities': options['calendar_entities'],
        'agenda_days': options['agenda_days'],
        'empty_calendar_quote': options.get('empty_calendar_quote', 'Můžeme dělat malé věci s velkou láskou.'),
        'empty_calendar_author': options.get('empty_calendar_author', 'Matka Tereza'),
        'metrics': [
            {'entity': options['outdoor_entity'], 'label': 'VENKU', 'unit': '°C'},
            {'entity': options['indoor_entity'], 'label': 'DOMA', 'unit': '°C'},
            {'entity': options['max_temperature_entity'], 'label': 'MAX DNES', 'unit': '°C'}],
        'schedule': {key: options[key] for key in
                     ('day_start', 'night_start', 'day_minutes', 'night_minutes')}})

def main():
    options = json.loads(Path('/data/options.json').read_text())
    token = options['frame_token']
    if len(token) < 24:
        raise ValueError('frame_token musí mít alespoň 24 znaků')
    os.environ['FRAME_TOKEN'] = token
    os.environ['HA_URL'] = 'http://supervisor/core'
    os.environ['HA_TOKEN'] = os.environ['SUPERVISOR_TOKEN']
    serve(Dashboard(config(options), options['demo'], options.get('demo_empty_calendar', False)), '0.0.0.0', 8080)

if __name__ == '__main__': main()
```


## requirements.txt

```text
Pillow>=11.1,<13
```


## requirements-dev.txt

```text
-r requirements.txt
PyYAML>=6,<7
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
```


## tools/generate_previews.py

```python
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
```


## tools/package_project.py

```python
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
```


## tests/test_renderer.py

```python
import json
import struct
import unittest
import zlib
import threading
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import importlib.util
import yaml
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from PIL import Image
from renderer import app

CFG = app.validate(json.loads(Path('config.example.json').read_text()))
ZONE = ZoneInfo('Europe/Prague')

class RendererTests(unittest.TestCase):
    def test_bitplanes_and_crc(self):
        image = Image.new('RGB', (800, 480), 'white')
        image.putpixel((0, 0), (0, 0, 0))
        image.putpixel((7, 0), app.RED)
        data = app.packet(image, 1800)
        self.assertEqual(len(data), 96016)
        magic, width, height, sleep, crc = struct.unpack('>4sHHII', data[:16])
        self.assertEqual((magic,width,height,sleep), (b'EIF1',800,480,1800))
        self.assertEqual(data[16], 0x7f)
        self.assertEqual(data[16+48000], 0xfe)
        self.assertEqual(crc, zlib.crc32(data[16:]))
        self.assertNotEqual(crc, zlib.crc32(data[17:]))

    def test_schedule_boundaries_and_event(self):
        now = datetime(2026,10,8,21,45,tzinfo=ZONE)
        self.assertEqual(app.next_wake(now, CFG), 900)
        now = now.replace(hour=22,minute=1)
        self.assertEqual(app.next_wake(now, CFG), 7140)
        event = {'start': now.replace(minute=12), 'end': now.replace(minute=20)}
        self.assertEqual(app.next_wake(now,CFG,[event]),660)

    def test_dst_spring_and_fall(self):
        # Czech spring jump: 01:59 -> 03:00. Next night slot 04:00.
        now = datetime(2026,3,29,1,59,tzinfo=ZONE)
        self.assertEqual(app.next_wake(now,CFG),3660)
        # Both occurrences of 02:00 are legal slots during autumn switch.
        now = datetime(2026,10,25,2,1,tzinfo=ZONE,fold=0)
        self.assertEqual(app.next_wake(now,CFG),3540)

    def test_calendar_and_missing_metric(self):
        now = datetime(2026,10,8,12,tzinfo=ZONE)
        def fake(path):
            if path.startswith('/api/calendars/'):
                return [
                    {'summary':'Celý den','start':{'date':'2026-10-08'},'end':{'date':'2026-10-09'}},
                    {'summary':'Minulost','start':{'dateTime':'2026-10-08T08:00:00+02:00'},'end':{'dateTime':'2026-10-08T09:00:00+02:00'}}]
            if 'outdoor_temperature' in path: return {'state':'12.4','attributes':{}}
            return {'state':'unavailable'}
        with patch.object(app,'api',side_effect=fake):
            events,values,errors = app.collect(CFG,now,False)
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['all_day'])
        self.assertEqual(values,['12,4 °C','—','—'])
        self.assertTrue(errors)

    def test_calendar_lifecycle_and_background(self):
        now = datetime(2026,10,8,8,30,tzinfo=ZONE)
        events,values,errors = app.collect(CFG,now,True)
        current=events[0]
        self.assertTrue(app.is_running(current,now))
        self.assertTrue(app.is_running(current,current['start']))
        self.assertFalse(app.is_running(current,current['end']))
        self.assertNotIn(current,app.visible_events(events,current['end']))
        image=app.render(CFG,now,events,values,errors,True)
        self.assertEqual(image.getpixel((210,72)),app.RED)
        self.assertEqual(image.getpixel((1,479)),(0,0,0))
        future=app.render(CFG,current['start'].replace(hour=7),events,values,errors,True)
        self.assertEqual(future.getpixel((210,72)),(255,255,255))
        all_day={'summary':'Celý den','all_day':True,
                 'start':datetime(2026,10,8,tzinfo=ZONE),
                 'end':datetime(2026,10,9,tzinfo=ZONE)}
        self.assertTrue(app.is_running(all_day,now))
        self.assertEqual(app.visible_events([all_day],all_day['end']),[])

    def test_max_temperature_entity(self):
        now = datetime(2026,10,8,12,tzinfo=ZONE)
        def fake(path):
            if path.startswith('/api/calendars/'): return []
            if 'outdoor_effective_temperature' in path: return {'state':'16.8','attributes':{}}
            return {'state':'12.4','attributes':{}}
        with patch.object(app,'api',side_effect=fake):
            _,values,errors = app.collect(CFG,now,False)
        self.assertEqual(values[-1],'16,8 °C')
        self.assertEqual(errors,[])

    def test_addon_config_matches_entities(self):
        spec = importlib.util.spec_from_file_location('entrypoint','addon/eink_frame/entrypoint.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        options = yaml.safe_load(Path('addon/eink_frame/config.yaml').read_text())['options']
        self.assertEqual(module.config(options), CFG)

    def test_http_auth_and_payload(self):
        with patch.dict(app.os.environ, {'FRAME_TOKEN': 'test-token'}):
            server = app.create_server(app.Dashboard(CFG,True),'127.0.0.1',0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url = 'http://127.0.0.1:%s' % server.server_port
        urlopen = build_opener(ProxyHandler({})).open
        try:
            with self.assertRaises(HTTPError) as caught: urlopen(url+'/frame.bin')
            self.assertEqual(caught.exception.code,401)
            caught.exception.close()
            req = Request(url+'/frame.bin',headers={'Authorization':'Bearer test-token'})
            with urlopen(req) as response:
                data = response.read()
                self.assertEqual(len(data),96016)
                self.assertEqual(zlib.crc32(data[16:]),struct.unpack('>I',data[12:16])[0])
            req = Request(url+'/preview.png',headers={'Authorization':'Bearer test-token'})
            with urlopen(req) as response: self.assertEqual(response.read(8),b'\x89PNG\r\n\x1a\n')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_render_palette_and_size(self):
        now = datetime(2026,10,8,7,tzinfo=ZONE)
        events,values,errors = app.collect(CFG,now,True)
        image = app.render(CFG,now,events,values,errors,True)
        self.assertEqual(image.size,(800,480))
        self.assertEqual(set(image.getdata()),{(255,255,255),(0,0,0),app.RED})

if __name__ == '__main__': unittest.main()
```
