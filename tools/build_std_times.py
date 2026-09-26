# -*- coding: utf-8 -*-
"""
シェイクユアハート：基準タイム（ものさし）を作るプログラム
GitHub Actions で定期的に動かし、次の2つのファイルを更新する。
  data/races.jsonl.gz  … 集めたレース結果（1レース1行・勝ちタイムなど最低限だけ）
  data/std_times.json  … Botが読む基準タイムの表（競馬場×芝ダ×距離×馬場×クラス）と、日ごとの馬場の速さ
1回の実行で集めるレース数には上限があり、残りは次回の実行で続きから集める。
"""
import datetime
import gzip
import json
import os
import re
import statistics
import sys
import time

import requests
from bs4 import BeautifulSoup

START = datetime.date(2024, 1, 1)
WAIT = 1.2                 # netkeibaに負担をかけないよう、1回ごとに少し待つ
MAX_RACES_PER_RUN = 2500   # 1回の実行で集める上限（約1時間）
MAX_MINUTES = 300          # GitHub Actionsの制限(6時間)より前に切り上げる

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
RACES = os.path.join(DATA, 'races.jsonl.gz')
PROGRESS = os.path.join(DATA, 'progress.json')
OUT = os.path.join(DATA, 'std_times.json')

HEADERS = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
           'Accept-Language': 'ja,en;q=0.8'}
SESSION = requests.Session()
JRA_PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
             "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
T0 = time.time()


def log(*a):
    print(*a, flush=True)


def get(url):
    time.sleep(WAIT)
    r = SESSION.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r


def soup_of(r):
    raw = r.content
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    enc = m.group(1).decode('ascii').lower() if m else 'euc-jp'
    if 'utf' in enc:
        html = raw.decode('utf-8', errors='replace')
    else:
        try:
            html = raw.decode('euc_jp')
        except UnicodeDecodeError:
            html = raw.decode('euc_jis_2004', errors='replace')
    return BeautifulSoup(html, 'html.parser')


def norm(t):
    return str(t or '').translate(str.maketrans('０１２３４５６７８９', '0123456789'))


def class_group(text):
    """0=新馬・未勝利 1=1勝 2=2勝 3=3勝 4=OP・L 5=重賞"""
    t = norm(text)
    if re.search(r'G1|G2|G3|GI|GⅠ|GⅡ|GⅢ|重賞', t): return 5
    if '3勝' in t: return 3
    if '2勝' in t: return 2
    if '1勝' in t: return 1
    if re.search(r'新馬|未勝利', t): return 0
    if re.search(r'オープン|OP|リステッド|\(L\)', t): return 4
    return 2


def to_sec(t):
    m = re.match(r'(\d+):(\d{2}\.\d)', t or '')
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.match(r'(\d{2}\.\d)$', t or '')
    return float(m.group(1)) if m else None


# ── 1. 日付ごとのレースID ──
def race_ids_on(date):
    r = get(f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date:%Y%m%d}")
    return sorted(set(i for i in re.findall(r'race_id=(\d{12})', r.text) if i[4:6] in JRA_PLACE))


