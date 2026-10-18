"""Public feed: explicit allowlist, no state, logs or environment serialization."""
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

CATEGORIES = dict(zip(
    ['📚 追憶の書庫', '💎 英雄の神殿', '✨ ノーマルクエスト', '⚔️ 絶級・降臨', '🎁 その他'],
    ['archive', 'temple', 'normal', 'descent', 'other']))
HOSTS = {'公式': 'www.monster-strike.com', 'GameWith': 'gamewith.jp',
         'Game8': 'game8.jp', 'AppMedia': 'appmedia.jp', 'アルテマ': 'altema.jp'}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def campaign(record):
    category = CATEGORIES[record['category']]
    name = record['name']
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise ValueError('Invalid campaign name')
    start, end = (date.fromisoformat(record[k]) for k in ('start', 'end'))
    if end < start:
        raise ValueError('Invalid period')
    source, url = record['source'], record['url']
    parsed = urlparse(url)
    if (source not in HOSTS or parsed.scheme != 'https' or parsed.hostname != HOSTS[source]
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in (None, 443) or not parsed.path.startswith('/')):
        raise ValueError('Invalid source URL')
    days = set()
    for ds, attr in record['days']:
        d = date.fromisoformat(ds)
        if not start <= d <= end or attr not in ('火', '水', '木', '光', '闇'):
            raise ValueError('Invalid daily attribute')
        days.add((ds, attr))
    identity = [category, name, start.isoformat(), end.isoformat()]
    uid = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    return dict(id=uid, category=category, name=name, start_date=start.isoformat(),
                end_date=end.isoformat(), daily_attributes=[{'date': d, 'attribute': a} for d, a in sorted(days)],
                source={'name': source, 'url': url, 'official': source == '公式'})


def export_feed(records, issues, now, directory=Path('public')):
    """Keep the last good snapshot; mark incomplete results as degraded."""
    directory = Path(directory)
    feed_path = directory / 'campaigns.json'
    status = 'degraded'

    if records and (not issues or not feed_path.exists()):
        items = sorted(
            (campaign(r) for r in records),
            key=lambda r: (r['start_date'], r['id'])
        )
        atomic_json(feed_path, {
            'schema_version': 1,
            'generated_at': now.isoformat(),
            'timezone': 'Asia/Tokyo',
            'date_precision': 'day',
            'campaigns': items
        })
        if not issues:
            status = 'ok'

    atomic_json(directory / 'status.json', {
        'schema_version': 1,
        'checked_at': now.isoformat(),
        'status': status,
        'reason': None if status == 'ok' else 'incomplete_or_empty',
        'has_snapshot': feed_path.exists()
    })
