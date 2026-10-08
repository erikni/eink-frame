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