# ── 2. 1レース分の結果 ──
def parse_race(race_id, date):
    s = soup_of(get(f"https://race.netkeiba.com/race/result.html?race_id={race_id}"))
    d1 = s.select_one('.RaceData01')
    d1t = d1.get_text(' ', strip=True) if d1 else ''
    dm = re.search(r'(芝|ダ|障)\s*(\d{3,4})m', d1t)
    gm = re.search(r'馬場\s*[:：]\s*(良|稍重?|重|不良?)', d1t)
    if not dm or dm.group(1) == '障':
        return None
    name_el = s.select_one('.RaceName')
    name = name_el.get_text(' ', strip=True) if name_el else ''
    if name_el:
        for sp in name_el.select('[class*="Icon_GradeType"]'):
            g = re.search(r'Icon_GradeType(\d+)', ' '.join(sp.get('class', [])))
            if g and g.group(1) in ('1', '2', '3'):
                name += ' G' + g.group(1)
    d2 = s.select_one('.RaceData02')
    cond_text = d2.get_text(' ', strip=True) if d2 else ''
    table = s.select_one('table#All_Result_Table') or s.select_one('table.RaceTable01')
    if not table:
        return None
    header = table.select_one('tr.Header') or table.find('tr')
    cols = [re.sub(r'\s+', '', c.get_text()) for c in header.find_all(['th', 'td'])]
    i_t = next((i for i, c in enumerate(cols) if c.startswith('タイム')), None)
    i_l3 = next((i for i, c in enumerate(cols) if '後3F' in c or '上り' in c), None)
    rows = [tr.find_all('td') for tr in (table.select('tr.HorseList') or table.find_all('tr')[1:])]
    finishers = [tds for tds in rows if tds and re.sub(r'\s+', '', tds[0].get_text()).isdigit()]
    win = next((tds for tds in finishers if re.sub(r'\s+', '', tds[0].get_text()) == '1'), None)
    if not win or i_t is None:
        return None
    t = to_sec(re.sub(r'\s+', '', win[i_t].get_text()))
    if not t:
        return None
    l3 = None
    if i_l3 is not None and i_l3 < len(win):
        m = re.search(r'\d{2}\.\d', win[i_l3].get_text())
        l3 = float(m.group()) if m else None
    g = gm.group(1) if gm else '良'
    pm = re.search(r'ペース\s*[:：]?\s*([HMS])(?![a-zA-Z])', s.get_text(' '))
    return {'id': race_id, 'd': f"{date:%Y.%m.%d}", 'p': JRA_PLACE[race_id[4:6]], 'surf': dm.group(1),
            'dist': int(dm.group(2)), 'cond': g[:1], 'cls': class_group(name + ' ' + cond_text),
            'n': len(finishers), 't': t, 'l3': l3, 'pace': pm.group(1) if pm else None}


# ── 3. 集計（基準タイム・クラス差・馬場差・日ごとの馬場の速さ） ──
def build_table(races):
    def med(v):
        return statistics.median(v) if v else None

    km = lambda r: r['dist'] / 1000
    base, exact = {}, {}
    for r in races:
        base.setdefault(f"{r['p']}|{r['surf']}|{r['dist']}|{r['cond']}", []).append(r['t'])
        exact.setdefault(f"{r['p']}|{r['surf']}|{r['dist']}|{r['cond']}|{r['cls']}", []).append(r['t'])
    base_m = {k: [round(med(v), 2), len(v)] for k, v in base.items() if len(v) >= 3}
    exact_m = {k: [round(med(v), 2), len(v)] for k, v in exact.items() if len(v) >= 3}

    # クラスごとの速さの差（1000mあたりの秒。－は速い）
    cls_res = {}
    for r in races:
        b = base_m.get(f"{r['p']}|{r['surf']}|{r['dist']}|{r['cond']}")
        if b and b[1] >= 5:
            cls_res.setdefault(r['surf'], {}).setdefault(str(r['cls']), []).append((r['t'] - b[0]) / km(r))
    cls_off = {s: {c: round(statistics.mean(v), 3) for c, v in d.items() if len(v) >= 10} for s, d in cls_res.items()}

    # 馬場状態ごとの差（良との比較、1000mあたりの秒）
    cond_res = {}
    for r in races:
        if r['cond'] == '良':
            continue
        b = base_m.get(f"{r['p']}|{r['surf']}|{r['dist']}|良")
        if b and b[1] >= 5:
            cond_res.setdefault(r['surf'], {}).setdefault(r['cond'], []).append((r['t'] - b[0]) / km(r))
    cond_off = {s: {c: round(statistics.mean(v), 3) for c, v in d.items() if len(v) >= 10} for s, d in cond_res.items()}

    def expected(r):
        e = exact_m.get(f"{r['p']}|{r['surf']}|{r['dist']}|{r['cond']}|{r['cls']}")
        if e and e[1] >= 5:
            return e[0]
        b = base_m.get(f"{r['p']}|{r['surf']}|{r['dist']}|{r['cond']}")
        if b and b[1] >= 5:
            return b[0] + cls_off.get(r['surf'], {}).get(str(r['cls']), 0) * km(r)
        return None

    # 日ごとの馬場の速さ（その日・その競馬場・芝ダの全レースの平均的なズレ。＋は時計がかかる日）
    day = {}
    for r in races:
        e = expected(r)
        if e:
            day.setdefault(f"{r['d']}|{r['p']}|{r['surf']}", []).append((r['t'] - e) / km(r))
    variant = {k: [round(statistics.mean(v) * len(v) / (len(v) + 2), 3), len(v)] for k, v in day.items() if len(v) >= 2}

    return {'updated': datetime.date.today().isoformat(), 'n_races': len(races),
            'first': min((r['d'] for r in races), default=None), 'last': max((r['d'] for r in races), default=None),
            'exact': exact_m, 'base': base_m, 'cls_off': cls_off, 'cond_off': cond_off, 'variant': variant}


