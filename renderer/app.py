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
