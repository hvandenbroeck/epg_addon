#!/usr/bin/env python3
"""Assess the heat-pump thermal pre-heating feature against real operation data.

Run this from the Advanced SSH add-on shell (it uses ``$SUPERVISOR_TOKEN`` to read Home Assistant
history and the EPG add-on's web UI on port 8099 for the current plan). It pulls the relevant
ebusd sensors, the committed plan and the predicted/measured temperature series, and prints a
Markdown-ready review block: comfort, setpoint chatter, load-shift, and prediction accuracy.

    python3 scripts/assess_preheat.py [hours_back]     # default 30

Workflow for a periodic review:
  1. Run it, read the printed block.
  2. Append the block (plus your findings and any changes made) to
     documentation/preheat_review_log.md.
  3. Implement improvements; note them under "Changes made" so the next review can attribute.

It makes no changes to Home Assistant or the add-on — read-only.
"""
import os
import sys
import json
import urllib.request
import urllib.parse
import datetime as dt

# ebusd entities for the zone-2 heat pump (match the default wp device in src/devices_config.py).
ROOM = 'sensor.ebusd_700_z2roomtemp'
SETPOINT = 'sensor.ebusd_700_z2quickvetotemp'
SFMODE = 'sensor.ebusd_700_z2sfmode'
FLOW = 'sensor.ebusd_700_hc2flowtemp'
OUTSIDE = 'sensor.ebusd_700_displayedoutsidetemp'
ENERGY = 'sensor.ebusd_700_prenergysumhc'
ENTITIES = [ROOM, SETPOINT, SFMODE, FLOW, OUTSIDE, ENERGY]

# Balanced comfort config (keep in step with the wp device's preheat_comfort_windows).
COMFORT_DEFAULT = 19.5
COMFORT_WINDOWS = [
    ('23:00', '06:30', 19.5, None),            # asleep
    ('06:30', '08:30', 20.5, None),            # home morning
    ('17:00', '23:00', 20.5, None),            # home evening
    ('08:30', '17:00', 20.5, {5, 6}),          # home weekends (sat/sun)
]
LOCAL_OFFSET_H = 1  # CET; informational only (history is converted via astimezone equivalent below)