def load_races():
    if not os.path.exists(RACES):
        return {}
    with gzip.open(RACES, 'rt', encoding='utf-8') as f:
        return {r['id']: r for r in (json.loads(x) for x in f if x.strip())}


def save_races(races):
    os.makedirs(DATA, exist_ok=True)
    with gzip.open(RACES, 'wt', encoding='utf-8') as f:
        for r in sorted(races.values(), key=lambda r: r['id']):
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def main():
    os.makedirs(DATA, exist_ok=True)
    races = load_races()
    prog = json.load(open(PROGRESS)) if os.path.exists(PROGRESS) else {}
    done_dates = set(prog.get('dates_done', []))
    pending = [p for p in prog.get('pending', []) if p[0] not in races]   # [レースID, 'YYYYMMDD']
    queued = {p[0] for p in pending}
    today = datetime.date.today()
    recheck_from = today - datetime.timedelta(days=7)   # 最近1週間は毎回見直す
    added = 0
    time_left = lambda: (time.time() - T0) / 60 < MAX_MINUTES

    def collect():
        nonlocal added
        while pending and added < MAX_RACES_PER_RUN and time_left():
            rid, ds = pending[0]
            try:
                r = parse_race(rid, datetime.date(int(ds[:4]), int(ds[4:6]), int(ds[6:])))
                if r:
                    races[rid] = r
                    added += 1
                    if added % 100 == 0:
                        log(f'{added}レース追加（合計{len(races)}）')
                pending.pop(0)
            except Exception as e:
                log('結果の取得に失敗', rid, e)
                pending.append(pending.pop(0))
                return

    collect()   # 前回の残りから
    d = START
    while d < today and added < MAX_RACES_PER_RUN and time_left():
        key = f"{d:%Y%m%d}"
        if key not in done_dates or d >= recheck_from:
            try:
                for rid in race_ids_on(d):
                    if rid not in races and rid not in queued:
                        pending.append([rid, key])
                        queued.add(rid)
                if d < recheck_from:
                    done_dates.add(key)
            except Exception as e:
                log('一覧の取得に失敗', key, e)
            collect()
        d += datetime.timedelta(days=1)

    save_races(races)
    json.dump({'dates_done': sorted(done_dates), 'pending': pending}, open(PROGRESS, 'w'))
    table = build_table(list(races.values()))
    json.dump(table, open(OUT, 'w'), ensure_ascii=False, separators=(',', ':'))
    log(f'完了：今回{added}レース追加、合計{len(races)}レース、残り{len(pending)}レース、'
        f'基準タイム{len(table["exact"])}件（{table["first"]}〜{table["last"]}）')


if __name__ == '__main__':
    sys.exit(main())
