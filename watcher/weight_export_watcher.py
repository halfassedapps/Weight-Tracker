#!/usr/bin/env python3
"""
Watches the iCloud-synced Health Auto Export folder directly on disk and
pushes new weight / calorie data to the same GitHub Gist that index.html
reads from — so other devices see new exports without this Mac's browser
needing to be open.

Mirrors (does not call) the parsing/merge logic in index.html, since that
logic runs in a browser: EXPORT_RE, detectSchema, buildDailyMap,
buildCalorieMap, buildBpMap (raw readings only — the sitting/outlier/daily-
average reduction is display-time-only, in BpChart, not mirrored here),
fetchGist/patchGist, mergeWithStored/mergeCaloriesWithStored/mergeBpWithStored.

Also owns the intervals.icu sync (mirrors the browser's former
fetchIntervalsCalories/pushWeightToIntervals, index.html): this runs here
instead of in the browser specifically so the API key never has to live in
the shared Gist, where any visitor with the page URL could read it. Only the
computed result (intervalsActivities — calorie/ride data, no key) is written.

One-time setup:
    security add-generic-password -s weight-tracker-watcher -a github-pat -w <your-github-pat>
    security add-generic-password -s weight-tracker-watcher -a intervals-icu -w '<API_KEY>:<athleteId>'
    (the second one is optional — skip it and the watcher just won't sync rides)

Run manually to test:
    python3 weight_export_watcher.py
"""
import base64
import csv
import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPORT_FOLDER = Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / 'DrClaude'
GIST_ID = 'aa6e7ae2259f94c7a91637b447a0cd99'
GIST_FILE = 'weight-data.json'
API_BASE = 'https://api.github.com'
KEYCHAIN_SERVICE = 'weight-tracker-watcher'
KEYCHAIN_ACCOUNT = 'github-pat'
INTERVALS_KEYCHAIN_ACCOUNT = 'intervals-icu'
INTERVALS_WINDOW_DAYS = 14
STATE_DIR = Path.home() / 'Library' / 'Application Support' / 'weight-tracker-watcher'
STATE_FILE = STATE_DIR / 'state.json'

EXPORT_RE = re.compile(r'^(Automation_Daily_export|HealthExport)_\d{4}-\d{2}-\d{2}_\d{6}(\.csv)?$', re.IGNORECASE)
TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{6})')

WEIGHT_METRIC_CANON = {'bodyweight', 'body weight', 'weight', 'bodymass', 'body mass'}


