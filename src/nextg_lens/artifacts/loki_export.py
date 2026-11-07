"""Bounded Loki evidence export with explicit completeness information."""
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


def _query(url, selector, start_ns, end_ns, limit):
    params = urllib.parse.urlencode({'query': selector, 'start': start_ns,
        'end': end_ns, 'limit': limit, 'direction': 'forward'})
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/loki/api/v1/query_range?' + params, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read(1024).decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'Loki query returned HTTP {error.code}: {detail or error.reason}') from error
    if payload.get('status') != 'success' or payload.get('data', {}).get('resultType') != 'streams':
        raise ValueError('Loki did not return successful log streams')
    records = []
    for stream in payload['data']['result']:
        for value in stream['values']:
            records.append({'timestamp_ns': str(value[0]), 'labels': stream['stream'], 'line': value[1]})
    records.sort(key=lambda row: int(row['timestamp_ns']))
    return records


def collect_logs(directory, *, url, selector, start, end, limit=5000):
    """Split full pages into disjoint Loki windows, with a bounded query budget."""
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('Loki limit must be between 1 and 100000')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    page_limit = min(limit, 5000)
    pending = [(int(start * 1e9), int(end * 1e9))]
    records = []
    truncated = False
    queries = 0
    while pending and queries < 128:
        left, right = pending.pop()
        page = _query(url, selector, left, right, page_limit)
        queries += 1
        if len(page) >= page_limit and right - left > 1 and queries + len(pending) + 2 <= 128:
            middle = (left + right) // 2
            pending.extend(((middle, right), (left, middle)))
            continue
        records.extend(page)
        truncated |= len(page) >= page_limit
    with (directory / 'loki.jsonl').open('w', encoding='utf-8') as output:
        for record in records:
            output.write(json.dumps(record) + '\n')
    summary = {'start': start, 'end': end, 'selector': selector, 'limit': limit,
               'rows': len(records), 'possible_truncation': truncated or bool(pending),
               'page_limit': page_limit, 'queries': queries,
               'status': 'available' if records else 'empty'}
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary
