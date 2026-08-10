#!/usr/bin/env python3
"""
Watches the iCloud-synced Health Auto Export folder directly on disk and
pushes new weight / calorie data to the same GitHub Gist that index.html
reads from — so other devices see new exports without this Mac's browser
needing to be open.

Mirrors (does not call) the parsing/merge logic in index.html, since that
logic runs in a browser: EXPORT_RE, detectSchema, buildDailyMap,
buildCalorieMap, fetchGist/patchGist, mergeWithStored/mergeCaloriesWithStored.

One-time setup:
    security add-generic-password -s weight-tracker-watcher -a github-pat -w <your-github-pat>

Run manually to test:
    python3 weight_export_watcher.py
"""
import csv
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXPORT_FOLDER = Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / 'DrClaude'
GIST_ID = 'aa6e7ae2259f94c7a91637b447a0cd99'
GIST_FILE = 'weight-data.json'
API_BASE = 'https://api.github.com'
KEYCHAIN_SERVICE = 'weight-tracker-watcher'
KEYCHAIN_ACCOUNT = 'github-pat'
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
    return new_daily, meta, calorie_data


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


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    folder = EXPORT_FOLDER
    if not folder.is_dir():
        print(f'[watcher] Export folder not found: {folder}')
        sys.exit(1)

    latest = find_latest_export(folder)
    if latest is None:
        print('[watcher] No matching export files found.')
        return

    state = load_state()
    if state.get('lastFile') == latest.name:
        print(f'[watcher] Already up to date ({latest.name}).')
        return

    print(f'[watcher] New export found: {latest.name}')
    text = latest.read_text(encoding='utf-8', errors='replace')
    new_daily, meta, calorie_data = process_csv_text(text)

    pat = get_pat()
    gist = fetch_gist(pat)
    merged_entries = merge_entries(gist.get('entries', []), new_daily)
    merged_calories = merge_calories(gist.get('calories', []), calorie_data)
    payload = {
        'version': 1,
        'savedAt': datetime.now(timezone.utc).isoformat(),
        'meta': meta,
        'injSchedule': gist.get('injSchedule', []),
        'injections': gist.get('injections', []),
        'entries': merged_entries,
        'calories': merged_calories,
        # This is a full-file PATCH, not a merge — any field fetched-and-not-
        # re-sent here gets silently deleted from the Gist. calorieTargets,
        # proteinTargets (and intervalsCfg) have no watcher-side concept of
        # their own, so just pass through whatever's already there untouched.
        'calorieTargets': gist.get('calorieTargets', []),
        'proteinTargets': gist.get('proteinTargets', []),
    }
    if gist.get('intervalsCfg'):
        payload['intervalsCfg'] = gist['intervalsCfg']
    patch_gist(pat, payload)

    state['lastFile'] = latest.name
    save_state(state)
    print(f'[watcher] Pushed {len(merged_entries)} weight rows, {len(merged_calories)} calorie rows to Gist. ({latest.name})')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        print(f'[watcher] ERROR: {e}')
        sys.exit(1)
