"""モンストのキャンペーン日程のみをDiscordへ投稿するBot。
情報源は公式キャンペーン記事を優先し、攻略サイトは補助候補として参照。
"""
import hashlib
import logging
from urllib.robotparser import RobotFileParser
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from campaign_feed import export_feed

JST = ZoneInfo('Asia/Tokyo')
OFFICIAL = 'https://www.monster-strike.com/news/campaign/'
SITES = {'GameWith': 'gamewith.jp', 'Game8': 'game8.jp', 'AppMedia': 'appmedia.jp', 'アルテマ': 'altema.jp'}
ISSUES = []
ROBOTS = {}
SEARCHES = ['モンスト キャンペーン 追憶の書庫', 'モンスト 英雄の神殿 ノーマルクエスト キャンペーン']
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; MonstCampaignCalendar/2.0)'}
STATE = Path('seen.json')
CATEGORIES = ['📚 追憶の書庫', '💎 英雄の神殿', '✨ ノーマルクエスト', '⚔️ 絶級・降臨', '🎁 その他']
DATE_RX = r'(?:(?:20\d{2})[年/.-])?\s*\d{1,2}\s*(?:月|[/.-])\s*\d{1,2}\s*日?'
DATE_PART = re.compile(DATE_RX)
DATE_RANGE = re.compile(rf'({DATE_RX})\s*(?:[（(][月火水木金土日祝]+[）)])?\s*(?:\d{{1,2}}:\d{{2}})?\s*[～〜~－–—-]\s*({DATE_RX})')
ATTR = re.compile(r'(火|水|木|光|闇)属性')
KEYS = re.compile('追憶の書庫|書庫ポイント|英雄の神殿|わくわくの実|ノーマルクエスト|ノマクエ|絶級|超絶|爆絶|轟絶|黎絶|降臨|キャンペーン|報酬|経験値|金卵|モンスポット|強化')


def clean(s):
    return re.sub(r'\s+', ' ', s).strip()


def issue(message):
    ISSUES.append(message)
    logging.warning(message)


def fetch(url):
    host = urlparse(url).hostname
    if urlparse(url).scheme != 'https' or host not in {'www.monster-strike.com', *SITES.values()}:
        raise ValueError('許可されていない取得先')
    origin = 'https://' + host
    if origin not in ROBOTS:
        r = requests.get(origin + '/robots.txt', headers=HEADERS, timeout=22, allow_redirects=False)
        if r.status_code == 404:
            ROBOTS[origin] = None
        else:
            r.raise_for_status()
            if r.status_code != 200: raise ValueError('robots.txtを確認できません')
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            ROBOTS[origin] = rp
    rp = ROBOTS[origin]
    if rp and not rp.can_fetch(HEADERS['User-Agent'], url):
        raise ValueError('robots.txtにより取得停止')
    time.sleep(1)
    r = requests.get(url, headers=HEADERS, timeout=22, allow_redirects=False)
    r.raise_for_status()
    if r.status_code != 200: raise ValueError('リダイレクト・空応答は取得停止')
    r.encoding = 'utf-8'
    return BeautifulSoup(r.text, 'html.parser')