# ─── Keychain ────────────────────────────────────────────────────────────────
def get_pat():
    try:
        out = subprocess.run(
            ['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', KEYCHAIN_ACCOUNT, '-w'],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        print(f"No PAT found in Keychain. Run once:\n"
              f"  security add-generic-password -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT} -w <your-github-pat>")
        sys.exit(1)


# Optional — unlike the GitHub PAT, missing intervals.icu credentials just
# mean the ride sync is skipped for this run, not a fatal error. Stored as a
# single 'APIKEY:athleteId' value (athlete ID isn't secret, but one Keychain
# lookup is simpler than keeping two entries in sync).
def get_intervals_credentials():
    try:
        out = subprocess.run(
            ['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', INTERVALS_KEYCHAIN_ACCOUNT, '-w'],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None
    raw = out.stdout.strip()
    if ':' not in raw:
        print(f"[watcher] intervals.icu Keychain entry malformed (expected 'APIKEY:athleteId') — skipping ride sync.")
        return None
    api_key, athlete_id = raw.split(':', 1)
    if not api_key or not athlete_id:
        return None
    return api_key, athlete_id


# ─── Dedupe state ────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


# ─── Export folder ───────────────────────────────────────────────────────────
# Running headless under launchd (rather than an interactive Terminal session)
# can hit iCloud Drive files that haven't finished materializing locally yet —
# read_text() then raises OSError: [Errno 11] Resource deadlock avoided instead
# of just blocking. Nudge iCloud to fetch the file, then retry with backoff
# rather than failing the whole run (and waiting for the next 5-minute tick).
def read_export_text(path, attempts=6, delay=5):
    subprocess.run(['brctl', 'download', str(path)], capture_output=True)
    last_err = None
    for attempt in range(attempts):
        try:
            return path.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            last_err = e
            print(f'[watcher] Read failed (attempt {attempt + 1}/{attempts}): {e}')
            time.sleep(delay)
    raise last_err


def export_ts(name):
    m = TS_RE.search(name)
    return m.group(1) if m else ''


def find_latest_export(folder):
    matches = [p for p in folder.iterdir() if p.is_file() and EXPORT_RE.match(p.name)]
    if not matches:
        return None
    matches.sort(key=lambda p: export_ts(p.name), reverse=True)
    return matches[0]


# ─── Date parsing (mirrors index.html's msToLocalDate — local calendar day,
# not UTC, so this lines up with intervals.icu's start_date_local too) ──────
def parse_dt(raw):
    s = raw.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None
        for fmt in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_date_key(dt):
    return dt.astimezone().date().isoformat()


# ─── Schema detection (mirrors detectSchema, index.html:287-305) ───────────
def lc(h):
    return re.sub(r'[\s_\-]', '', h.lower())


def find_col(headers, keys):
    for h in headers:
        hl = lc(h)
        for k in keys:
            if hl == k or k in hl:
                return h
    return None


def detect_schema(headers, rows):
    date_col = find_col(headers, ['date', 'startdate', 'datetime', 'timestamp'])
    metric_col = find_col(headers, ['metric', 'metricname', 'type', 'category', 'name'])
    value_col = find_col(headers, ['value', 'qty'])
    unit_col = find_col(headers, ['unit', 'units'])
    if metric_col and value_col and date_col:
        seen, metric_values = set(), []
        for r in rows:
            m = r.get(metric_col)
            if m and m not in seen:
                seen.add(m)
                metric_values.append(m)
        weight_metric = None
        for m in metric_values:
            if re.sub(r'\s+', ' ', m.lower()).strip() in WEIGHT_METRIC_CANON:
                weight_metric = m
                break
        if weight_metric is None:
            weight_metric = next((m for m in metric_values if 'weight' in m.lower()), None)
        return {
            'format': 'long', 'dateCol': date_col, 'metricCol': metric_col,
            'valueCol': value_col, 'unitCol': unit_col,
            'weightMetric': weight_metric, 'allMetrics': metric_values,
        }
    wide_value = find_col(headers, ['weight', 'bodymass', 'body mass', 'mass', 'value'])
    if date_col and wide_value:
        return {'format': 'wide', 'dateCol': date_col, 'valueCol': wide_value, 'unitCol': unit_col}
    return {'format': 'unknown', 'dateCol': date_col, 'valueCol': value_col, 'unitCol': unit_col}


# ─── Extraction (mirrors buildDailyMap/buildCalorieMap, index.html:316-363) ─
def to_kg(weight, unit):
    u = (unit or '').lower().strip()
    if u in ('lb', 'lbs', 'pound', 'pounds'):
        return weight / 2.20462
    return weight


def build_daily_map(rows, schema):
    date_col, metric_col, value_col, unit_col = schema['dateCol'], schema['metricCol'], schema['valueCol'], schema.get('unitCol')
    weight_metric = schema['weightMetric']
    m = {}
    for row in rows:
        if row.get(metric_col) != weight_metric:
            continue
        raw_date = row.get(date_col)
        if not raw_date:
            continue
        dt = parse_dt(raw_date)
        if dt is None:
            continue
        try:
            w_raw = float((row.get(value_col) or '').replace(',', ''))
        except ValueError:
            continue
        w_kg = to_kg(w_raw, row.get(unit_col) if unit_col else '')
        key = local_date_key(dt)
        if key not in m or dt > m[key]['dt']:
            m[key] = {'date': key, 'wKg': w_kg, 'dt': dt}
    return sorted(({'date': v['date'], 'wKg': v['wKg']} for v in m.values()), key=lambda e: e['date'], reverse=True)


def build_calorie_map(rows, schema):
    date_col, metric_col, value_col = schema['dateCol'], schema['metricCol'], schema['valueCol']
    m, calorie_totals = {}, {}
    for row in rows:
        raw_date = row.get(date_col)
        if not raw_date:
            continue
        dt = parse_dt(raw_date)
        if dt is None:
            continue
        metric = row.get(metric_col)
        if not metric:
            continue
        key = local_date_key(dt)
        if key not in m:
            m[key] = {'date': key, 'Calories': 0.0, 'ActiveEnergy': 0.0, 'BasalEnergy': 0.0,
                       'Protein': 0.0, 'Carbs': 0.0, 'Fat': 0.0, 'HasWorkout': False}
        ml = metric.lower()
        # "Workout" rows use a different column layout (activity type + duration,
        # not a plain numeric value) — just flag the day, don't try to parse a number.
        if ml == 'workout':
            m[key]['HasWorkout'] = True
            continue
        try:
            val = float((row.get(value_col) or '').replace(',', ''))
        except ValueError:
            continue
        if 'calor' in ml:
            calorie_totals[key] = calorie_totals.get(key, 0.0) + val
        elif 'active energy' in ml:
            m[key]['ActiveEnergy'] += val
        elif 'basal energy' in ml:
            m[key]['BasalEnergy'] += val
        elif 'protein' in ml:
            m[key]['Protein'] += val
        elif 'carbohydrate' in ml:
            m[key]['Carbs'] += val
        elif 'total fat' in ml:
            m[key]['Fat'] += val
    for key, total in calorie_totals.items():
        if key in m:
            m[key]['Calories'] = total
    result = [d for d in m.values() if d['Calories'] > 0 or d['ActiveEnergy'] > 0]
    result.sort(key=lambda d: d['date'], reverse=True)
    return result


# ─── Blood pressure extraction (mirrors findBpMetrics/buildBpReadings/
# buildBpMap in index.html) ───────────────────────────────────────────────
# Raw readings only — no reduction here. Sitting/outlier/daily-average logic
# (clusterBpReadings/computeDailyBp in index.html) is display-time-only, lives
# entirely in BpChart, and isn't mirrored server-side: the watcher's job is
# just to get real readings into the Gist, not to decide how they're shown.
BP_SYSTOLIC_RE = re.compile(r'blood pressure.*systolic|systolic.*blood pressure', re.IGNORECASE)
BP_DIASTOLIC_RE = re.compile(r'blood pressure.*diastolic|diastolic.*blood pressure', re.IGNORECASE)


def find_bp_metrics(all_metrics):
    systolic = next((m for m in all_metrics if BP_SYSTOLIC_RE.search(m)), None)
    diastolic = next((m for m in all_metrics if BP_DIASTOLIC_RE.search(m)), None)
    return systolic, diastolic


def build_bp_readings(rows, schema):
    if schema['format'] != 'long':
        return []
    systolic, diastolic = find_bp_metrics(schema.get('allMetrics') or [])
    if not systolic or not diastolic:
        return []
    date_col, metric_col, value_col = schema['dateCol'], schema['metricCol'], schema['valueCol']
    by_ms = {}
    for row in rows:
        metric = row.get(metric_col)
        if metric not in (systolic, diastolic):
            continue
        raw_date = row.get(date_col)
        if not raw_date:
            continue
        dt = parse_dt(raw_date)
        if dt is None:
            continue
        try:
            val = float((row.get(value_col) or '').replace(',', ''))
        except ValueError:
            continue
        ms = int(dt.timestamp() * 1000)
        entry = by_ms.setdefault(ms, {'ms': ms})
        if metric == systolic:
            entry['systolic'] = val
        else:
            entry['diastolic'] = val
    readings = [r for r in by_ms.values() if 'systolic' in r and 'diastolic' in r]
    readings.sort(key=lambda r: r['ms'])
    return readings


def build_bp_map(rows, schema):
    out = []
    for r in build_bp_readings(rows, schema):
        date = datetime.fromtimestamp(r['ms'] / 1000, tz=timezone.utc).astimezone().date().isoformat()
        out.append({'date': date, **r})
    return out


def process_csv_text(text):
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise RuntimeError('CSV is empty.')
    headers = list(rows[0].keys())
    schema = detect_schema(headers, rows)
    if schema['format'] == 'unknown':
        raise RuntimeError(f"Couldn't identify schema. Headers: {', '.join(headers)}")
    if schema['format'] == 'long' and not schema.get('weightMetric'):
        sample = ', '.join((schema.get('allMetrics') or [])[:8])
        raise RuntimeError(f"No weight metric found. Metrics present: {sample}")
    new_daily = build_daily_map(rows, schema)
    if not new_daily:
        raise RuntimeError('No valid weight entries found.')
    calorie_data = build_calorie_map(rows, schema) if schema['format'] == 'long' else []

    src_col = next((h for h in headers if h.lower() == 'source'), None)
    w_rows = [r for r in rows if r.get(schema['metricCol']) == schema['weightMetric']] if schema['format'] == 'long' else rows
    sources, seen = [], set()
    if src_col:
        for r in w_rows:
            s = r.get(src_col)
            if s and s not in seen:
                seen.add(s)
                sources.append(s)
    meta = {'metric': schema.get('weightMetric') or 'Weight', 'source': ', '.join(sources)}
    bp_data = build_bp_map(rows, schema) if schema['format'] == 'long' else []
    return new_daily, meta, calorie_data, bp_data


# ─── Merge against current Gist (mirrors mergeWithStored/mergeCaloriesWithStored) ─
def merge_entries(existing_entries, new_daily):
    m = {e['date']: e['wKg'] for e in existing_entries if e.get('date') and e.get('wKg') is not None}
    for e in new_daily:
        m[e['date']] = e['wKg']
    return sorted(({'date': d, 'wKg': w} for d, w in m.items()), key=lambda e: e['date'], reverse=True)


def merge_calories(existing_calories, new_cal_rows):
    def norm(c):
        return {'date': c['date'], 'Calories': c.get('Calories') or 0,
                'ActiveEnergy': c.get('ActiveEnergy') or 0, 'BasalEnergy': c.get('BasalEnergy') or 0,
                'Protein': c.get('Protein') or 0, 'Carbs': c.get('Carbs') or 0, 'Fat': c.get('Fat') or 0,
                'HasWorkout': bool(c.get('HasWorkout'))}
    m = {c['date']: norm(c) for c in existing_calories if c.get('date')}
    for d in new_cal_rows:
        m[d['date']] = norm(d)
    return sorted(m.values(), key=lambda c: c['date'])


# Keyed by ms — each raw reading's own timestamp, a naturally stable and
# unique identity across re-imports. Mirrors mergeBpWithStored in index.html.
def merge_bp(existing_bp, new_readings):
    m = {r['ms']: r for r in existing_bp if r.get('ms') is not None}
    for r in new_readings:
        m[r['ms']] = r
    return sorted(m.values(), key=lambda r: r['ms'])


# ─── GitHub Gist (mirrors fetchGist/patchGist, index.html:204-242) ──────────
def fetch_gist(pat):
    req = urllib.request.Request(f'{API_BASE}/gists/{GIST_ID}', headers={
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'weight-tracker-watcher',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            gist = json.load(res)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Gist fetch failed: {e.code} {e.read().decode(errors="replace")}') from e
    content = gist.get('files', {}).get(GIST_FILE, {}).get('content')
    if not content:
        raise RuntimeError(f'Gist file {GIST_FILE} not found or empty.')
    return json.loads(content)


def patch_gist(pat, payload):
    body = json.dumps({'files': {GIST_FILE: {'content': json.dumps(payload, indent=2)}}}).encode('utf-8')
    req = urllib.request.Request(f'{API_BASE}/gists/{GIST_ID}', data=body, method='PATCH', headers={
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'weight-tracker-watcher',
    })
    try:
        urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Gist write failed: {e.code} {e.read().decode(errors="replace")}') from e


# A live client (phone/laptop) can successfully write to the Gist between our
# fetch and our eventual patch — since this is a full-file PATCH, not a
# merge, that write would otherwise be silently reverted the moment we patch
# with a payload built from a fetch taken before it landed. (This is exactly
# how a shot logged from a phone went missing: the phone's write succeeded,
# then this watcher's next scheduled run patched over it with stale state.)
# Re-fetch immediately before patching and rebuild the payload against
# whatever's actually there if anything changed; retry a few times in case
# of back-to-back writes, so the final patch is always built from state
# we've verified is current as of just before we sent it.
def fetch_build_and_patch(pat, build_payload, max_attempts=4,
                           fetch_gist=fetch_gist, patch_gist=patch_gist):
    gist = fetch_gist(pat)
    payload = build_payload(gist)
    for _ in range(max_attempts):
        fresh = fetch_gist(pat)
        if fresh.get('savedAt') == gist.get('savedAt'):
            break
        gist = fresh
        payload = build_payload(gist)
    patch_gist(pat, payload)
    return payload


# ─── intervals.icu (mirrors the browser's former fetchIntervalsCalories/
# pushWeightToIntervals, index.html — moved here so the API key stays local
# instead of living in the shared Gist) ──────────────────────────────────────
def fetch_intervals_activities(api_key, athlete_id, oldest, newest):
    auth = base64.b64encode(f'API_KEY:{api_key}'.encode()).decode()
    url = (f'https://intervals.icu/api/v1/athlete/{athlete_id}/activities'
           f'?oldest={oldest}&newest={newest}')
    req = urllib.request.Request(url, headers={
        'Authorization': f'Basic {auth}', 'User-Agent': 'weight-tracker-watcher',
    })
    with urllib.request.urlopen(req, timeout=20) as res:
        activities = json.load(res)
    cal_map, rides_map = {}, {}
    for a in activities:
        date = (a.get('start_date_local') or '')[:10]
        if not date:
            continue
        cal = a.get('calories')
        if cal is not None:
            cal_map[date] = cal_map.get(date, 0) + cal
        dur = a.get('moving_time') or a.get('elapsed_time') or 0
        dist = a.get('distance') or 0
        pwr = a.get('icu_average_watts', a.get('average_watts'))
        hr = a.get('average_heartrate')
        entry = rides_map.setdefault(date, {'date': date, 'totalDurSecs': 0, 'totalDistM': 0, 'totalCal': 0, 'rides': []})
        entry['totalDurSecs'] += dur
        entry['totalDistM'] += dist
        entry['totalCal'] += (cal or 0)
        entry['rides'].append({
            'id': a.get('id'), 'name': a.get('name') or 'Activity', 'sport': a.get('sport') or '',
            'durSecs': dur, 'distM': dist, 'cal': cal or 0, 'pwr': pwr, 'hr': hr,
        })
    return cal_map, rides_map


# PUT is idempotent — safe to call every run.
def push_weight_to_intervals(api_key, athlete_id, date_key, w_kg):
    auth = base64.b64encode(f'API_KEY:{api_key}'.encode()).decode()
    url = f'https://intervals.icu/api/v1/athlete/{athlete_id}/wellness/{date_key}'
    req = urllib.request.Request(url, data=json.dumps({'weight': w_kg}).encode(), method='PUT', headers={
        'Authorization': f'Basic {auth}', 'Content-Type': 'application/json',
        'User-Agent': 'weight-tracker-watcher',
    })
    urllib.request.urlopen(req, timeout=20)


# Pushes today's weight (if logged) and pulls a rolling window of recent
# activities, merging onto whatever's already in the Gist (older days outside
# the window are left untouched — mirrors the browser's old merge-on-top
# behavior). Best-effort: a failure here shouldn't abort the weight/CSV sync
# that's the watcher's primary job, so errors are logged, not raised.
def sync_intervals(creds, entries, existing_activities):
    api_key, athlete_id = creds
    today_key = datetime.now().astimezone().date().isoformat()
    today_entry = next((e for e in entries if e.get('date') == today_key and e.get('wKg') is not None), None)
    if today_entry:
        try:
            push_weight_to_intervals(api_key, athlete_id, today_key, today_entry['wKg'])
        except urllib.error.HTTPError as e:
            print(f'[watcher] intervals.icu weight push failed: {e.code}')
        except urllib.error.URLError as e:
            print(f'[watcher] intervals.icu weight push failed: {e.reason}')

    oldest = (datetime.now().astimezone().date() - timedelta(days=INTERVALS_WINDOW_DAYS)).isoformat()
    existing = existing_activities or {}
    try:
        cal_map, rides_map = fetch_intervals_activities(api_key, athlete_id, oldest, today_key)
    except urllib.error.HTTPError as e:
        print(f'[watcher] intervals.icu activities fetch failed: {e.code}')
        return existing or None
    except urllib.error.URLError as e:
        print(f'[watcher] intervals.icu activities fetch failed: {e.reason}')
        return existing or None

    return {
        'calMap': {**(existing.get('calMap') or {}), **cal_map},
        'ridesMap': {**(existing.get('ridesMap') or {}), **rides_map},
        'syncedAt': datetime.now(timezone.utc).isoformat(),
    }


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    folder = EXPORT_FOLDER
    if not folder.is_dir():
        print(f'[watcher] Export folder not found: {folder}')
        sys.exit(1)

    latest = find_latest_export(folder)
    state = load_state()
    has_new_export = latest is not None and state.get('lastFile') != latest.name

    intervals_creds = get_intervals_credentials()

    # Nothing to do: no new CSV to import, and no intervals.icu key to sync
    # rides/weight with independently of a new export.
    if not has_new_export and not intervals_creds:
        if latest is None:
            print('[watcher] No matching export files found.')
        else:
            print(f'[watcher] Already up to date ({latest.name}).')
        return

    new_daily = meta = calorie_data = bp_data = None
    if has_new_export:
        print(f'[watcher] New export found: {latest.name}')
        text = read_export_text(latest)
        new_daily, meta, calorie_data, bp_data = process_csv_text(text)

    pat = get_pat()

    def build_payload(gist):
        payload = {
            'version': 1,
            'savedAt': datetime.now(timezone.utc).isoformat(),
            'meta': meta if meta is not None else gist.get('meta'),
            'injSchedule': gist.get('injSchedule', []),
            'injections': gist.get('injections', []),
            'entries': merge_entries(gist.get('entries', []), new_daily) if new_daily is not None else gist.get('entries', []),
            'calories': merge_calories(gist.get('calories', []), calorie_data) if calorie_data is not None else gist.get('calories', []),
            'bloodPressure': merge_bp(gist.get('bloodPressure', []), bp_data) if bp_data is not None else gist.get('bloodPressure', []),
            # This is a full-file PATCH, not a merge — any field fetched-and-not-
            # re-sent here gets silently deleted from the Gist. calorieTargets/
            # proteinTargets have no watcher-side concept of their own, so just
            # pass through whatever's already there untouched.
            'calorieTargets': gist.get('calorieTargets', []),
            'proteinTargets': gist.get('proteinTargets', []),
        }
        if intervals_creds:
            payload['intervalsActivities'] = sync_intervals(intervals_creds, payload['entries'], gist.get('intervalsActivities'))
        elif gist.get('intervalsActivities'):
            payload['intervalsActivities'] = gist['intervalsActivities']
        return payload

    payload = fetch_build_and_patch(pat, build_payload)

    if has_new_export:
        state['lastFile'] = latest.name
        save_state(state)
    print(f"[watcher] Pushed {len(payload['entries'])} weight rows, {len(payload['calories'])} calorie rows, "
          f"{len(payload['bloodPressure'])} BP readings to Gist. ({latest.name if has_new_export else 'no new export'})")


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        print(f'[watcher] ERROR: {e}')
        sys.exit(1)
