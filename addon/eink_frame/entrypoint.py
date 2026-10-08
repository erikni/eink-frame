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
