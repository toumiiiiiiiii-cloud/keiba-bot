# -*- coding: utf-8 -*-
"""
シェイクユアハート：不利リスト（JRAの「開催競馬場・今日の出来事」から、レース中に不利を受けた馬を集める）

・JRAは開催日ごとに「今日の出来事」を公開している。制裁の欄に
  「2番〇〇は、競走後半で外側に斜行したため、13番△△の進路が狭くなりました。」のように、
  どの馬が進路をふさがれた・ぶつけられたかが書いてある。競走中の疾病（心房細動・鼻出血など）も載る
・その「不利を受けた馬」「競走中に疾病を発症した馬」を data/trouble.json に貯める
・Botは、出走馬の前走（2走前）がこのリストにあれば、重点補正で加点して短評に書く
・GitHub Actions（trouble_list.yml）が月〜金の朝に動かす。初回は過去12か月分をまとめて読む
"""
import datetime
import json
import os
import re
import sys
import time
import unicodedata

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'trouble.json')
BASE = 'https://www.jra.go.jp'
UA = {'User-Agent': 'Mozilla/5.0 (shake-your-heart trouble list)'}
KEEP_DAYS = 400
VENUES = ['札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉']


def get(url):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content.decode('shift_jis', errors='ignore')
        except Exception as e:
            print(f'  読めず（{i + 1}回目）{url}: {e}', flush=True)
            time.sleep(3)
    return None


def norm_name(s):
    return re.sub(r'\s', '', unicodedata.normalize('NFKC', s or ''))


def page_lines(html):
    t = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S)
    t = re.sub(r'<br\s*/?>', '\n', t)
    t = re.sub(r'<[^>]+>', '\n', t)
    t = t.replace('&nbsp;', ' ').replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&')
    return [x.strip() for x in t.split('\n') if x.strip()]


def parse_page(html, year):
    """1ページ → [{'name','d','venue','R','type','text'}]"""
    out = []
    mode = None
    venue = date = None
    cat = None
    R = None
    pending = None        # 競走中疾病：馬の行の次の行が病名
    for ln in page_lines(html):
        if ln.startswith('今日の出来事'):
            mode = 'today'; cat = None; R = None
            continue
        if ln.startswith('明日の取消') or ln.startswith('表示モード'):
            mode = None
            continue
        if mode != 'today':
            continue
        m = re.match(r'第\d+回(\S+?)第\d+日（(\d+)月(\d+)日', ln)
        if m:
            venue = next((v for v in VENUES if v in m.group(1)), m.group(1))
            date = f'{year}.{int(m.group(2)):02d}.{int(m.group(3)):02d}'
            cat = None; R = None
            continue
        if ln in ('競走除外', '競走中止', '競走中疾病', '制裁', '出走取消', '騎手変更', '発走時刻の変更', 'その他'):
            cat = ln; R = None; pending = None
            continue
        m = re.match(r'^(\d{1,2})R$', ln)
        if m:
            R = int(m.group(1)); pending = None
            continue
        if not (venue and date and R):
            continue
        if cat == '制裁' and 'ため' in ln:
            victims_part = ln.split('ため', 1)[1]
            victims_part = re.split(r'この件', victims_part)[0]
            if '進路' in victims_part:
                typ = '進路が狭くなる不利'
            elif '接触' in victims_part:
                typ = '他馬と接触する不利'
            elif '落馬' in victims_part:
                typ = '他馬の影響で落馬'
            elif re.search(r'つまず|躓', victims_part):
                typ = '他馬の影響でつまずく不利'
            else:
                typ = '不利'
            for num, nm in re.findall(r'(\d+)番\s*([^（(、，,\s]+)\s*[（(]', victims_part):
                out.append({'name': norm_name(nm), 'n': int(num), 'd': date, 'venue': venue, 'R': R,
                            'type': typ, 'text': ln[:160]})
        elif cat == '競走中疾病':
            m = re.match(r'^(\d+)番\s*([^（(]+?)\s*[（(]', ln)
            if m:
                pending = {'name': norm_name(m.group(2)), 'n': int(m.group(1)), 'd': date, 'venue': venue, 'R': R}
                continue
            if pending and ('発症' in ln or '疾病' in ln):
                ill = re.sub(r'^競走中に|を発症.*$', '', ln)
                out.append({**pending, 'type': f'競走中に{ill}', 'text': ln[:120]})
                pending = None
    return out


def month_list(n):
    d = datetime.date.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime('%Y%m'))
        d = (d - datetime.timedelta(days=1)).replace(day=1)
    return out


def main():
    data = {'pages': [], 'horses': {}}
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            data = json.load(f)
    first = not data['pages']
    months = month_list(13 if first else 2)
    print(f"{'初回：過去12か月分' if first else '直近2か月分'}の「今日の出来事」を読みます", flush=True)
    seen = set(data['pages'])
    added = 0
    for ym in months:
        html = get(f'{BASE}/news/{ym}/')
        if not html:
            continue
        links = []
        for m in re.finditer(r'href="(/news/\d{6}/\d+\.html)"[^>]*>(?:(?!</a>).){0,300}?今日の出来事', html, re.S):
            if m.group(1) not in links:
                links.append(m.group(1))
        for url in links:
            if url in seen:
                continue
            page = get(BASE + url)
            time.sleep(1.0)
            if not page:
                continue
            items = parse_page(page, ym[:4])
            for it in items:
                lst = data['horses'].setdefault(it['name'], [])
                if not any(x['d'] == it['d'] and x['R'] == it['R'] and x['type'] == it['type'] for x in lst):
                    lst.append({k: v for k, v in it.items() if k != 'name'})
                    added += 1
            seen.add(url)
            data['pages'].append(url)
            print(f'  {url}：{len(items)}件', flush=True)
    # 古いものを捨てる
    lim = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).strftime('%Y.%m.%d')
    for nm in list(data['horses']):
        data['horses'][nm] = [x for x in data['horses'][nm] if x['d'] >= lim]
        if not data['horses'][nm]:
            del data['horses'][nm]
    data['updated'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    print(f'完了：新しく{added}件・合計{sum(len(v) for v in data["horses"].values())}件（{len(data["horses"])}頭）', flush=True)


if __name__ == '__main__':
    sys.exit(main())