def _ha_history(entity, start_utc, end_utc, token):
    q = urllib.parse.urlencode({'filter_entity_id': entity, 'end_time': end_utc, 'minimal_response': ''})
    url = f'http://supervisor/core/api/history/period/{start_utc}?{q}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    data = json.loads(urllib.request.urlopen(req, timeout=120).read())
    pts = []
    if data:
        for x in data[0]:
            s = x['state']
            if s in ('unknown', 'unavailable', None):
                continue
            t = dt.datetime.fromisoformat(x['last_changed'].replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
            try:
                pts.append((t, float(s)))
            except (ValueError, TypeError):
                pts.append((t, s))
    return pts


def _comfort_floor(when):
    mod = when.hour * 60 + when.minute
    floor = COMFORT_DEFAULT
    for start, end, mt, days in COMFORT_WINDOWS:
        sh, sm = map(int, start.split(':')); eh, em = map(int, end.split(':'))
        s = sh * 60 + sm; e = eh * 60 + em
        if start < end:
            inw, ref = s <= mod < e, when
        elif mod >= s:
            inw, ref = True, when
        elif mod < e:
            inw, ref = True, when - dt.timedelta(days=1)
        else:
            inw, ref = False, when
        if inw and (days is None or ref.weekday() in days):
            floor = max(floor, mt)
    return floor


def main():
    token = os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        print('SUPERVISOR_TOKEN not set — run from the SSH add-on shell.'); sys.exit(1)
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    now = dt.datetime.now(dt.timezone.utc)
    start_utc = (now - dt.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end_utc = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    hist = {e: _ha_history(e, start_utc, end_utc, token) for e in ENTITIES}

    # Current plan + predicted/measured temp from the add-on.
    plan = {}
    try:
        with urllib.request.urlopen('http://local-epg-addon:8099/download/db.json', timeout=20) as r:
            db = json.loads(r.read())['_default']
        plan['schedule'] = next(v for v in db.values() if isinstance(v, dict) and v.get('id') == 'schedule')
        plan['pred'] = next(v for v in db.values() if isinstance(v, dict) and v.get('id') == 'predictions')
    except Exception as e:
        print(f'(could not read add-on db.json: {e})')

    room = hist[ROOM]
    sp = hist[SETPOINT]
    rv = [v for _, v in room]
    out = [v for _, v in hist[OUTSIDE]]
    flow = [v for _, v in hist[FLOW]]

    print(f'\n## Review {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}  (last {hours:.0f} h)\n')
    print(f'- Data window: {start_utc} → {end_utc} (UTC); samples: room {len(room)}, setpoint {len(sp)}, flow {len(flow)}')

    # Comfort
    if rv:
        breaches = [(t, v, _comfort_floor(t)) for t, v in room if v < _comfort_floor(t) - 0.01]
        print(f'- **Comfort:** room {min(rv):.2f}–{max(rv):.2f} °C (mean {sum(rv)/len(rv):.2f}); '
              f'floor breaches: {len(breaches)}'
              + (f' — e.g. {breaches[0][1]:.2f} < {breaches[0][2]:.1f} at {breaches[0][0]:%a %H:%M}' if breaches else ' (comfort held)'))

    # Setpoint chatter
    if len(sp) > 1:
        span_h = (sp[-1][0] - sp[0][0]).total_seconds() / 3600 or 1
        changes = sum(1 for i in range(1, len(sp)) if sp[i][1] != sp[i - 1][1])
        reversals = sum(1 for i in range(2, len(sp)) if sp[i][1] == sp[i - 2][1] and sp[i][1] != sp[i - 1][1])
        distinct = sorted({v for _, v in sp})
        print(f'- **Setpoint:** {len(sp)} writes / {span_h:.1f} h = {len(sp)/span_h:.1f} per h; '
              f'{changes} value-changes, {reversals} reversals (A→B→A = chatter); '
              f'distinct values {distinct}')

    # sfmode
    modes = hist[SFMODE]
    if modes:
        print(f'- **Veto mode:** last = {modes[-1][1]!r} at {modes[-1][0]:%a %H:%M} '
              f'(veto means the add-on is in control)')

    # Load shift proxy: outside temp context + flow stats
    if out:
        print(f'- **Outside:** {min(out):.1f}–{max(out):.1f} °C (mean {sum(out)/len(out):.1f})')
    if flow:
        print(f'- **Flow temp:** {min(flow):.1f}–{max(flow):.1f} °C (mean {sum(flow)/len(flow):.1f}) '
              f'— >~18 means continuously circulating')

    # Energy (coarse)
    en = hist[ENERGY]
    if len(en) >= 2:
        print(f'- **Heating energy counter:** {en[0][1]:.0f} → {en[-1][1]:.0f} kWh '
              f'(Δ {en[-1][1]-en[0][1]:.0f} kWh over {(en[-1][0]-en[0][0]).total_seconds()/3600:.1f} h; '
              f'only {len(en)} points — too coarse for per-hour load-shift)')

    # Prediction accuracy: compare stored predicted vs measured where timestamps overlap.
    pred = plan.get('pred', {})
    tp = (pred.get('thermal_temp') or {}).get('wp', [])
    ta = (pred.get('thermal_temp_actual') or {}).get('wp', [])
    if tp and ta:
        pa = {p['timestamp']: p['temp'] for p in tp}
        errs = []
        for p in ta:
            # nearest predicted within 15 min
            at = dt.datetime.fromisoformat(p['timestamp'])
            best = None
            for ts, tv in pa.items():
                d = abs((dt.datetime.fromisoformat(ts) - at).total_seconds())
                if d <= 900 and (best is None or d < best[0]):
                    best = (d, tv)
            if best:
                errs.append(p['temp'] - best[1])
        if errs:
            rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5
            bias = sum(errs) / len(errs)
            print(f'- **Prediction accuracy (this horizon so far):** {len(errs)} overlaps, '
                  f'RMSE {rmse:.2f} °C, bias {bias:+.2f} °C (measured − predicted)')
    elif ta:
        print(f'- **Measured-temp log:** {len(ta)} points stored this horizon '
              f'(predicted-vs-measured accrues as the horizon plays out)')

    # Simple RC sanity: observed max |dT/dt| of room (control authority).
    if len(room) >= 3:
        rates = []
        for i in range(1, len(room)):
            dh = (room[i][0] - room[i - 1][0]).total_seconds() / 3600
            if 0.2 < dh < 4:
                rates.append((room[i][1] - room[i - 1][1]) / dh)
        if rates:
            print(f'- **Room dynamics:** observed dT/dt range {min(rates):+.3f}…{max(rates):+.3f} °C/h '
                  f'(small magnitude ⇒ weak/slow control authority; informs thermal C / max_heat tuning)')

    print('\n_(Paste this block into documentation/preheat_review_log.md with your findings and changes.)_\n')


if __name__ == '__main__':
    main()