def date_value(s, default_year):
    m = re.search(r'(?:(20\d{2})[年/.-])?\s*(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})', s)
    if not m:
        return None
    try:
        return date(int(m.group(1) or default_year), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def category(text):
    if re.search('追憶の書庫|書庫ポイント', text): return CATEGORIES[0]
    if re.search('英雄の神殿|わくわくの実.*(?:排出|報酬)|感謝の間', text): return CATEGORIES[1]
    if re.search('ノーマルクエスト|ノマクエ', text): return CATEGORIES[2]
    if re.search('絶級|超絶|爆絶|轟絶|黎絶|降臨', text): return CATEGORIES[3]
    return CATEGORIES[4] if KEYS.search(text) else None


def label(heading):
    h = clean(heading).replace('！', '').replace('!', '')
    if '追憶の書庫' in h and re.search(r'(?:消費|ポイント).*?(?:0|ゼロ)', h): return '書庫ポイント消費0'
    if 'ノーマルクエスト' in h and ('経験値' in h or 'EXP' in h):
        mult = re.search(r'\d+(?:\.\d+)?倍', h)
        return '獲得経験値' + (mult.group() if mult else 'アップ')
    if '英雄の神殿' in h:
        if '2個' in h: return 'ノーコン報酬 わくわくの実2個'
        return '英雄の神殿キャンペーン'
    if 'モンスポット' in h and '学び' in h: return '学びのパワー出現率アップ'
    if '金卵' in h: return '金卵排出率アップ'
    if '追憶の書庫' in h and '絶級' in h and '2倍' in h: return '書庫の絶級クエスト：スペシャル報酬2倍'
    if re.search('絶級|超絶|爆絶|轟絶|黎絶|降臨|超爆轟黎', h): return '絶級・降臨キャンペーン'
    # 記事タイトルの流用は禁止。内容が特定できないものは表示しない。
    return None


def zekkyu_detail(heading, lines):
    """絶級・降臨の見出しと同じ節に明記された特典・対象のみを返す。

    曖昧な場合は掲載しない（他カテゴリのキャンペーンを混ぜない）。
    """
    title = clean(heading)
    # 別カテゴリを優先して除外
    if re.search(r'追憶の書庫|書庫ポイント|英雄の神殿|わくわくの実|ノーマルクエスト|ノマクエ', title):
        return None
    if not re.search(r'絶級|超絶|爆絶|轟絶|黎絶|降臨', title):
        return None
    # キャンペーン内容は必ず同じ見出し内の文字列で確認する。
    text = ' '.join([title] + lines[:24])
    benefits = [
        (r'(?:スペシャル報酬|ノーコン報酬|クリア報酬|報酬)[^。\n]{0,35}?(?:2倍|３倍|3倍|アップ|増加|追加|確定)', '報酬アップ'),
        (r'(?:金卵|宝箱)[^。\n]{0,25}?(?:2倍|確率アップ|排出率アップ|増加)', '金卵・宝箱の獲得率アップ'),
        (r'(?:消費スタミナ|スタミナ消費|スタミナ)[^。\n]{0,20}?(?:1/2|半分|半減|0|ゼロ)', '消費スタミナ減少'),
        (r'(?:経験値|EXP)[^。\n]{0,20}?(?:2倍|3倍|アップ)', '獲得経験値アップ'),
    ]
    benefit = None
    for pat, fallback in benefits:
        m = re.search(pat, text)
        if m:
            phrase = clean(m.group())
            benefit = phrase if len(phrase) <= 40 else fallback
            break
    if not benefit:
        return None
    targets = [x for x in ('黎絶', '轟絶', '爆絶', '超絶') if x in title]
    # 本文全体から対象を推測しない。見出しに書かれている対象だけ表示。
    target = '・'.join(targets) if targets else ('絶級・降臨' if '絶級' in title or '降臨' in title else None)
    if not target:
        return None
    return f'{target}：{benefit}'


def section_text(node):
    """見出しから次の同レベル以上の見出しまでの内容を取得。"""
    parts = []
    level = int(node.name[1]) if node.name and re.fullmatch(r'h[1-6]', node.name) else 3
    for nxt in node.next_elements:
        if nxt is node: continue
        if isinstance(nxt, Tag) and re.fullmatch(r'h[1-6]', nxt.name or '') and int(nxt.name[1]) <= level:
            break
        if isinstance(nxt, Tag) and nxt.name in ('tr', 'p', 'li', 'div'):
            # divの大きなブロックによる別セクション混入を防止
            if nxt.name == 'div': continue
            s = clean(nxt.get_text(' ', strip=True))
            if s and (not parts or parts[-1] != s): parts.append(s)
        if len(parts) > 180: break
    return parts


def extract_article(url, source, today):
    soup = fetch(url)
    for tag in soup.select('script,style,nav,footer,header'):
        tag.decompose()
    main = soup.select_one('article, main, .newsDetail, .news_detail, #contents') or soup.body or soup
    published = re.search(r'/news/(20\d{2})(\d{2})(\d{2})_', url)
    article_year = int(published.group(1)) if published else today.year
    records = []
    headings = main.select('h2,h3,h4')
    print(f'[{source}] 見出し数={len(headings)} URL={url}')
    for heading in headings:
        title = clean(heading.get_text(' ', strip=True))
        cat = category(title)
        name = label(title)
        if not cat: continue
        if not name:
            issue(f'未確認：{source}のキャンペーン内容を安全に分類できません')
            continue
        lines = section_text(heading)
        if not lines: continue
        blob = '\n'.join(lines)
        ranges = DATE_RANGE.findall(blob)
        periods = []
        for a, b in ranges:
            start = date_value(a, article_year)
            end = date_value(b, start.year if start else today.year)
            if start and end and end < start and start.month == 12 and end.month == 1 and not re.search(r'20\d{2}', b):
                try: end = end.replace(year=end.year + 1)
                except ValueError: continue
            if start and end and start <= end and end >= today and start <= today + timedelta(days=120):
                periods.append((start, end))
        # 日付・属性の対応が表で確認できる場合のみ、日別属性を紐付ける
        daily = []
        for line in lines:
            matches = DATE_PART.findall(line)
            attrs = ATTR.findall(line)
            if len(matches) == 1 and len(attrs) == 1 and not re.search('[～〜~]', line):
                d = date_value(matches[0], article_year)
                if d and periods and d < min(a for a, _ in periods) and d.month == 1 and not re.search(r'20\d{2}', matches[0]):
                    d = date_value(matches[0], article_year + 1)
                if d and today <= d <= today + timedelta(days=120):
                    daily.append((d, attrs[0]))
        if not periods:
            issue(f'未確認：開催期間を確定できません（{source}）')
            continue
        # 絶級・降臨だけの安全策：複数の日付行から隣接日を誤って
        # 「開始〜終了」と解釈した場合に、連続した期間を大量表示しない。
        # 見出し内で開催期間が一意に確定できない場合は掲載しない。
        if cat == CATEGORIES[3]:
            distinct = sorted(set(periods))
            if len(distinct) != 1 or len(ranges) != 1:
                issue('未確認：絶級・降臨の開催期間が一意に確定できません')
                print(f'[絶級・降臨] 期間が一意に確定できないため掲載を保留: {title[:60]} / 候補={len(distinct)}件')
                continue
            detail = zekkyu_detail(title, lines)
            if not detail:
                issue('未確認：絶級・降臨の対象または特典が確定できません')
                print(f'[絶級・降臨] 内容または対象を確認できず掲載保留: {title[:60]}')
                continue
            name = detail
        for start, end in sorted(set(periods)):
            days = [(d, a) for d, a in daily if start <= d <= end]
            records.append({'category': cat, 'name': name, 'start': start.isoformat(),
                            'end': end.isoformat(), 'days': [(d.isoformat(), a) for d, a in days],
                            'source': source, 'url': url})
    print(f'[{source}] 確認できた日程={len(records)}件')
    return records


def official_urls():
    soup = fetch(OFFICIAL)
    out = []
    for a in soup.select('a[href]'):
        url = urljoin(OFFICIAL, a.get('href', '')).split('#')[0]
        if re.search(r'/news/20\d{6}_\d+\.html$', urlparse(url).path) and url not in out:
            out.append(url)
    if not out: issue('未確認：公式一覧から記事リンクを抽出できません')
    return out[:20]


def external_urls():
    # 検索エンジン経由の無差別取得を廃止。利用条件を確認した記事だけ明示設定。
    config = Path('sources.json')
    if not config.exists(): return []
    out = []
    for item in json.loads(config.read_text(encoding='utf-8')):
        source = item.get('source')
        if not item.get('enabled'): continue
        if source not in SITES or not item.get('terms_reviewed_on') or not item.get('permission_note'):
            raise ValueError('補助情報源には利用条件の確認記録が必要です')
        date.fromisoformat(item['terms_reviewed_on'])
        for url in item.get('urls', []):
            if urlparse(url).hostname != SITES[source]: raise ValueError('補助情報源のドメイン不一致')
            out.append((url, source))
    return list(dict.fromkeys(out))[:18]


def gather(today):
    candidates = []
    try:
        candidates += [(u, '公式') for u in official_urls()]
    except (requests.RequestException, ValueError):
        issue('未確認：公式一覧の取得失敗')
    candidates += external_urls()
    print('候補件数:', len(candidates), '内訳:', {s: sum(src == s for _, src in candidates) for s in ['公式', *SITES]})
    records = []
    for url, source in candidates:
        try:
            records.extend(extract_article(url, source, today))
        except (requests.RequestException, ValueError) as e:
            issue(f'未確認：{source}の記事取得失敗')
    # 公式を優先。同一カテゴリ・内容・期間を重複表示しない。
    records.sort(key=lambda x: (x['source'] != '公式', x['start'], x['name']))
    unique = {}
    for r in records:
        key = (r['category'], r['name'], r['start'], r['end'])
        if key not in unique: unique[key] = r
    print('一覧掲載:', len(unique), '件')
    return list(unique.values())


def selected(records, today, weekly=False):
    end = today + timedelta(days=6) if weekly else today
    return [r for r in records if date.fromisoformat(r['start']) <= end
            and date.fromisoformat(r['end']) >= today]


def split_messages(text, limit=1800):
    # DiscordのUTF-16換算でも上限を超えない。異常に長い1行にも対応。
    result, buf, size = [], '', 0
    for char in text:
        n = len(char.encode('utf-16-le')) // 2
        if size + n > limit:
            result.append(buf); buf, size = '', 0
        buf += char; size += n
    if buf: result.append(buf)
    return result


def render(records, today, weekly=False):
    end = today + timedelta(days=6) if weekly else today
    title = f'{today:%Y/%m/%d}〜{end:%m/%d} モンスト 週間スケジュール' if weekly else f'{today:%Y/%m/%d} モンスト キャンペーン一覧'
    lines = ['🎁 **' + title + '**']
    for cat in CATEGORIES:
        items = [r for r in selected(records, today, weekly) if r['category'] == cat]
        if not items: continue
        lines.append('\n**' + cat + '**')
        for r in items:
            a, b = date.fromisoformat(r['start']), date.fromisoformat(r['end'])
            lines.append(f'・{r["name"]}｜{a:%m/%d}〜{b:%m/%d}' + ('【未確認：公式未照合】' if r['source'] != '公式' else ''))
            day_attrs = {}
            for ds, attr in r['days']:
                if today <= date.fromisoformat(ds) <= end:
                    day_attrs.setdefault(ds, set()).add(attr)
            for ds, attrs in sorted(day_attrs.items()):
                if len(attrs) != 1:
                    lines.append(f'  {ds} 属性未確認'); continue
                attr = next(iter(attrs))
                emoji = {'火':'🔥','水':'💧','木':'🌳','光':'✨','闇':'🌙'}[attr]
                lines.append(f'  {date.fromisoformat(ds):%m/%d} {emoji}{attr}属性')
            if r.get('url'): lines.append(f'  出典：<{r["url"]}>')
    if len(lines) == 1: lines.append('確認できた対象期間の日程はありません（開催なしとは限りません）。')
    if ISSUES: lines.append('⚠ 未確認：取得失敗または日程を確定できない情報があります。公式情報をご確認ください。')
    lines.append('\n※日程は取得時点の情報です。日付単位の掲載のため開始・終了時刻は出典をご確認ください。')
    return split_messages('\n'.join(lines))


def save_state(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATE)


def send(webhook, content):
    if not re.fullmatch(r'https://(?:discord.com|discordapp.com)/api/webhooks/\d+/[^/?#]+', webhook):
        raise ValueError('Discord Webhook URLの形式を確認してください')
    for attempt in range(4):
        try:
            r = requests.post(webhook + '?wait=true', json={'content': content, 'allowed_mentions': {'parse': []}}, timeout=25, allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('Discord通信失敗。配信状況を確認してから再実行してください') from None
        if r.status_code == 429:
            try: delay = min(60, max(1, float(r.json().get('retry_after', 2))))
            except (ValueError, TypeError): delay = 2
            time.sleep(delay); continue
        if not 200 <= r.status_code < 300:
            raise RuntimeError(f'Discord投稿失敗 HTTP {r.status_code}')
        return
    raise RuntimeError('Discordレート制限が継続しています')


def publish(state, records, today, weekly, webhook):
    key = ('weekly:' if weekly else 'daily:') + today.isoformat()
    if not weekly and state.get('last_campaign_morning') == today.isoformat(): return
    deliveries = state.setdefault('deliveries', {})
    if deliveries.get(key, {}).get('complete'): return
    # 初回の本文を固定し、途中失敗時に同じ続きから再開する。
    delivery = deliveries.setdefault(key, {'chunks': render(records, today, weekly), 'sent': 0})
    save_state(state)
    for i in range(delivery['sent'], len(delivery['chunks'])):
        send(webhook, delivery['chunks'][i])
        delivery['sent'] = i + 1
        save_state(state)
        time.sleep(1)
    delivery['complete'] = True
    if not weekly: state['last_campaign_morning'] = today.isoformat()
    save_state(state)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    mode = os.environ.get('MODE', 'morning')
    if mode not in ('morning', 'campaigns', 'weekly', 'check'): raise ValueError('不明なMODE')
    now = datetime.now(JST); today = now.date()
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if not isinstance(state, dict): raise ValueError('seen.jsonはオブジェクトである必要があります')
    try:
        records = gather(today)
    except Exception:
        if mode != 'check':
            try:
                export_feed([], ['gather_failed'], now)
            except Exception:
                logging.warning('アプリJSONの状態更新失敗（詳細は非公開）')
        raise
    # checkは従来どおりファイルを変更しない。JSON失敗はDiscord通知を妨げない。
    if mode != 'check':
        try:
            export_feed(records, ISSUES, now)
        except Exception:
            logging.warning('アプリJSONの更新失敗（前回データを保持）')
            try:
                export_feed([], ['export_failed'], now)
            except Exception:
                logging.warning('アプリJSONの状態更新失敗（詳細は非公開）')
    state['campaign_count'] = len(records)
    state['last_campaign_check'] = now.isoformat()
    state['last_issues'] = list(dict.fromkeys(ISSUES))
    if mode == 'check':
        for chunk in render(records, today): print(chunk)
        return
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook: raise RuntimeError('DISCORD_WEBHOOK_URL が未設定です')
    publish(state, records, today, mode == 'weekly', webhook)
    if mode == 'morning' and today.weekday() == 0:
        publish(state, records, today, True, webhook)
    save_state(state)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
