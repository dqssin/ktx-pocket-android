"""Validate web input and select at most three explicitly reviewed conditions."""
import json
from datetime import datetime
import engine

FIELDS = ('departure', 'arrival', 'date', 'start', 'end', 'passengers', 'option', 'seniors', 'children')


def prepare(body, credentials):
    entries = body.get('conditions')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
        raise ValueError('예약 조건은 1~3개로 입력하세요.')
    conditions = []
    for entry in entries:
        if not isinstance(entry, dict): raise ValueError('예약 조건을 확인하세요.')
        value = {key: entry.get(key) for key in FIELDS}
        value['seniors'] = entry.get('seniors', 0)
        value['children'] = entry.get('children', 0)
        for key in ('departure', 'arrival'):
            if not isinstance(value[key], str) or len(value[key]) > 40:
                raise ValueError('역 이름을 확인하세요.')
            value[key] = value[key].strip()
        if value not in conditions: conditions.append(value)
    access = body.get('station_access')
    config = dict(credentials, conditions=conditions, confirmed=True)
    if access is not None: config['station_access'] = access
    now = datetime.now(engine.KST)
    plans = engine.validated_conditions(config, now)
    preferences = json.loads(json.dumps({'conditions': conditions, 'station_access': access}))
    selected = [plan[0] for plan in plans if engine.earliest_departure(plan[0], plan[1], now) < plan[2]]
    if access is not None:
        stations, limit, _ = engine.validated_access(access)
        baseline = conditions[0]['departure']
        if access['baseline'] != baseline:
            raise ValueError('기준 출발역은 첫 예약 조건의 출발역과 같아야 합니다.')
        template = conditions[0]
        for name in sorted(stations, key=lambda name: (stations[name], name)):
            if len(selected) >= 3: break
            if stations[name] > limit or name == baseline: continue
            candidate = {**template, 'departure': name}
            plan = engine.validated_conditions({**config, 'conditions': [candidate]}, now)[0]
            if engine.earliest_departure(plan[0], plan[1], now) >= plan[2]: continue
            if not any(all(other[key] == candidate[key] for key in FIELDS) for other in selected):
                selected.append(plan[0])
    if not selected: raise ValueError('이동·승차 여유를 확보할 수 있는 조건이 없습니다. 시간 범위를 늘리세요.')
    config['conditions'] = [{key: value[key] for key in FIELDS} for value in selected[:3]]
    return config, preferences
