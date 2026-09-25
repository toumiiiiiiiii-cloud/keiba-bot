# -*- coding: utf-8 -*-
"""
シェイクユアハート LINE Bot
netkeibaのURLを送ると、出馬表（5走表示）を読み込み、
シェイクユアハートと同じ計算方式でスコア・勝率・予想印・買い目を返す。
"""
import os
import re
import sys
import math
import requests
import json
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import glob
import time
import uuid
from flask import send_from_directory
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET or 'dummy')

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept-Language': 'ja,en;q=0.8',
}
HORSE_HREF = re.compile(r'/horse/\d{10}')

# ═════════════════════════════════════════
# シェイクユアハートの設定値（アプリと同じ）
# ═════════════════════════════════════════
SETTINGS = {
    'T': 14,                                   # 実力差の見立て（softmax温度）
    'mudw': 1,                                 # 道悪重視の強さ
    'jb_names': ['ルメール', '岩田望来', '松山弘平'],  # 騎手バイアス対象
    'jb_strength': 0.03,
    'wl': 0.40,                                # 過去レースレベルの重視度
}
CLS = [("未勝利・新馬", 1), ("1勝クラス", 2), ("2勝クラス", 3), ("3勝クラス", 4),
       ("OP・リステッド", 4.5), ("G3", 5), ("G2", 6), ("G1", 7), ("地方・その他", 1)]
PACE = {'slow': {'逃': .03, '先': .015, '差': -.015, '追': -.03},
        'base': {},
        'fast': {'逃': -.03, '先': -.015, '差': .015, '追': .03}}
PACE_LABEL = {'slow': 'スロー', 'base': '平均', 'fast': 'ハイ'}
MUD_K = {'良': 0, '稍重': 0.012, '重': 0.035, '不良': 0.05}
CLASS_SHRINK = {'良': 1, '稍重': 0.95, '重': 0.85, '不良': 0.75}
SIRE_MUD = {"キズナ": 5, "スクリーンヒーロー": 5, "キタサンブラック": 5, "リアルスティール": 5,
            "ファインニードル": 5, "セイウンコウセイ": 5, "オルフェーヴル": 4, "ゴールドシップ": 3.5}
EURO_SIRES = ["Galileo", "Frankel", "Dubawi", "New Approach", "Kingman", "Sea The Stars", "Camelot",
              "Nathaniel", "Wootton Bassett", "Invincible Spirit", "Teofilo", "Golden Horn", "Australia",
              "Zoffany", "Le Havre", "Siyouni", "Dark Angel", "Motivator", "Harzand", "Galileo Gold"]
JP_SIRE_EURO_PARENT = {"シルバーステート": "Frankel", "タワーオブロンドン": "Dubawi", "マクフィ": "Dubawi"}
SIRE_CLASS = {"ディープインパクト": 5, "キズナ": 4.5, "ドゥラメンテ": 4.5, "ロードカナロア": 4.5,
              "キタサンブラック": 4.5, "エピファネイア": 4.5, "モーリス": 4, "リアルスティール": 4,
              "サトノダイヤモンド": 4, "スワーヴリチャード": 4, "コントレイル": 4.5, "リオンディーズ": 3.5,
              "ドレフォン": 3.5, "サトノクラウン": 3.5, "ハーツクライ": 4, "ハービンジャー": 4,
              "オルフェーヴル": 4, "ゴールドシップ": 3.5, "シルバーステート": 3.5, "イスラボニータ": 3,
              "サトノアラジン": 3, "アメリカンペイトリオット": 3, "タワーオブロンドン": 3}
LBL = {'lv': ["低い", "やや低い", "標準", "やや高い", "高い"],
       'form': ["不調", "やや不調", "普通", "やや好調", "好調"],
       'fit': ["合わない", "やや不安", "普通", "やや合う", "合う"],
       'jk': ["不利", "やや不利", "普通", "やや有利", "有利"]}
MARKS = ["◎", "○", "▲", "△", "△", "△", "☆"]
JRA_PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
             "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


# ═════════════════════════════════════════
# 共通ユーティリティ
# ═════════════════════════════════════════
def clean(t):
    return re.sub(r'\s+', '', t or '')


def to_float(t):
    if t is None:
        return None
    m = re.search(r'\d+(?:\.\d+)?', str(t))
    return float(m.group()) if m else None


def norm_digits(s):
    return str(s or '').translate(str.maketrans('０１２３４５６７８９', '0123456789'))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


SESSION = requests.Session()


def fetch_soup(url):
    res = SESSION.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    raw = res.content
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


def class_of_text(t, place=''):
    """レース名からクラス（CLSの番号）を判定。アプリの classOfText と同じ順序"""
    t = norm_digits(t)
    if re.search(r'Jpn1|JpnⅠ|ＪpnI', t): return 7
    if re.search(r'Jpn2|JpnⅡ', t): return 6
    if re.search(r'Jpn3|JpnⅢ', t): return 5
    if re.search(r'GIII|G3|GⅢ', t): return 5
    if re.search(r'GII|G2|GⅡ', t): return 6
    if re.search(r'GI|G1|GⅠ', t): return 7
    if '重賞' in t: return 4
    if re.search(r'(笠松|金沢|川崎|大井|船橋|浦和|園田|姫路|名古屋|盛岡|水沢|門別|帯広|高知|佐賀)', place or ''): return 8
    if '3勝' in t: return 3
    if '2勝' in t: return 2
    if '1勝' in t: return 1
    if re.search(r'新馬|未勝利', t): return 0
    if re.search(r'オープン|OP|リステッド|\bL\b', t): return 4
    return 2


def dist_num(dist):
    m = re.search(r'\d+', dist or '')
    return int(m.group()) if m else 0


def is_flat(l):
    return (l.get('dist') or '')[:1] in ('芝', 'ダ')


# ═════════════════════════════════════════
# netkeiba 出馬表（5走表示）の読み取り
# ═════════════════════════════════════════
def parse_input(text):
    m = re.search(r'race_id=(\d{12})', text) or re.search(r'/race/(\d{12})', text) or re.search(r'\b(\d{12})\b', text)
    if not m:
        return None, None
    base = 'https://nar.netkeiba.com' if 'nar.' in text else 'https://race.netkeiba.com'
    return m.group(1), base


def parse_past_cell(td):
    """過去1走分のセルを辞書にする。読めなければ None"""
    txt = re.sub(r'\s+', ' ', td.get_text(' ', strip=True))
    m = re.search(r'(\d{4})\.(\d{2})\.(\d{2})\s*(\S+?)\s+(\d+|除|中|取)(?:\s|$)', txt)
    if not m:
        return None
    pos = int(m.group(5)) if m.group(5).isdigit() else 0

    name_el = td.select_one('.Data02 a') or td.select_one('.Data02')
    if name_el:
        rname = name_el.get_text(' ', strip=True)
        # 途中で切れたカッコを閉じる（例：「天皇賞(春 GI」→「天皇賞(春) GI」）
        if rname.count('(') > rname.count(')'):
            rname = re.sub(r'\(([^()\s]*)(\s|$)', r'(\1)\2', rname, count=1)
    else:
        a = next((x for x in td.find_all('a') if not HORSE_HREF.search(x.get('href', ''))), None)
        rname = a.get_text(' ', strip=True) if a else ''

    dm = re.search(r'(芝|ダ|障)\s*(外|内)?\s*(\d{3,4})\s*(\((?:外|内)\))?', txt)
    if not dm:
        return None
    side = dm.group(2) or (dm.group(4) or '').strip('()')
    dist = dm.group(1) + dm.group(3) + (f'({side})' if side else '')
    after = txt[dm.end():]
    cm = re.search(r'(良|稍|重|不)', after)
    cond = cm.group(1) if cm else '良'
    tm = re.search(r'(\d):(\d{2}\.\d)', after)
    t_sec = int(tm.group(1)) * 60 + float(tm.group(2)) if tm else 0

    fm = re.search(r'(\d+)頭\s*(\d+)番\s*(\d*)人?\s*(\S+)\s+(\d{2}(?:\.\d)?)', txt)
    pm = re.search(r'([\d]+(?:-[\d]+)+|\d+)\s*\(([\d.]+)\)\s*(\d{3})\s*\(([+\-]?\d+)\)', txt)
    mm = re.findall(r'\(([+\-]?\d+\.\d)\)', txt)
    if not mm:
        return None
    win_a = [x for x in td.find_all('a') if HORSE_HREF.search(x.get('href', ''))]
    return {
        'd': f'{m.group(1)}.{m.group(2)}.{m.group(3)}', 'p': m.group(4), 'pos': pos, 'r': rname,
        'dist': dist, 'cond': cond, 't': t_sec,
        'f': int(fm.group(1)) if fm else 16, 'n': int(fm.group(2)) if fm else 0,
        'pop': int(fm.group(3)) if fm and fm.group(3) else 0,
        'j': fm.group(4) if fm else '', 'w': float(fm.group(5)) if fm else 0,
        'ps': pm.group(1) if pm else '', 'l3': float(pm.group(2)) if pm else 0,
        'bw': f'{pm.group(3)}({pm.group(4)})' if pm else '',
        'win': clean(win_a[-1].get_text()) if win_a else '',
        'm': float(mm[-1]),
    }


def parse_race_info(soup, race_id, base):
    r = {}
    name_el = soup.select_one('.RaceName')
    name = name_el.get_text(' ', strip=True) if name_el else ''
    # 重賞グレードはアイコン画像なので、クラス名から文字に直す
    if name_el:
        for sp in name_el.select('[class*="Icon_GradeType"]'):
            g = re.search(r'Icon_GradeType(\d+)', ' '.join(sp.get('class', [])))
            if g:
                name += {'1': ' G1', '2': ' G2', '3': ' G3', '15': ' L'}.get(g.group(1), '')
    r['name'] = name.strip() or '対象レース'

    d1 = soup.select_one('.RaceData01')
    d1t = d1.get_text(' ', strip=True) if d1 else ''
    dm = re.search(r'(芝|ダ|障)\s*(\d{3,4})m', d1t)
    r['surf'] = dm.group(1) if dm else '芝'
    r['dist'] = int(dm.group(2)) if dm else 0
    tm = re.search(r'(\d{1,2}:\d{2})発走', d1t)
    r['time'] = tm.group(1) if tm else ''
    gm = re.search(r'馬場\s*[:：]\s*(良|稍重?|重|不良?)', d1t)
    g = gm.group(1) if gm else '良'
    r['going'] = {'稍': '稍重', '不': '不良'}.get(g, g)
    r['going_known'] = bool(gm)

    d2 = soup.select_one('.RaceData02')
    r['condText'] = norm_digits(d2.get_text(' ', strip=True)) if d2 else ''
    r['clsIdx'] = class_of_text(r['name'] + ' ' + r['condText'], '')

    if 'nar.' in base:
        r['venue'] = (re.search(r'\d+回\s*(\S+?)\s*\d+日目', r['condText']) or [None, ''])[1] or ''
    else:
        r['venue'] = JRA_PLACE.get(race_id[4:6], '')
    r['R'] = int(race_id[-2:])

    pm = re.search(r'ペース\s*([HMS])', soup.get_text(' '))
    r['pace'] = {'H': 'fast', 'M': 'base', 'S': 'slow'}[pm.group(1)] if pm else None
    return r


def parse_shutuba_past(soup):
    horses = []
    rows = soup.select('table.Shutuba_Past5_Table tr.HorseList') or soup.select('tr.HorseList')
    for tr in rows:
        info = tr.select_one('td.Horse_Info')
        if info is None:
            info = next((td for td in tr.find_all('td')
                         if td.find('a', href=HORSE_HREF) and not re.search(r'\d{4}\.\d{2}\.\d{2}', td.get_text())), None)
        if info is None:
            continue
        a = info.select_one('.Horse02 a') or info.find('a', href=HORSE_HREF)
        if not a:
            continue
        name = clean(a.get_text())
        hm_ = re.search(r'/horse/(\d{10})', a.get('href', ''))
        hid = hm_.group(1) if hm_ else None

        tds = tr.find_all('td')
        nums = [int(t) for t in (clean(td.get_text()) for td in tds[:3]) if t.isdigit()]
        waku = nums[0] if len(nums) >= 2 else None
        umaban = nums[1] if len(nums) >= 2 else (nums[0] if nums else None)

        def txt(sel):
            el = info.select_one(sel)
            return el.get_text(' ', strip=True) if el else ''
        sire = txt('.Horse01')
        dam = txt('.Horse03')
        damsire = txt('.Horse04').strip('()（） ')
        info_text = info.get_text(' ', strip=True)
        stm = re.search(r'([逃先差追大])\s*(?:中\s*\d+\s*週|連闘)', info_text)
        st = stm.group(1) if stm else '?'
        if st == '大':
            st = '逃'

        # 前走からの間隔（週）：「中3週」「連闘」
        wk = re.search(r'中\s*(\d+)\s*週', info_text)
        weeks = 0 if '連闘' in info_text else (int(wk.group(1)) if wk else None)

        jk_td = tr.select_one('td.Jockey')
        jockey, wt, jid = '', 0.0, None
        if jk_td:
            ja = jk_td.find('a', href=re.compile(r'/jockey/'))
            jockey = clean(ja.get_text()) if ja else ''
            jm = re.search(r'/jockey/(?:result/)?(?:recent/)?(\d{5})', ja.get('href', '')) if ja else None
            jid = jm.group(1) if jm else None
            wm = re.search(r'(\d{2}\.\d)', jk_td.get_text(' '))
            wt = float(wm.group(1)) if wm else 0.0
        if not jockey:
            ja = next((x for x in tr.find_all('a', href=re.compile(r'/jockey/'))
                       if not x.find_parent('td', class_='Past')), None)
            jockey = clean(ja.get_text()) if ja else '不明'
        jockey = re.sub(r'^[▲△☆★◇]+', '', jockey) or '不明'

        past_tds = tr.select('td.Past') or [td for td in tds if re.search(r'\d{4}\.\d{2}\.\d{2}', td.get_text())]
        lines = [x for x in (parse_past_cell(td) for td in past_tds) if x]

        # 当日の馬体重（発表後のみ）。過去走の欄は除いて探す
        bw_now = bw_diff = None
        for td in tds:
            if td in past_tds:
                continue
            bm = re.search(r'(\d{3})\s*\(\s*([+\-]?\d+)\s*\)', td.get_text(' '))
            if bm:
                bw_now, bw_diff = int(bm.group(1)), int(bm.group(2))
                break

        cancelled = 'Cancel' in tr.get('class', []) or bool(re.search(r'取消|除外', tr.get_text()[:200]))
        horses.append({'w': waku, 'n': umaban, 'name': name, 'sire': clean(sire),
                       'dam': f'{clean(dam)}({damsire})' if damsire else clean(dam),
                       'damsire': damsire, 'st': st, 'jockey': jockey, 'wt': wt, 'jid': jid, 'hid': hid,
                       'weeks': weeks, 'bwNow': bw_now, 'bwDiff': bw_diff,
                       'lines': lines, 'cancelled': cancelled})
    return horses


def fetch_win_odds(race_id, base):
    """単勝オッズ（JRAのみ）。発売前などで取れなければ空"""
    if 'nar.' in base:
        return {}
    url = ('https://race.netkeiba.com/api/api_get_jra_odds.html'
           f'?race_id={race_id}&type=1&action=update')
    try:
        js = requests.get(url, headers=HEADERS, timeout=10).json()
        data = js.get('data')
        if not isinstance(data, dict):
            return {}
        win = (data.get('odds') or {}).get('1') or {}
        out = {}
        for k, v in win.items():
            val = to_float(v[0]) if v else None
            if val:
                out[int(k)] = val
        return out
    except Exception:
        return {}


# ═════════════════════════════════════════
# シェイクユアハートの計算（アプリのJSを移植）
# ═════════════════════════════════════════
def calc_level(past, rc):
    if not past:
        return None
    wts = [1] if len(past) == 1 else [.6, .4] if len(past) == 2 else [.5, .3, .2]
    L = 0
    for i, (ci, d) in enumerate(past):
        adj = clamp((0.6 - d) * 0.8, -1.5, 1.5)
        L += wts[i] * (CLS[ci][1] + adj)
    gap = L - rc
    rating = 5 if gap >= 1 else 4 if gap >= 0.4 else 3 if gap >= -0.4 else 2 if gap >= -1 else 1
    return {'L': L, 'gap': gap, 'rating': rating}


def auto_heuristics(race, horses):
    """過去走から 過去レースレベル・近走・適性・騎手条件 の1〜5評価を作る"""
    rcv = CLS[race['clsIdx']][1]
    wts = sorted(h['wt'] for h in horses if h['wt'] > 0)
    med = wts[len(wts) // 2] if wts else 55
    for h in horses:
        ls = [l for l in h['lines'] if l['pos'] > 0 and is_flat(l)]
        past = [(class_of_text(l['r'], l['p']), l['m']) for l in ls[:3]]
        cl = calc_level(past, rcv)
        is_new = not ls
        lv = cl['rating'] if cl else (SIRE_CLASS.get(h['sire'], 3) if is_new else 3)

        def mv(l):
            if l['pos'] == 1:
                return 0
            lcv = CLS[class_of_text(l['r'], l['p'])][1]
            relief = min(1, rcv / max(1, lcv))   # 格上レースの着差は軽く見る（クラス補填）
            return max(0, l['m']) * relief

        form = 3
        if ls:
            wl = [.5, .3, .2]
            use = ls[:3]
            tw = sum(wl[:len(use)])
            avg = sum(wl[i] * mv(l) for i, l in enumerate(use)) / tw
            form = 5 if avg <= 0.3 else 4 if avg <= 0.7 else 3 if avg <= 1.2 else 2 if avg <= 2.0 else 1

        # 適性は全成績で見る（古いレースほど軽く）
        cs = [l for l in h.get('career', h['lines']) if l['pos'] > 0 and is_flat(l)]
        same = [l for l in cs if l['dist'][:1] == race['surf'] and abs(dist_num(l['dist']) - race['dist']) <= 100]
        fit = 2
        if same:
            ws = [age_w(l, race) for l in same]
            t3w = sum(w_ for w_, l in zip(ws, same) if l['pos'] <= 3)
            avg = sum(w_ * mv(l) for w_, l in zip(ws, same)) / sum(ws)
            fit = (5 if t3w >= 1.5 else 4) if (t3w >= 0.6 and avg <= 1.0) else 3 if avg <= 1.5 else 2
        elif cs and all(l['dist'][:1] != race['surf'] for l in cs):
            fit = 1

        dw = med - h['wt']
        jk = clamp(3 + (1 if dw >= 2 else 0) + (1 if dw >= 4 else 0) - (1 if dw <= -2 else 0), 1, 5)

        t3 = sum(1 for l in same if l['pos'] <= 3)
        why = []
        if is_new:
            why.append(f"新馬。父{h['sire'] or '不明'}の産駒レベルで評価")
        elif ls:
            l0 = ls[0]
            res = (f"{abs(l0['m']):.1f}秒差で勝ち" if l0['pos'] == 1 else f"{l0['m']:.1f}秒差")
            why.append(f"前走{l0['r']}{l0['pos']}着（{res}）")
            why.append(f"同条件は全{len(same)}走で3着内{t3}回" if same else "同条件の経験なし")

        wl_ = SETTINGS['wl']
        rest = 1 - wl_
        w = {'lv': wl_, 'form': .40 * rest, 'fit': .33 * rest, 'jk': .27 * rest}
        c = lambda v: (v - 3) / 2
        h.update({'cl': cl, 'pastCls': [CLS[ci][0] for ci, _ in past], 'med': med, 'dw': dw,
                  'recent': ls[:3], 'sameN': len(same), 'sameT3': t3,
                  'lv': lv, 'form': form, 'fit': fit, 'jk': jk, 'isNew': is_new, 'why': why,
                  'rec': f"{t3}-{len(same) - t3}" if same else "初",
                  's': round(1 + 0.09 * (w['lv'] * c(lv) + w['form'] * c(form) + w['fit'] * c(fit) + w['jk'] * c(jk)), 3)})
    return horses


def jb_bonus(name):
    n = re.sub(r'[▲△☆◇★\s]', '', name or '')
    n = re.sub(r'^[A-Za-zＡ-Ｚ]\.', '', n)
    if len(n) < 2:
        return 0
    return SETTINGS['jb_strength'] if any(e in n or n in e for e in SETTINGS['jb_names']) else 0


def mud_parts(h):
    m, known, parts = 3, False, []
    sire = h['sire']
    if sire in SIRE_MUD:
        m = SIRE_MUD[sire]; known = True; parts.append(f"父{sire}は道悪巧者")
    bw_line = next((l for l in h['lines'] if re.match(r'^\d+', l.get('bw') or '')), None)
    if bw_line:
        bw = int(re.match(r'^\d+', bw_line['bw']).group())
        d = 0.5 if bw >= 500 else 0.25 if bw >= 480 else -0.5 if bw < 440 else 0
        m += d; known = True
    wet = [l for l in h.get('career', h['lines']) if l['pos'] > 0 and l['dist'].startswith('芝') and re.search(r'[稍重不]', l['cond'])]
    if wet:
        g = sum(1 for l in wet if l['pos'] <= 3 or l['m'] <= 0.5)
        b = sum(1 for l in wet if l['pos'] > 3 and l['m'] > 1.0)
        m += clamp(0.5 * g - 0.75 * b, -1.5, 1); known = True
        parts.append(f"道悪{len(wet)}走で好走{g}回")
    if sire in EURO_SIRES or sire in JP_SIRE_EURO_PARENT:
        m += 0.5; known = True; parts.append("父が欧州系")
    if h.get('damsire') in EURO_SIRES:
        m += 0.5; known = True; parts.append("母父が欧州系")
    return (clamp(m, 1, 6) if known else 0), parts


def dist_record(h, race):
    tol = 200 if race['dist'] >= 2000 else 100
    wts = [1, .9, .8, .7, .6, .5, .45, .4]
    rows = [l for l in h.get('career', h['lines']) if l['pos'] > 0 and is_flat(l) and l['dist'][:1] == race['surf']
            and abs(dist_num(l['dist']) - race['dist']) <= tol][:8]
    s = 0
    for i, l in enumerate(rows):
        near = 1 if abs(dist_num(l['dist']) - race['dist']) <= tol / 2 else 0.7
        q = 1 if l['pos'] == 1 else (0.8 if l['m'] <= 0.2 else 0.6 if l['m'] <= 0.4 else 0.4 if l['m'] <= 0.6 else 0.15 if l['m'] <= 1.0 else 0)
        s += q * (wts[i] if i < len(wts) else 0.5) * near
    return s, rows


def makuri_score(h):
    wts = [1, .8, .6, .4, .3]
    score = 0
    for i, l in enumerate([l for l in h['lines'] if l['pos'] > 0][:5]):
        p = [int(x) for x in (l.get('ps') or '').split('-') if x.isdigit() and int(x) > 0]
        if len(p) < 3 or not is_flat(l):
            continue
        gain = p[1] - min(p[2:])
        if gain < max(5, math.ceil((l['f'] or 16) * 0.3)):
            continue
        good = l['pos'] <= 3 or l['m'] <= 0.5
        score += (1 if good else 0.3) * wts[i]
    return score


def rival_level(h, keys):
    """対戦相手がその後、格上のレースで2着以内 → そのレースは見た目より強かった"""
    bonus, notes, seen = 0, [], set()
    for l in [x for x in h.get('career', h['lines']) if x['pos'] > 0][:15]:
        shared = class_of_text(l['r'], l['p'])
        for o in keys.get(line_key(l), []):
            if o['name'] == h['name'] or o['pos'] <= 0 or o['name'] in seen:
                continue
            best = None
            for f in o['horse'].get('career', o['horse']['lines']):
                if f['pos'] > 0 and f['d'] > l['d'] and f['pos'] <= 2:
                    fc = class_of_text(f['r'], f['p'])
                    if fc != 8 and fc > shared and (not best or fc > best[0]):
                        best = (fc, f)
            if not best:
                continue
            won, tie = l['pos'] < o['pos'], l['pos'] == o['pos']
            gain = min(2, best[0] - shared) * (0.35 if won else 0.2 if tie else 0.12) * 0.05
            if gain <= 0.003:
                continue
            seen.add(o['name']); bonus += gain
            notes.append(f"{l['r']}で対戦した{o['name']}がその後{best[1]['r']}で{best[1]['pos']}着")
    return min(bonus, 0.05), notes[:2]


def line_key(l):
    # 同じレースかどうかの目印（日付・競馬場・距離・頭数）。出馬表と馬のページで書き方が違っても一致するように
    return f"{l.get('d')}|{l.get('p')}|{dist_num(l.get('dist'))}|{l.get('f')}"


def ranked(race, horses, odds):
    going, pace = race['going'], race['pace']
    shr = 1 - (1 - CLASS_SHRINK.get(going, 1)) * SETTINGS['mudw']
    adj = PACE.get(pace, {})
    keys = {}
    for h in horses:
        for l in h.get('career', h['lines']):
            keys.setdefault(line_key(l), []).append({'name': h['name'], 'pos': l['pos'], 'horse': h})

    for h in horses:
        jb = jb_bonus(h['jockey'])
        m, mparts = mud_parts(h)
        mb = (m - 3) / 2 * MUD_K.get(going, 0) * SETTINGS['mudw'] if m else 0
        dsum, drows = dist_record(h, race)
        mk = makuri_score(h)
        pace_f = (1 if pace == 'slow' else 0.4 if pace == 'base' else 0) if race['dist'] >= 2000 else 0
        ana = min(dsum, 2.5) * 0.012 + min(mk, 2) / 2 * 0.03 * pace_f
        rl, rnotes = rival_level(h, keys)
        h['a'] = round(1 + (h['s'] - 1) * shr + adj.get(h['st'], 0) + jb + mb + ana + rl + h.get('xBonus', 0), 3)
        h.update({'paceAdj': adj.get(h['st'], 0), 'distSum': dsum, 'mudM': m, 'rl': rl,
                  'jb': jb > 0, 'mudb': mb, 'mudParts': mparts, 'ana': ana, 'makuri': mk,
                  'distRows': drows, 'rivalNotes': rnotes})

    arr = sorted(horses, key=lambda x: (-x['a'], x['n'] or 99))
    ex = [math.exp(SETTINGS['T'] * (h['a'] - 1)) for h in arr]
    tot = sum(ex)
    for i, h in enumerate(arr):
        a = h['a']
        h['rank'] = 'S' if a >= 1.08 - 1e-9 else 'A' if a >= 1.03 - 1e-9 else 'B' if a >= 1.0 - 1e-9 else 'C' if a >= 0.96 - 1e-9 else 'D'
        h['p'] = ex[i] / tot
        h['rk'] = i + 1
        h['o'] = odds.get(h['n'])
        h['ev'] = h['p'] * h['o'] if h['o'] else None
        h['anaScore'] = h['ana'] + max(0, h['mudb']) * (1 if going in ('重', '不良') else 0)
        h['anaFlag'] = h['rk'] >= 4 and h['anaScore'] >= 0.02
    return arr


def chaos_info(arr):
    N = len(arr)
    top = arr[0]['a']
    close = sum(1 for h in arr if h['a'] >= top - 0.045)
    density = clamp((close - 1) / max(1, min(6, N - 1)), 0, 1)
    top3p = sum(h['p'] for h in arr[:3])
    top3s = clamp((0.75 - top3p) / 0.4, 0, 1)
    gap = max(0, arr[0]['a'] - arr[1]['a']) if N >= 2 else 0.05
    gaps = clamp((0.05 - gap) / 0.05, 0, 1)
    anas = min(1, sum(1 for h in arr if h['anaFlag']) / 4)
    pct = round(clamp(density * .35 + top3s * .25 + gaps * .25 + anas * .15, 0, 1) * 100)
    label = '大荒れ注意' if pct >= 70 else '荒れ気味' if pct >= 50 else '普通' if pct >= 30 else 'やや堅い' if pct >= 15 else '堅い'
    return pct, label


def verdict(arr):
    N = len(arr)
    score = 0
    top3 = sum(h['p'] for h in arr[:3])
    ratio = top3 / (3 / N)
    score += 2 if ratio >= 2.6 else 1 if ratio >= 2.0 else 0 if ratio >= 1.6 else -1
    if arr[0]['a'] - arr[1]['a'] >= 0.03:
        score += 1
    if N >= 15:
        score -= 1
    with_odds = sum(1 for h in arr if h['o'])
    if with_odds < math.ceil(N / 2):
        return '様子見（オッズ未発表のため妙味は未判定）'
    top8 = arr[:8]
    good = [h for h in top8 if h['ev'] and h['ev'] >= 1.15]
    best = max((h['ev'] or 0) for h in top8)
    score += 2 if (len(good) >= 2 or best >= 1.5) else 1 if good else -2 if best < 0.8 else 0
    return '買い' if score >= 3 else '少額で買い' if score >= 1 else '見送り推奨'


def bet_plan(arr):
    """おすすめ買い目。各要素は {'label','text','parts':[(券種, [組み合わせ,...]), ...]}"""
    n = [h['n'] for h in arr]
    plan = []
    if len(n) >= 3:
        plan.append({'label': '本線', 'text': f"馬連 {n[0]}-{n[1]}, {n[0]}-{n[2]}",
                     'parts': [('馬連', [(n[0], n[1]), (n[0], n[2])])]})
        plan.append({'label': '本線', 'text': f"ワイド {n[0]}-{n[1]}", 'parts': [('ワイド', [(n[0], n[1])])]})
    if len(n) >= 6:
        combos = sorted({tuple(sorted((n[0], b, c))) for b in n[1:3] for c in n[1:6] if b != c})
        plan.append({'label': '本線', 'text': f"3連複 {n[0]} - {n[1]},{n[2]} - {','.join(map(str, n[1:6]))}（{len(combos)}点）",
                     'parts': [('3連複', combos)]})
    evs = [h for h in arr[:8] if h.get('ev') and h['ev'] >= 1.15]
    if evs:
        b = max(evs, key=lambda h: h['ev'])
        plan.append({'label': '妙味', 'text': f"単複 {b['n']}（期待値{b['ev']:.2f}）",
                     'parts': [('単勝', [(b['n'],)]), ('複勝', [(b['n'],)])]})
    anas = [h for h in arr if h['anaFlag']][:2]
    if anas:
        plan.append({'label': '穴', 'text': "ワイド " + ', '.join(f"{n[0]}-{h['n']}" for h in anas),
                     'parts': [('ワイド', [(n[0], h['n']) for h in anas])]})
    t = trifecta_high(arr)
    if t:
        plan.append(t)
    return plan


def bets(arr):
    return [f"{b['label']} {b['text']}" for b in bet_plan(arr)]


def trifecta_high(arr):
    """高目狙いの3連単フォーメーション。
    1着：◎＋期待値が一番高い馬 ／ 2着：1着候補＋○▲＋穴馬・高期待値の馬 ／ 3着：2着の馬＋△△△☆"""
    if len(arr) < 5:
        return None
    top8 = arr[:8]
    value = sorted([h for h in top8[1:] if h.get('ev') and h['ev'] >= 1.3], key=lambda h: -h['ev'])
    anas = [h for h in arr if h['anaFlag']]

    first = [arr[0]] + value[:1]
    # 2着：1着候補同士の入れ替わり（例：穴馬が勝って◎が2着）も拾えるよう、1着候補も必ず入れる
    second = list(first)
    for h in arr[1:3] + anas + value:
        if h not in second and len(second) < len(first) + 3:
            second.append(h)
    third = []
    for h in second + first + arr[1:7]:
        if h not in third and len(third) < 7:
            third.append(h)

    combos = [(a['n'], b['n'], c['n']) for a in first for b in second for c in third
              if len({a['n'], b['n'], c['n']}) == 3]
    j = lambda hs: ','.join(str(h['n']) for h in hs)
    return {'label': '高目', 'text': f"3連単 1着{j(first)} → 2着{j(second)} → 3着{j(third)}（{len(combos)}点）",
            'parts': [('3連単', combos)]}


# ═════════════════════════════════════════
# 追加要素 ③タイム・上がり ④枠順・コース・当日の馬場 ⑤騎手データ・馬の状態
# ═════════════════════════════════════════
# 良馬場の目安タイム（JRAの平均的な勝ちタイム・秒）。距離の間は直線でつなぐ
STD_TIME = {
    '芝': [(1000, 56.0), (1200, 68.8), (1400, 81.8), (1500, 88.5), (1600, 94.3), (1800, 107.5), (2000, 120.5),
           (2200, 133.5), (2300, 140.5), (2400, 146.5), (2500, 153.0), (2600, 160.0), (3000, 184.5),
           (3200, 198.0), (3600, 226.0)],
    'ダ': [(1000, 59.8), (1150, 68.5), (1200, 72.0), (1300, 79.0), (1400, 85.0), (1600, 97.5), (1700, 105.0),
           (1800, 113.0), (1900, 119.5), (2000, 125.0), (2100, 131.5), (2400, 156.0), (2500, 163.0)],
}
# 競馬場ごとの時計の出やすさ（1000mあたりの秒。＋は時計がかかる）
VENUE_ADJ = {'芝': {'札幌': .6, '函館': .6, '福島': .3, '新潟': -.2, '東京': -.3, '中山': .2, '中京': .2,
                    '京都': -.2, '阪神': 0, '小倉': 0},
             'ダ': {'札幌': .3, '函館': .3, '福島': .2, '新潟': 0, '東京': -.2, '中山': .2, '中京': .2,
                    '京都': 0, '阪神': 0, '小倉': -.1}}
# 馬場状態による時計の変化（1000mあたりの秒）。ダートは湿ると速くなる
COND_ADJ = {'芝': {'良': 0, '稍': .5, '重': 1.2, '不': 2.0}, 'ダ': {'良': 0, '稍': -.3, '重': -.6, '不': -.8}}

# コースの枠順の有利不利（＋は内枠有利、－は外枠有利）。よく知られたコースのみ
COURSE_DRAW = {('中山', '芝', 1200): 1.0, ('中山', '芝', 1600): 1.5, ('東京', '芝', 2000): 1.5,
               ('札幌', '芝', 1200): 0.7, ('函館', '芝', 1200): 0.7, ('福島', '芝', 1200): 0.7,
               ('小倉', '芝', 1200): 0.5, ('新潟', '芝', 1000): -2.0,
               ('中山', 'ダ', 1200): -1.0, ('東京', 'ダ', 1600): -1.0, ('阪神', 'ダ', 1400): -0.7,
               ('中京', 'ダ', 1400): -0.7, ('新潟', 'ダ', 1200): -0.7, ('福島', 'ダ', 1150): -0.7}

_cache = {}          # 取得結果の一時保存 {キー: (保存時刻, 値)}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def std_time(surf, dist, venue, cond):
    tab = STD_TIME.get(surf)
    if not tab or venue not in VENUE_ADJ[surf]:
        return None
    if dist <= tab[0][0]:
        base = tab[0][1] * dist / tab[0][0]
    elif dist >= tab[-1][0]:
        base = tab[-1][1] * dist / tab[-1][0]
    else:
        for (d0, t0), (d1, t1) in zip(tab, tab[1:]):
            if d0 <= dist <= d1:
                base = t0 + (t1 - t0) * (dist - d0) / (d1 - d0)
                break
    k = dist / 1000
    return base + (VENUE_ADJ[surf][venue] + COND_ADJ[surf].get(cond, 0)) * k


def speed_index(l):
    """1走分の簡易タイム指数（目安タイムより1000mあたり1秒速いと＋10）"""
    surf, dist = l['dist'][:1], dist_num(l['dist'])
    if not l.get('t') or surf not in STD_TIME or not dist:
        return None
    st_ = std_time(surf, dist, l['p'], l['cond'])
    if not st_:
        return None
    return (st_ - l['t']) / (dist / 1000) * 10


def rate_by_diff(d, steps):
    a, b = steps
    return 5 if d >= b else 4 if d >= a else 3 if d > -a else 2 if d > -b else 1


def time_factors(race, horses):
    """③ 走破タイム（簡易指数）と上がり3ハロン"""
    surf, dist = race['surf'], race['dist']
    for h in horses:
        near = [l for l in h['lines'] if l['pos'] > 0 and l['dist'][:1] == surf
                and abs(dist_num(l['dist']) - dist) <= 600][:4]
        idx = sorted([x for x in (speed_index(l) for l in near) if x is not None], reverse=True)
        h['si'] = sum(idx[:2]) / len(idx[:2]) if idx else None
        ag = sorted(l['l3'] - 0.0006 * (dist_num(l['dist']) - dist) for l in near[:3] if l.get('l3'))
        h['ag'] = sum(ag[:2]) / len(ag[:2]) if ag else None

    sis = sorted(h['si'] for h in horses if h['si'] is not None)
    med = sis[len(sis) // 2] if sis else None
    ags = sorted(h['ag'] for h in horses if h['ag'] is not None)
    pace_k = 1.2 if race['pace'] == 'fast' else 0.8 if race['pace'] == 'slow' else 1.0
    for h in horses:
        h['siRate'] = rate_by_diff(h['si'] - med, (3, 8)) if h['si'] is not None and len(sis) >= 4 else 3
        if h['ag'] is not None and len(ags) >= 4:
            pr = ags.index(h['ag']) / (len(ags) - 1)
            h['agRate'] = 5 if pr <= .15 else 4 if pr <= .35 else 3 if pr <= .65 else 2 if pr <= .85 else 1
            h['agRank'] = ags.index(h['ag']) + 1
        else:
            h['agRate'], h['agRank'] = 3, None
        closer = 1.3 if h['st'] in ('差', '追') else 1.0
        h['bTime'] = (h['siRate'] - 3) / 2 * 0.02
        h['bAgari'] = (h['agRate'] - 3) / 2 * 0.012 * closer * pace_k


def draw_pos(h, N):
    return ((h['n'] or 1) - 1) / max(1, N - 1)   # 0=最内、1=大外


def draw_factors(race, horses):
    """④-1 コースの枠順の有利不利"""
    N = len(horses)
    key = (race['venue'], race['surf'], race['dist'])
    bias = COURSE_DRAW.get(key)
    if bias is None:
        bias = 0.3 if (race['surf'] == '芝' and race['dist'] <= 1400 and race['venue'] in VENUE_ADJ['芝']) else 0
    race['drawBias'] = bias
    for h in horses:
        h['bDraw'] = bias * 0.008 * (0.5 - draw_pos(h, N)) * 2 * min(1, N / 14)


def parse_race_date(soup):
    txt = (soup.title.get_text() if soup.title else '') + ' '.join(
        m.get('content', '') for m in soup.find_all('meta'))
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', txt)
    return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def race_ids_on(date):
    def load():
        url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date:%Y%m%d}"
        res = SESSION.get(url, headers=HEADERS, timeout=10)
        return sorted(set(re.findall(r'race_id=(\d{12})', res.text)))
    return cached(('list', date), 600, load)


def parse_result_brief(race_id):
    """終わったレースの結果から、上位3頭の馬番・最終コーナーの位置・頭数・芝ダを取り出す"""
    def load():
        soup = fetch_soup(f"https://race.netkeiba.com/race/result.html?race_id={race_id}")
        d1 = soup.select_one('.RaceData01')
        sm = re.search(r'(芝|ダ)', d1.get_text()) if d1 else None
        table = soup.select_one('table#All_Result_Table') or soup.select_one('table.RaceTable01')
        if not table or not sm:
            return None
        header = table.select_one('tr.Header') or table.find('tr')
        cols = [clean(c.get_text()) for c in header.find_all(['th', 'td'])]
        col = lambda *ks: next((i for i, c in enumerate(cols) if any(k in c for k in ks)), None)
        i_n, i_c = col('馬番'), col('通過')
        rows = []
        for tr in table.select('tr.HorseList') or table.find_all('tr')[1:]:
            tds = tr.find_all('td')
            if not tds or i_n is None or i_n >= len(tds):
                continue
            rk = clean(tds[0].get_text())
            n = to_float(tds[i_n].get_text())
            corners = re.findall(r'\d+', tds[i_c].get_text()) if i_c is not None and i_c < len(tds) else []
            if n:
                rows.append((int(rk) if rk.isdigit() else 99, int(n), int(corners[-1]) if corners else None))
        if not rows or not any(r[0] == 1 for r in rows):
            return None
        return {'surf': sm.group(1), 'N': len(rows), 'top3': [r for r in rows if r[0] <= 3]}
    return cached(('res', race_id), 86400, load)


def track_bias(race, race_id, date):
    """④-2 当日の同じ競馬場・同じ芝ダの、終わったレースの上位馬から馬場の傾向を読む"""
    if not date or 'nar' in race.get('base', ''):
        return None
    try:
        ids = [i for i in race_ids_on(date)
               if i[:10] == race_id[:10] and int(i[-2:]) < int(race_id[-2:])]
    except Exception:
        return None
    if not ids:
        return None
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = [r for r in ex.map(lambda i: _safe(parse_result_brief, i), ids) if r and r['surf'] == race['surf']]
    if len(res) < 3:
        return None
    front = inner = tot = 0
    for r in res:
        cut = max(2, round(r['N'] * 0.3))
        for _, n, c in r['top3']:
            tot += 1
            front += 1 if (c is not None and c <= cut) else 0
            inner += 1 if n <= r['N'] / 2 else 0
    fb = clamp((front / tot - 0.45) / 0.3, -1, 1)
    ib = clamp((inner / tot - 0.5) / 0.25, -1, 1)
    words = []
    if fb >= 0.4: words.append('前残り')
    elif fb <= -0.4: words.append('差し有利')
    if ib >= 0.4: words.append('内有利')
    elif ib <= -0.4: words.append('外有利')
    return {'front': fb, 'inner': ib, 'races': len(res), 'text': '・'.join(words) or 'フラット'}


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print(f'[extra] {fn.__name__} 失敗: {e}', flush=True)
        return None


def bias_factors(race, horses):
    tb = race.get('trackBias')
    N = len(horses)
    for h in horses:
        b = 0
        if tb:
            style = 1 if h['st'] in ('逃', '先') else -1 if h['st'] in ('差', '追') else 0
            b += tb['front'] * style * 0.012
            b += tb['inner'] * (0.5 - draw_pos(h, N)) * 2 * 0.01
        h['bTrack'] = b


def fetch_jockey_stats(jid):
    """騎手の今年の成績（騎乗数・複勝率）。取れなければ None"""
    def load():
        soup = fetch_soup(f"https://db.netkeiba.com/jockey/{jid}/")
        year = str(jst_today().year)
        for table in soup.find_all('table'):
            head = table.find('tr')
            if not head:
                continue
            cols = [clean(c.get_text()) for c in head.find_all(['th', 'td'])]
            if not any('複勝率' in c for c in cols) or '1着' not in cols:
                continue
            idx = {k: cols.index(k) for k in ('1着', '2着', '3着', '着外') if k in cols}
            if len(idx) < 4:
                continue
            for want in (year, '本年', '累計'):
                for tr in table.find_all('tr')[1:]:
                    cells = [clean(c.get_text()) for c in tr.find_all(['th', 'td'])]
                    if cells and want in cells[0] and len(cells) > max(idx.values()):
                        v = {k: int(to_float(cells[i]) or 0) for k, i in idx.items()}
                        rides = sum(v.values())
                        if rides >= 30:
                            return {'rides': rides, 'win': v['1着'] / rides,
                                    'fuku': (v['1着'] + v['2着'] + v['3着']) / rides,
                                    'span': '今年' if want != '累計' else '通算'}
        return None
    return cached(('jk', jid), 86400, load)


def jst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()


def jockey_factors(race, horses):
    """⑤-1 騎手の成績と乗り替わり"""
    for h in horses:
        js = h.get('jkStats')
        b = 0
        if js:
            b += clamp((js['fuku'] - 0.22) * 0.08, -0.012, 0.024)
        prev = next((l['j'] for l in h['lines'] if l.get('j')), '')
        norm = lambda x: re.sub(r'[▲△☆★◇\s]', '', x or '')[:2]
        h['change'] = bool(prev) and norm(prev) != norm(h['jockey'])
        h['prevJockey'] = prev
        if h['change'] and js and js['fuku'] >= 0.35:
            b += 0.008   # 上位騎手への乗り替わり
        h['bJockey'] = b


def condition_factors(race, horses):
    """⑤-2 馬の状態（前走からの間隔、叩き2戦目、当日の馬体重）"""
    for h in horses:
        b, notes = 0, []
        w = h.get('weeks')
        if w is None and len(h['lines']) >= 1 and race.get('date'):
            try:
                ld = datetime.date(*map(int, h['lines'][0]['d'].split('.')))
                w = max(0, (race['date'] - ld).days // 7 - 1)
            except Exception:
                w = None
        h['weeksCalc'] = w
        if w is not None:
            if w == 0:
                b -= 0.005; notes.append('連闘')
            elif w >= 20:
                b -= 0.015; notes.append(f'中{w}週の長期休み明け')
            elif w >= 10:
                b -= 0.008; notes.append(f'中{w}週の休み明け')
            elif len(h['lines']) >= 2:
                try:
                    d0 = datetime.date(*map(int, h['lines'][0]['d'].split('.')))
                    d1 = datetime.date(*map(int, h['lines'][1]['d'].split('.')))
                    if (d0 - d1).days >= 70:
                        b += 0.008; notes.append('休み明けを1度使った叩き2戦目')
                except Exception:
                    pass
        dd = h.get('bwDiff')
        if dd is not None:
            if abs(dd) >= 20 and not (dd > 0 and (w or 0) >= 10):
                b -= 0.015; notes.append(f'馬体重{dd:+d}kgの大幅増減')
            elif abs(dd) >= 12 and not (dd > 0 and (w or 0) >= 10):
                b -= 0.008; notes.append(f'馬体重{dd:+d}kg')
        h['bCond'] = clamp(b, -0.02, 0.02)
        h['condNotes'] = notes


def extra_factors(race, horses, soup, race_id):
    """③④⑤をまとめて計算し、各馬の xBonus に入れる"""
    race['date'] = race.get('date') or parse_race_date(soup)
    jids = {h['jid'] for h in horses if h.get('jid')}
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_tb = ex.submit(_safe, track_bias, race, race_id, race['date'])
        f_js = {j: ex.submit(_safe, fetch_jockey_stats, j) for j in jids}
        race['trackBias'] = f_tb.result()
        stats = {j: f.result() for j, f in f_js.items()}
    for h in horses:
        h['jkStats'] = stats.get(h.get('jid'))
    time_factors(race, horses)
    draw_factors(race, horses)
    bias_factors(race, horses)
    jockey_factors(race, horses)
    condition_factors(race, horses)
    career_factors(race, horses)
    for h in horses:
        h['xBonus'] = h['bTime'] + h['bAgari'] + h['bDraw'] + h['bTrack'] + h['bJockey'] + h['bCond'] + h['bCareer']


# ═════════════════════════════════════════
# 全成績（馬ごとのページから）
# ═════════════════════════════════════════
LEFT_TURN = {'東京', '中京', '新潟', '川崎', '船橋', '浦和', '盛岡'}   # 左回りの競馬場
KNOWN_DIR = LEFT_TURN | {'札幌', '函館', '福島', '中山', '京都', '阪神', '小倉', '大井', '門別', '水沢',
                         '金沢', '笠松', '名古屋', '園田', '姫路', '高知', '佐賀'}


def age_w(l, race):
    """古いレースほど軽く扱う重み（2年で半分）"""
    try:
        ld = datetime.date(*map(int, l['d'].split('.')))
        ref = race.get('date') or jst_today()
        return 0.5 ** (max(0, (ref - ld).days) / 730)
    except Exception:
        return 0.5


def fetch_career(hid):
    """netkeibaの馬のページから全成績を読む。読めなければ None"""
    def load():
        for url in (f"https://db.netkeiba.com/horse/result/{hid}/", f"https://db.netkeiba.com/horse/{hid}/"):
            try:
                soup = fetch_soup(url)
            except Exception:
                continue
            lines = parse_career_table(soup)
            if lines:
                return lines
        return None
    return cached(('career', hid), 43200, load)


def parse_career_table(soup):
    for table in soup.find_all('table'):
        head = table.find('tr')
        if not head:
            continue
        cols = [clean(c.get_text()) for c in head.find_all(['th', 'td'])]
        if not (any(c == '日付' for c in cols) and any('着順' in c for c in cols) and any('距離' in c for c in cols)):
            continue
        col = lambda *ks: next((i for i, c in enumerate(cols) if any(c == k or c.startswith(k) for k in ks)), None)
        I = {'d': col('日付'), 'p': col('開催'), 'r': col('レース名'), 'f': col('頭数'), 'n': col('馬番'),
             'pop': col('人気'), 'pos': col('着順'), 'j': col('騎手'), 'w': col('斤量'), 'dist': col('距離'),
             'cond': col('馬場'), 't': col('タイム'), 'm': col('着差'), 'ps': col('通過'), 'l3': col('上り'),
             'bw': col('馬体重'), 'win': col('勝ち馬')}
        out = []
        for tr in table.find_all('tr')[1:]:
            tds = tr.find_all('td')
            g = lambda k: (clean(tds[I[k]].get_text()) if I.get(k) is not None and I[k] < len(tds) else '')
            dm = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', g('d'))
            dist_m = re.match(r'(芝|ダ|障)\D*(\d{3,4})', g('dist'))
            if not dm or not dist_m:
                continue
            pos_t = g('pos')
            pos = int(pos_t) if pos_t.isdigit() else 0
            m = to_float(g('m'))
            if m is not None and g('m').startswith('-'):
                m = -m
            if m is None:
                if pos == 1:
                    m = 0.0
                elif pos > 0:
                    continue
                else:
                    m = 9.9
            tt = re.match(r'(\d):(\d{2}\.\d)', g('t'))
            cond = (g('cond') or '良')[:1]
            out.append({
                'd': f"{dm.group(1)}.{int(dm.group(2)):02d}.{int(dm.group(3)):02d}",
                'p': re.sub(r'\d', '', g('p')) or '?', 'pos': pos, 'r': g('r'),
                'dist': dist_m.group(1) + dist_m.group(2), 'cond': cond if cond in '良稍重不' else '良',
                't': int(tt.group(1)) * 60 + float(tt.group(2)) if tt else 0,
                'f': int(to_float(g('f')) or 0) or 16, 'n': int(to_float(g('n')) or 0),
                'pop': int(to_float(g('pop')) or 0), 'j': g('j'), 'w': to_float(g('w')) or 0,
                'ps': g('ps'), 'l3': to_float(g('l3')) or 0, 'bw': g('bw'), 'win': g('win'), 'm': m,
            })
        if out:
            return out
    return None


def load_careers(race, horses):
    """全頭の全成績を同時に取りに行く。取れなかった馬は出馬表の5走で代用"""
    hids = [h['hid'] for h in horses if h.get('hid')]
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = dict(zip(hids, ex.map(lambda i: _safe(fetch_career, i), hids)))
    cutoff = race['date'].strftime('%Y.%m.%d') if race.get('date') else None
    n_ok = 0
    for h in horses:
        c = got.get(h.get('hid'))
        if c:
            h['career'] = [l for l in c if not cutoff or l['d'] < cutoff]  # 当日以降の成績は使わない
            h['careerOk'] = True
            n_ok += 1
        else:
            h['career'] = h['lines']
            h['careerOk'] = False
    race['careerOk'] = n_ok
    print(f"[career] 全成績 {n_ok}/{len(horses)}頭", flush=True)


def rec_str(ls):
    t3 = sum(1 for l in ls if 0 < l['pos'] <= 3)
    return f"{len(ls)}戦{sum(1 for l in ls if l['pos'] == 1)}勝・3着内{t3}回", t3


def career_factors(race, horses):
    """全成績から：回り、季節、休み明けの実績、力の上限"""
    rcv = CLS[race['clsIdx']][1]
    left_today = race['venue'] in LEFT_TURN
    month = race['date'].month if race.get('date') else None
    for h in horses:
        cs = [l for l in h.get('career', []) if l['pos'] > 0 and is_flat(l)]
        b, notes = 0.0, []
        if h.get('careerOk') and len(cs) >= 4:
            # 回り
            known = [l for l in cs if l['p'] in KNOWN_DIR]
            same_dir = [l for l in known if (l['p'] in LEFT_TURN) == left_today]
            other = [l for l in known if (l['p'] in LEFT_TURN) != left_today]
            if len(same_dir) >= 2 and len(other) >= 2:
                r1 = sum(1 for l in same_dir if l['pos'] <= 3) / len(same_dir)
                r2 = sum(1 for l in other if l['pos'] <= 3) / len(other)
                if r1 - r2 >= 0.25:
                    b += 0.006; notes.append(f"{'左' if left_today else '右'}回りが得意（{rec_str(same_dir)[0]}）")
                elif r2 - r1 >= 0.25:
                    b -= 0.006; notes.append(f"{'左' if left_today else '右'}回りは苦手（{rec_str(same_dir)[0]}）")
            # 季節（前後1か月）
            if month:
                def near_m(l):
                    mm = int(l['d'][5:7]); dd = min(abs(mm - month), 12 - abs(mm - month))
                    return dd <= 1
                sea = [l for l in cs if near_m(l)]
                if len(sea) >= 3:
                    r1 = sum(1 for l in sea if l['pos'] <= 3) / len(sea)
                    r0 = sum(1 for l in cs if l['pos'] <= 3) / len(cs)
                    if r1 - r0 >= 0.25:
                        b += 0.005; notes.append(f"この時期に好走が多い（{rec_str(sea)[0]}）")
                    elif r0 - r1 >= 0.25:
                        b -= 0.005; notes.append(f"この時期は成績が落ちる（{rec_str(sea)[0]}）")
            # 力の上限：今回より上のクラスで3着以内（古いほど軽く）
            best = None
            for l in cs:
                ci = class_of_text(l['r'], l['p'])
                if ci != 8 and l['pos'] <= 3 and CLS[ci][1] > rcv:
                    v = (CLS[ci][1] - rcv) * age_w(l, race)
                    if not best or v > best[0]:
                        best = (v, l)
            if best:
                b += min(best[0], 2) / 2 * 0.01
                l = best[1]
                notes.append(f"格上の{l['r']}で{l['pos']}着の実績（{l['d'][:4]}年）")
        # 休み明けの実績（今回が中10週以上のとき）
        w = h.get('weeksCalc')
        if w is not None and w >= 10 and len(cs) >= 2:
            fresh = []
            sorted_cs = sorted(cs, key=lambda l: l['d'])
            for prev, cur in zip(sorted_cs, sorted_cs[1:]):
                try:
                    gap = (datetime.date(*map(int, cur['d'].split('.'))) - datetime.date(*map(int, prev['d'].split('.')))).days
                except Exception:
                    continue
                if gap >= 70:
                    fresh.append(cur)
            if len(fresh) >= 2:
                txt, t3 = rec_str(fresh)
                if t3 / len(fresh) >= 0.5:
                    b += 0.01; notes.append(f"休み明けは得意（{txt}）")
                elif t3 == 0:
                    b -= 0.005; notes.append(f"休み明けは苦手（{txt}）")
        h['bCareer'] = clamp(b, -0.015, 0.02)
        h['careerNotes'] = notes


# ═════════════════════════════════════════
# まとめ：LINEに返す文章を作る
# ═════════════════════════════════════════
def analyze(text):
    race_id, base = parse_input(text)
    if not race_id:
        return None, "URLからレースIDを読み取れませんでした。"
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_page = ex.submit(fetch_soup, f"{base}/race/shutuba_past.html?race_id={race_id}")
        f_odds = ex.submit(fetch_win_odds, race_id, base)
        soup = f_page.result()
        odds = f_odds.result()
    race = parse_race_info(soup, race_id, base)
    horses = [h for h in parse_shutuba_past(soup) if not h['cancelled'] and h['n']]
    if not horses:
        return None, "出走馬を読み取れませんでした。ページの形式が変わった可能性があります。"

    # ペース：netkeibaの展開予想が無ければ、逃げ馬の数から推定
    nige = sum(1 for h in horses if h['st'] == '逃')
    race['pace_from'] = 'netkeiba展開予想'
    if not race['pace']:
        race['pace'] = 'fast' if nige >= 3 else 'slow' if nige <= 1 else 'base'
        race['pace_from'] = '逃げ馬の数から推定'
    race['nige'] = nige
    race['base'] = base
    race['id'] = race_id
    race['date'] = parse_race_date(soup)
    load_careers(race, horses)

    auto_heuristics(race, horses)
    extra_factors(race, horses, soup, race_id)
    arr = ranked(race, horses, odds)
    return (race, arr), None


def format_reply(race, arr):
    pct, label = chaos_info(arr)
    N = len(arr)
    out = [f"🐎 シェイクユアハート",
           f"{race['venue']}{race['R']}R {race['name']}" + ("" if CLS[race['clsIdx']][0] in race['name'] else f"（{CLS[race['clsIdx']][0]}）"),
           f"{race['surf']}{race['dist']}m／{race['going']}{'' if race['going_known'] else '（未発表→良で計算）'}／{N}頭",
           f"波乱度 {pct}%（{label}）",
           f"判定：{verdict(arr)}", ""]

    # 展開・相手関係
    pace = race['pace']
    tenkai = {'fast': '逃げ馬が多くペースが上がりやすい→差し・追込有利',
              'slow': '逃げ馬が少なくスローになりやすい→前に行く馬が有利',
              'base': '平均ペース想定→脚質の有利不利は小さめ'}[pace]
    out.append(f"【展開】{PACE_LABEL[pace]}（{race['pace_from']}・逃げ{race['nige']}頭）")
    out.append(tenkai)
    if race['going'] != '良':
        out.append("道悪のため、道悪適性（血統・馬格・実績）を加点し、実力差を少し縮めて評価")
    if race.get('trackBias'):
        tb = race['trackBias']
        out.append(f"【今日の馬場】{tb['text']}（{race['surf']}・終わった{tb['races']}レースから）")
    out.append("")

    out.append("【予想印】")
    for i, h in enumerate(arr[:7]):
        tags = []
        if h['anaFlag']: tags.append('穴')
        if h['jb']: tags.append('騎手◎')
        if h['isNew']: tags.append('初')
        o = f"{h['o']}倍" if h['o'] else "ｵｯｽﾞ未"
        ev = f"／期待値{h['ev']:.2f}" if h['ev'] else ""
        out.append(f"{MARKS[i]} {h['n']} {h['name']} [{h['rank']}] ×{h['a']:.3f}"
                   + (f" 〈{'・'.join(tags)}〉" if tags else ""))
        out.append(f"　勝率{h['p'] * 100:.1f}%／{o}{ev}／{h['jockey']}")
        reasons = list(h['why'])
        if h['rivalNotes']: reasons.append(h['rivalNotes'][0])
        if h['mudb'] > 0 and race['going'] != '良' and h['mudParts']: reasons.append('、'.join(h['mudParts']))
        if reasons:
            out.append("　" + "／".join(reasons[:3]))
    out.append("")

    out.append("【おすすめ買い目】")
    out.extend(bets(arr))
    out.append("")
    out.append("※スコア×1.00が基準。S≥1.08／A≥1.03／B≥1.00／C≥0.96／D")
    msg = "\n".join(out)
    return msg[:4900]


def generate_prediction(text):
    try:
        result, err = analyze(text)
        if err:
            return err
        return format_reply(*result)
    except requests.HTTPError as e:
        return f"netkeibaへのアクセスに失敗しました（{e.response.status_code}）。"
    except Exception as e:
        return f"予想中にエラーが発生しました。\n詳細: {e}"



# ═════════════════════════════════════════
# 画像で送る（ダーク×ゴールドのインフォグラフィック）
# ═════════════════════════════════════════
IMG_DIR = '/tmp/syh_img'
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
FONT_FILES = {
    'sans_b': 'Sans/OTF/Japanese/NotoSansCJKjp-Bold.otf',
    'sans_r': 'Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf',
}
FONT_MIRRORS = [  # 1つ目がダメなら2つ目から取る（jsDelivrは大きいファイルを断ることがあるので後ろ）
    'https://github.com/notofonts/noto-cjk/raw/main/{}',
    'https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/{}',
]
FONT_LOCAL = {  # サーバーやPCに最初から入っている場合はそれを使う
    'sans_b': ['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc'],
    'sans_r': ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'],
    'serif_b': ['/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc'],
}
_font_path_cache = {}
import threading
_font_locks = {k: threading.Lock() for k in ('sans_b', 'sans_r')}
_render_lock = threading.Lock()
fonts_ready = threading.Event()


def font_path(kind):
    """日本語フォントを探す。無ければ初回だけダウンロードして /tmp に置く"""
    if kind in _font_path_cache:
        return _font_path_cache[kind]
    if kind not in FONT_FILES:  # 明朝体（タイトル用）はPCに入っていれば使い、無ければゴシック太字
        for c in FONT_LOCAL.get(kind, []):
            if os.path.exists(c):
                _font_path_cache[kind] = c
                return c
        return font_path('sans_b')
    fname = os.path.basename(FONT_FILES[kind])
    cands = [os.path.join(FONT_DIR, fname)] + FONT_LOCAL.get(kind, []) + [os.path.join('/tmp', fname)]
    for c in cands:
        if os.path.exists(c):
            _font_path_cache[kind] = c
            return c
    with _font_locks[kind]:
        dst = os.path.join('/tmp', fname)
        if os.path.exists(dst):
            _font_path_cache[kind] = dst
            return dst
        last = None
        for mirror in FONT_MIRRORS:
            try:
                r = requests.get(mirror.format(FONT_FILES[kind]), timeout=60)
                r.raise_for_status()
                if len(r.content) < 1_000_000:
                    raise ValueError('フォントのダウンロードが不完全')
                tmp = dst + '.part'
                with open(tmp, 'wb') as f:
                    f.write(r.content)
                os.replace(tmp, dst)
                _font_path_cache[kind] = dst
                return dst
            except Exception as e:
                last = e
                print(f'[font] {kind} download failed from {mirror}: {e}', flush=True)
    if kind != 'sans_b':
        return font_path('sans_b')
    raise RuntimeError(f'日本語フォントを取得できません（{last}）')


_font_obj_cache = {}


def F(kind, size):
    key = (kind, size)
    if key not in _font_obj_cache:
        _font_obj_cache[key] = ImageFont.truetype(font_path(kind), size, index=0)
    return _font_obj_cache[key]


def fonts_ok(timeout=20):
    """画像用フォントが使えるか。ファイルが既にあれば即OK、無ければ準備を待つ"""
    def local():
        for k in ('sans_b', 'sans_r'):
            fname = os.path.basename(FONT_FILES[k])
            if not any(os.path.exists(c) for c in [os.path.join(FONT_DIR, fname), os.path.join('/tmp', fname)] + FONT_LOCAL.get(k, [])):
                return False
        return True
    if fonts_ready.is_set() or local():
        return True
    fonts_ready.wait(timeout=timeout)
    return fonts_ready.is_set() or local()


def preload_fonts():
    t = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda k: _try_font(k), ('sans_b', 'sans_r')))
    if all(results):
        fonts_ready.set()
    print(f'[font] 準備{"完了" if all(results) else "失敗"}（{time.time() - t:.1f}秒）', flush=True)


def _try_font(k):
    try:
        font_path(k)
        return True
    except Exception as e:
        print(f'[font] preload failed: {e}', flush=True)
        return False


GOLD = (221, 183, 98)
GOLD_D = (150, 116, 52)
SILVER = (232, 234, 242)
MUTED = (150, 160, 184)
WAKU = {1: ((245, 245, 245), (20, 20, 20)), 2: ((30, 30, 30), (255, 255, 255)), 3: ((214, 48, 49), (255, 255, 255)),
        4: ((41, 98, 214), (255, 255, 255)), 5: ((247, 206, 38), (20, 20, 20)), 6: ((38, 160, 80), (255, 255, 255)),
        7: ((245, 142, 38), (20, 20, 20)), 8: ((240, 128, 170), (20, 20, 20))}
RANK_C = {'S': (235, 90, 90), 'A': (240, 170, 60), 'B': (90, 170, 240), 'C': (140, 150, 170), 'D': (100, 108, 125)}


def gradient_bg(W, H, top=(20, 30, 58), bot=(6, 9, 18)):
    """縦グラデーションを1列だけ作って引き伸ばす（1行ずつ塗るより速い）"""
    col = Image.new('RGB', (1, 256))
    col.putdata([tuple(int(top[i] * (1 - t / 255) + bot[i] * t / 255) for i in range(3)) for t in range(256)])
    return col.resize((W, H), Image.BILINEAR)


def tw(d, t, f):
    return d.textlength(t, font=f)


def fit_text(d, t, f, maxw):
    if tw(d, t, f) <= maxw:
        return t
    while t and tw(d, t + '…', f) > maxw:
        t = t[:-1]
    return t + '…'


def ctext(d, cx, y, t, f, fill):
    d.text((cx - tw(d, t, f) / 2, y), t, font=f, fill=fill)


def gold_frame(d, box, r=14, w=2):
    d.rounded_rectangle(box, radius=r, outline=GOLD_D, width=w)
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 + 5, y0 + 5, x1 - 5, y1 - 5), radius=max(2, r - 4), outline=(80, 66, 40), width=1)


def render_image(race, arr):
    W, PAD = 1080, 36
    row_h = 92
    blist = bets(arr)
    tmp = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    bet_rows = []
    for line in blist:
        lab, rest_ = (line.split(' ', 1) + [''])[:2]
        bet_rows.append((lab, wrap(tmp, rest_, F('sans_b', 28), W - PAD * 2 - 170)))
    bet_h = sum(22 + 40 * len(ls) for _, ls in bet_rows)
    H = 610 + 64 + row_h * len(arr) + 40 + 90 + bet_h + 120

    img = gradient_bg(W, H)  # 背景グラデーション（夜空のイメージ）
    d = ImageDraw.Draw(img)
    gold_frame(d, (14, 14, W - 14, H - 14), r=22, w=3)

    # ── ヘッダー ──
    ctext(d, W / 2, 44, 'K E I B A   P R E D I C T I O N', F('sans_b', 20), MUTED)
    ctext(d, W / 2, 74, 'シェイクユアハート', F('serif_b', 66), GOLD)
    d.line((PAD + 120, 170, W - PAD - 120, 170), fill=GOLD_D, width=2)
    title = f"{race['venue']}{race['R']}R  {race['name']}"
    ft = F('serif_b', 54)
    while tw(d, title, ft) > W - PAD * 2 and ft.size > 30:
        ft = F('serif_b', ft.size - 4)
    ctext(d, W / 2, 190, title, ft, SILVER)

    # バッジ（場R・距離・馬場・頭数）
    badges = [CLS[race['clsIdx']][0], f"{race['surf']}{race['dist']}m",
              race['going'] + ('' if race['going_known'] else '（想定）'), f"{len(arr)}頭"]
    fb = F('sans_b', 30)
    bw_ = (W - PAD * 2 - 18 * 3) / 4
    for i, b in enumerate(badges):
        x = PAD + i * (bw_ + 18)
        d.rounded_rectangle((x, 280, x + bw_, 340), radius=10, fill=(14, 22, 42), outline=GOLD_D, width=2)
        ctext(d, x + bw_ / 2, 290, fit_text(d, b, fb, bw_ - 16), fb, SILVER)

    # 波乱度・判定・展開
    pct, label = chaos_info(arr)
    vd = verdict(arr)
    vshort = vd.split('（')[0]
    stats = [('波乱度', f'{pct}%', label), ('判定', vshort, ''), ('展開', PACE_LABEL[race['pace']], f"逃げ{race['nige']}頭")]
    sw = (W - PAD * 2 - 18 * 2) / 3
    for i, (k, v, sub) in enumerate(stats):
        x = PAD + i * (sw + 18)
        d.rounded_rectangle((x, 364, x + sw, 490), radius=14, fill=(16, 24, 46))
        gold_frame(d, (x, 364, x + sw, 490), r=14)
        ctext(d, x + sw / 2, 376, k, F('sans_r', 24), MUTED)
        col = GOLD if k != '判定' else ((120, 220, 140) if '買い' in v else (240, 170, 60) if '様子見' in v else (235, 110, 110))
        ctext(d, x + sw / 2, 400, v, F('sans_b', 40), col)
        if sub:
            ctext(d, x + sw / 2, 458, sub, F('sans_r', 20), MUTED)

    tenkai = {'fast': '逃げ馬が多くハイペース想定 → 差し・追込が有利',
              'slow': '逃げ馬が少なくスロー想定 → 前に行く馬が有利',
              'base': '平均ペース想定 → 脚質の有利不利は小さめ'}[race['pace']]
    fs = F('sans_r', 26)
    ctext(d, W / 2, 508, fit_text(d, tenkai, fs, W - PAD * 2), fs, SILVER)
    tb = race.get('trackBias')
    if tb:
        note2 = f"今日の{race['surf']}の傾向：{tb['text']}（終わった{tb['races']}レースから）"
    elif race['going'] != '良':
        note2 = '道悪：血統・馬格・実績から道悪適性を加点'
    else:
        note2 = f"ペース根拠：{race['pace_from']}"
    ctext(d, W / 2, 546, fit_text(d, note2, F('sans_r', 22), W - PAD * 2), F('sans_r', 22), MUTED)

    # ── 表 ──
    y = 600
    d.rectangle((PAD, y, W - PAD, y + 56), fill=(24, 34, 62))
    fh = F('sans_b', 24)
    COLS = {'印': 74, '馬番': 130, '馬名': 330, '評価': 590, 'スコア': 680, '勝率': 800, 'オッズ': 905, '期待値': 1000}
    for k, cx in COLS.items():
        if k == '馬名':
            d.text((200, y + 13), k, font=fh, fill=GOLD)
        else:
            ctext(d, cx, y + 13, k, fh, GOLD)
    y += 64

    medal = [(92, 70, 26), (70, 74, 88), (84, 56, 36)]
    for i, h in enumerate(arr):
        y0 = y + i * row_h
        if i < 3:
            d.rounded_rectangle((PAD, y0 + 2, W - PAD, y0 + row_h - 4), radius=10, fill=medal[i])
            d.rounded_rectangle((PAD, y0 + 2, W - PAD, y0 + row_h - 4), radius=10, outline=GOLD_D, width=1)
        elif i % 2 == 0:
            d.rectangle((PAD, y0 + 2, W - PAD, y0 + row_h - 4), fill=(14, 21, 40))
        cy = y0 + row_h / 2
        # 印
        if i < len(MARKS):
            ctext(d, COLS['印'], cy - 24, MARKS[i], F('sans_b', 38), GOLD if i < 3 else SILVER)
        # 馬番（枠色の丸）
        bg, fg = WAKU.get(h.get('w') or 0, ((245, 245, 245), (20, 20, 20)))
        d.ellipse((COLS['馬番'] - 26, cy - 26, COLS['馬番'] + 26, cy + 26), fill=bg, outline=GOLD_D, width=2)
        ctext(d, COLS['馬番'], cy - 18, str(h['n']), F('sans_b', 28), fg)
        # 馬名＋タグ＋根拠
        fn = F('sans_b', 32)
        nm = fit_text(d, h['name'], fn, 300)
        d.text((180, y0 + 10), nm, font=fn, fill=SILVER)
        tx = 180 + tw(d, nm, fn) + 10
        for tag, col in (('穴', (235, 90, 90)) if h['anaFlag'] else (None, None),
                         ('騎', (90, 170, 240)) if h['jb'] else (None, None),
                         ('初', (38, 160, 80)) if h['isNew'] else (None, None)):
            if tag and tx < 540:
                d.rounded_rectangle((tx, y0 + 16, tx + 34, y0 + 48), radius=6, fill=col)
                ctext(d, tx + 17, y0 + 17, tag, F('sans_b', 22), (255, 255, 255))
                tx += 40
        reason = '／'.join(([h['rivalNotes'][0]] if h['rivalNotes'] else []) + h['why'])
        sub = f"{h['jockey']}　{reason}"
        d.text((180, y0 + 52), fit_text(d, sub, F('sans_r', 20), 380), font=F('sans_r', 20), fill=MUTED)
        # 評価
        rc = RANK_C[h['rank']]
        d.rounded_rectangle((COLS['評価'] - 24, cy - 24, COLS['評価'] + 24, cy + 24), radius=8, fill=rc)
        ctext(d, COLS['評価'], cy - 19, h['rank'], F('sans_b', 30), (255, 255, 255))
        fv = F('sans_b', 28)
        ctext(d, COLS['スコア'], cy - 18, f"{h['a']:.3f}", fv, SILVER)
        ctext(d, COLS['勝率'], cy - 18, f"{h['p'] * 100:.1f}%", fv, GOLD if i < 3 else SILVER)
        ctext(d, COLS['オッズ'], cy - 18, f"{h['o']}" if h['o'] else '―', fv, SILVER)
        if h['ev']:
            ctext(d, COLS['期待値'], cy - 18, f"{h['ev']:.2f}", fv, (120, 220, 140) if h['ev'] >= 1.15 else SILVER)
        else:
            ctext(d, COLS['期待値'], cy - 18, '―', fv, MUTED)
    y += row_h * len(arr) + 30

    # ── 買い目 ──
    box_h = 80 + bet_h
    d.rounded_rectangle((PAD, y, W - PAD, y + box_h), radius=16, fill=(14, 22, 42))
    gold_frame(d, (PAD, y, W - PAD, y + box_h), r=16)
    ctext(d, W / 2, y + 16, '― おすすめ買い目 ―', F('serif_b', 36), GOLD)
    LC = {'本線': (200, 160, 70), '妙味': (38, 160, 80), '穴': (214, 60, 60), '高目': (150, 80, 200)}
    fl = F('sans_b', 26)
    yy = y + 78
    for lab, lines in bet_rows:
        d.rounded_rectangle((PAD + 24, yy, PAD + 124, yy + 44), radius=22, fill=LC.get(lab, GOLD_D))
        ctext(d, PAD + 74, yy + 6, lab, fl, (255, 255, 255))
        for k, ln in enumerate(lines):
            d.text((PAD + 144, yy + 5 + k * 40), ln, font=F('sans_b', 28), fill=SILVER)
        yy += 22 + 40 * len(lines)
    y += box_h + 24

    ctext(d, W / 2, y, 'スコア×1.00基準　S≥1.08 ／ A≥1.03 ／ B≥1.00 ／ C≥0.96 ／ D', F('sans_r', 20), MUTED)
    ctext(d, W / 2, y + 30, '※AIシミュレーションの参考値です。オッズは取得時点のもの', F('sans_r', 20), MUTED)
    return img


def short_date(d):
    return f"{int(d[5:7])}/{int(d[8:10])}"


def make_reasons(race, h):
    """アプリの「根拠」と同じ見出しで、1頭分の根拠を作る"""
    R = []
    R.append(('総合評価', f"ランク{h['rank']}・スコア×{h['a']:.3f}（勝率{h['p'] * 100:.1f}%）。"
              f"過去レース「{LBL['lv'][h['lv'] - 1]}」／近走「{LBL['form'][h['form'] - 1]}」／"
              f"適性「{LBL['fit'][h['fit'] - 1]}」／騎手・条件「{LBL['jk'][h['jk'] - 1]}」"))

    cls_now = CLS[race['clsIdx']][0]
    if h['isNew']:
        R.append(('過去レースのレベル', f"新馬のため実績なし。父{h['sire'] or '不明'}の産駒レベルから暫定評価"))
    elif h['cl']:
        c = h['cl']
        R.append(('過去レースのレベル', f"直近{len(h['pastCls'])}走のクラスは{'・'.join(h['pastCls'])}。"
                  f"着差も加えた実質レベルは{c['L']:.1f}で、今回（{cls_now}）の基準より{c['gap']:+.1f}"))
    if h['rivalNotes']:
        R.append(('対戦相手のその後', '。'.join(h['rivalNotes']) + f"（+{h['rl']:.3f}）"))

    if h['recent']:
        def one(l):
            res = '勝ち' if l['pos'] == 1 else f"{l['m']:.1f}秒差"
            return f"{short_date(l['d'])}{l['p']}{l['r']}{l['pos']}着({res})"
        rs = '、'.join(one(l) for l in h['recent'])
        R.append(('近走', rs))

    if h['sameN']:
        fit_t = (f"全成績のうち今回と同じ{race['surf']}{race['dist']}m前後は{h['sameN']}走で3着以内{h['sameT3']}回"
                 "（新しいレースほど重視）")
    else:
        fit_t = "同じ距離・コースの経験は直近5走になし"
    if h['distSum'] > 0:
        fit_t += f"。距離実績で加点（+{min(h['distSum'], 2.5) * 0.012:.3f}）"
    R.append(('コース・距離適性', fit_t))

    st = h['st'] if h['st'] != '?' else '不明'
    pa = h['paceAdj']
    eff = f"有利（+{pa:.3f}）" if pa > 0 else f"不利（{pa:.3f}）" if pa < 0 else "影響なし"
    R.append(('脚質・展開', f"脚質は{st}。{PACE_LABEL[race['pace']]}ペース想定で{eff}"))

    jt = f"{h['jockey']}騎乗・斤量{h['wt']}kg"
    if h['dw'] >= 2:
        jt += f"（出走馬の中では{h['dw']:.1f}kg軽く有利）"
    elif h['dw'] <= -2:
        jt += f"（出走馬の中では{-h['dw']:.1f}kg重く不利）"
    if h['jb']:
        jt += f"。騎手バイアス対象で加点（+{SETTINGS['jb_strength']}）"
    R.append(('騎手・条件', jt))

    if race['going'] != '良' and h['mudM']:
        mt = '、'.join(h['mudParts']) or '馬格から判定'
        R.append(('道悪適性', f"{mt}（{h['mudb']:+.3f}）"))

    if h['anaFlag'] or h['makuri'] > 0:
        parts = []
        if h['distSum'] > 0: parts.append('距離実績あり')
        if h['makuri'] > 0: parts.append('3-4角で一気に位置を上げた（まくり）経験あり')
        R.append(('穴馬チェック', ('【穴】' if h['anaFlag'] else '') + '、'.join(parts)))

    # ③ タイム・上がり
    if h.get('si') is not None:
        R.append(('タイム', f"近走の簡易タイム指数{h['si']:+.0f}（メンバー内の評価「{['低い','やや低い','標準','やや高い','高い'][h['siRate'] - 1]}」）"
                  f"{sgn(h['bTime'])}"))
    if h.get('agRank'):
        R.append(('上がり', f"近走の上がり3F（距離補正）はメンバー中{h['agRank']}番目の速さ{sgn(h['bAgari'])}"))
    # ④ 枠順・当日の馬場
    if abs(h.get('bDraw', 0)) >= 0.002:
        R.append(('枠順', f"{h['n']}番。{'内' if race['drawBias'] > 0 else '外'}枠が有利なコースで"
                  f"{'有利' if h['bDraw'] > 0 else '不利'}{sgn(h['bDraw'])}"))
    tb = race.get('trackBias')
    if tb and abs(h.get('bTrack', 0)) >= 0.002:
        R.append(('当日の馬場', f"今日の{race['surf']}は{tb['text']}（{tb['races']}レース）。"
                  f"この馬には{'追い風' if h['bTrack'] > 0 else '向かい風'}{sgn(h['bTrack'])}"))
    # ⑤ 騎手データ・状態
    js = h.get('jkStats')
    jt = []
    if js:
        jt.append(f"{h['jockey']}の{js['span']}成績：勝率{js['win'] * 100:.0f}%・複勝率{js['fuku'] * 100:.0f}%（{js['rides']}騎乗）")
    if h.get('change'):
        jt.append(f"前走{h['prevJockey']}から乗り替わり")
    if jt:
        R.append(('騎手データ', '。'.join(jt) + sgn(h.get('bJockey', 0))))
    if h.get('condNotes'):
        R.append(('状態', '、'.join(h['condNotes']) + sgn(h.get('bCond', 0))))
    if h.get('careerNotes'):
        R.append(('全成績から', '、'.join(h['careerNotes']) + sgn(h.get('bCareer', 0))))

    R.append(('血統・厩舎', f"父{h['sire'] or '不明'}" + (f"・母父{h['damsire']}" if h.get('damsire') else '')))
    return R


def sgn(v):
    return f"（{v:+.3f}）" if abs(v) >= 0.001 else ''


SHORT_HEAD = {'総合評価': '総合評価', '過去レースのレベル': 'レース格', '対戦相手のその後': '対戦相手',
              '近走': '近走', 'コース・距離適性': '適性', '脚質・展開': '展開', '騎手・条件': '騎手・斤量',
              '道悪適性': '道悪', '穴馬チェック': '穴馬', '血統・厩舎': '血統', 'タイム': 'タイム',
              '上がり': '上がり', '枠順': '枠順', '当日の馬場': '当日馬場', '騎手データ': '騎手成績', '状態': '状態', '全成績から': '全成績'}


_cw_cache = {}


def char_w(f, ch):
    """1文字の幅を覚えておく（毎回測り直さない）"""
    k = (id(f), ch)
    if k not in _cw_cache:
        _cw_cache[k] = f.getlength(ch)
    return _cw_cache[k]


def wrap(d, text, f, maxw):
    lines, cur, w = [], '', 0.0
    for ch in text:
        cw = char_w(f, ch)
        # 行頭に「）」「、」「。」などが来ないよう、前の行にくっつける
        if w + cw > maxw and cur and ch not in '）)」、。・':
            lines.append(cur)
            cur, w = ch, cw
        else:
            cur += ch
            w += cw
    if cur:
        lines.append(cur)
    return lines


def render_reasons_images(race, arr):
    """根拠の画像。長くなりすぎないよう ◎○▲△ と それ以降 の2枚に分ける"""
    targets = [(i, h) for i, h in enumerate(arr[:7])] + [(99, h) for h in arr[7:] if h['anaFlag']]
    parts = [targets[:4], targets[4:]]
    return [render_reasons_image(race, p, k + 1, len([x for x in parts if x])) for k, p in enumerate(parts) if p]


def render_reasons_image(race, targets, page, pages):
    W, PAD = 1080, 36
    fh, fb, fn = F('sans_b', 24), F('sans_r', 24), F('sans_b', 34)
    head_w = 165
    tmp = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    cards = []
    for i, h in targets:
        rows = []
        for head, body in make_reasons(race, h):
            rows.append((head, wrap(tmp, body, fb, W - PAD * 2 - 60 - head_w)))
        height = 86 + sum(len(b) * 36 + 12 for _, b in rows) + 16
        cards.append((i, h, rows, height))
    H = 200 + sum(c[3] + 20 for c in cards) + 70

    img = gradient_bg(W, H)
    d = ImageDraw.Draw(img)
    gold_frame(d, (14, 14, W - 14, H - 14), r=22, w=3)
    ctext(d, W / 2, 40, f'シェイクユアハート ／ 根拠 {page}/{pages}', F('serif_b', 44), GOLD)
    title = f"{race['venue']}{race['R']}R {race['name']}"
    ctext(d, W / 2, 106, fit_text(d, title, F('sans_b', 34), W - PAD * 2), F('sans_b', 34), SILVER)
    d.line((PAD + 120, 166, W - PAD - 120, 166), fill=GOLD_D, width=2)

    y = 190
    for i, h, rows, height in cards:
        d.rounded_rectangle((PAD, y, W - PAD, y + height), radius=16, fill=(14, 22, 42))
        gold_frame(d, (PAD, y, W - PAD, y + height), r=16)
        mark = MARKS[i] if i < len(MARKS) else '穴'
        d.text((PAD + 22, y + 16), mark, font=F('sans_b', 40), fill=GOLD)
        bg, fg = WAKU.get(h.get('w') or 0, ((245, 245, 245), (20, 20, 20)))
        d.ellipse((PAD + 80, y + 18, PAD + 128, y + 66), fill=bg, outline=GOLD_D, width=2)
        ctext(d, PAD + 104, y + 24, str(h['n']), F('sans_b', 26), fg)
        d.text((PAD + 144, y + 18), h['name'], font=fn, fill=SILVER)
        rc = RANK_C[h['rank']]
        rx = W - PAD - 70
        d.rounded_rectangle((rx, y + 20, rx + 46, y + 64), radius=8, fill=rc)
        ctext(d, rx + 23, y + 23, h['rank'], F('sans_b', 28), (255, 255, 255))
        o = f"{h['o']}倍" if h['o'] else ''
        info = f"勝率{h['p'] * 100:.1f}%  {o}"
        d.text((rx - 16 - tw(d, info, F('sans_r', 22)), y + 30), info, font=F('sans_r', 22), fill=MUTED)
        yy = y + 86
        for head, body in rows:
            d.text((PAD + 30, yy), SHORT_HEAD.get(head, head), font=fh, fill=GOLD)
            for j, ln in enumerate(body):
                d.text((PAD + 30 + head_w, yy + j * 36), ln, font=fb, fill=SILVER)
            yy += len(body) * 36 + 12
        y += height + 20
    ctext(d, W / 2, y + 10, '※根拠は過去5走とnetkeibaの出馬表から自動で作成しています', F('sans_r', 20), MUTED)
    return img


def save_image(img):
    """画像を保存して、ファイル名を返す（1日より古い画像は消す）"""
    os.makedirs(IMG_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(IMG_DIR, '*')):
        try:
            if time.time() - os.path.getmtime(f) > 86400:
                os.remove(f)
        except Exception:
            pass
    key = uuid.uuid4().hex
    img.save(os.path.join(IMG_DIR, key + '.png'), compress_level=1)
    pv = img.copy()
    pv.thumbnail((540, 4000), Image.BILINEAR)
    pv.save(os.path.join(IMG_DIR, key + '_pv.jpg'), quality=80)
    return key


@app.route('/img/<path:fname>')
def serve_image(fname):
    return send_from_directory(IMG_DIR, fname)


def public_base_url():
    base = os.environ.get('RENDER_EXTERNAL_URL') or request.url_root
    return base.rstrip('/').replace('http://', 'https://')


def bets_text(race, arr):
    out = [f"{race['venue']}{race['R']}R {race['name']}", "【おすすめ買い目】"] + bets(arr)
    return "\n".join(out)



# ═════════════════════════════════════════
# 成績の記録（Googleスプレッドシート）
# ═════════════════════════════════════════
SHEET_URL = os.environ.get('SHEET_URL', '').strip()
SHEET_TOKEN = os.environ.get('SHEET_TOKEN', '').strip()
PAYOUT_KINDS = {'単勝': 1, '複勝': 1, '枠連': 2, '馬連': 2, 'ワイド': 2, '馬単': 2, '3連複': 3, '3連単': 3}
ORDERED = {'単勝', '複勝', '馬単', '3連単'}   # 着順どおりに判定する券種
_settle_lock = threading.Lock()


def sheet_call(action, **payload):
    """スプレッドシート（Apps Script）に命令を送って、返事を受け取る"""
    if not SHEET_URL:
        return None
    body = json.dumps({'token': SHEET_TOKEN, 'action': action, **payload}, ensure_ascii=False)
    r = requests.post(SHEET_URL, data=body.encode('utf-8'), headers={'Content-Type': 'application/json'},
                      timeout=30, allow_redirects=False)
    if r.status_code in (301, 302, 303) and r.headers.get('Location'):
        r = requests.get(r.headers['Location'], timeout=30)
    r.raise_for_status()
    res = r.json()
    if not res.get('ok'):
        raise RuntimeError(f"スプレッドシートがエラーを返しました: {res.get('error')}")
    return res


def start_dt(race):
    if not race.get('date') or not re.match(r'\d{1,2}:\d{2}$', race.get('time') or ''):
        return None
    hh, mm = map(int, race['time'].split(':'))
    return datetime.datetime(race['date'].year, race['date'].month, race['date'].day, hh, mm)


def jst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def record_prediction(race, arr):
    """発走前の予想だけを記録する（発走後に出した予想は成績に入れない）"""
    if not SHEET_URL:
        return
    st = start_dt(race)
    if st and jst_now() >= st:
        print(f"[sheet] 発走後のため記録しません {race['id']}", flush=True)
        return
    plan = bet_plan(arr)
    marks = ' '.join(f"{MARKS[i]}{h['n']}" for i, h in enumerate(arr[:7]))
    row = {
        'recorded': jst_now().strftime('%Y/%m/%d %H:%M'),
        'race_id': race['id'],
        'start': st.strftime('%Y/%m/%d %H:%M') if st else '',
        'race': f"{race['venue']}{race['R']}R {race['name']}",
        'honmei': f"{arr[0]['n']} {arr[0]['name']}",
        'marks': marks,
        'odds': arr[0]['o'] or '',
        'bets': '\n'.join(f"{b['label']} {b['text']}" for b in plan),
        'plan': json.dumps([{'label': b['label'], 'parts': b['parts']} for b in plan], ensure_ascii=False),
        'site': race.get('base', ''),
    }
    try:
        sheet_call('record', row=row)
        print(f"[sheet] 記録しました {race['id']}", flush=True)
    except Exception as e:
        print(f"[sheet] 記録に失敗: {e}", flush=True)


def parse_result_order(soup):
    """結果ページから {馬番: 着順}。取消・除外は入れない。競走中止は99"""
    table = soup.select_one('table#All_Result_Table') or soup.select_one('table.RaceTable01')
    if not table:
        return {}
    header = table.select_one('tr.Header') or table.find('tr')
    cols = [clean(c.get_text()) for c in header.find_all(['th', 'td'])]
    i_n = next((i for i, c in enumerate(cols) if '馬番' in c), None)
    out = {}
    for tr in table.select('tr.HorseList') or table.find_all('tr')[1:]:
        tds = tr.find_all('td')
        if not tds or i_n is None or i_n >= len(tds):
            continue
        n = to_float(tds[i_n].get_text())
        rk = clean(tds[0].get_text())
        if n and rk not in ('取消', '除外', ''):
            out[int(n)] = int(rk) if rk.isdigit() else 99
    return out


def parse_payouts(soup):
    """払戻金 {券種: {組み合わせ: 円}}"""
    pays = {}
    for tr in soup.select('table[class*="Payout"] tr'):
        th = tr.find('th')
        if not th:
            continue
        kind = norm_digits(clean(th.get_text())).replace('三連', '3連')
        res_td, pay_td = tr.select_one('td.Result'), tr.select_one('td.Payout')
        if kind not in PAYOUT_KINDS or not res_td or not pay_td:
            continue
        nums = [int(x) for x in re.findall(r'\d+', res_td.get_text(' '))]
        yen = [int(x.replace(',', '')) for x in re.findall(r'([\d,]+)円', pay_td.get_text(' '))]
        k = PAYOUT_KINDS[kind]
        if not yen or len(nums) != k * len(yen):
            continue
        d = pays.setdefault(kind, {})
        for i, y in enumerate(yen):
            c = tuple(nums[i * k:(i + 1) * k])
            d[c if kind in ORDERED else tuple(sorted(c))] = y
    return pays


def settle_one(row):
    """1レース分の答え合わせ。結果がまだなら None"""
    site = row.get('site') or 'https://race.netkeiba.com'
    soup = fetch_soup(f"{site}/race/result.html?race_id={row['race_id']}")
    order = parse_result_order(soup)
    if not any(v == 1 for v in order.values()):
        return None
    pays = parse_payouts(soup)
    top3 = [n for n, _ in sorted(((n, r) for n, r in order.items() if r <= 3), key=lambda x: x[1])]
    hm = int(re.match(r'\d+', str(row.get('honmei', '0'))).group()) if re.match(r'\d+', str(row.get('honmei', ''))) else 0
    cost = ret = 0
    detail = {}
    for b in json.loads(row.get('plan') or '[]'):
        for kind, combos in b['parts']:
            if kind not in pays:
                continue
            c = 100 * len(combos)
            got = sum(pays[kind].get(tuple(cb) if kind in ORDERED else tuple(sorted(cb)), 0) for cb in combos)
            key = f"{b['label']} {kind}"
            dc, dr = detail.get(key, [0, 0])
            detail[key] = [dc + c, dr + got]
            cost += c
            ret += got
    return {'race_id': row['race_id'], 'result': '-'.join(map(str, top3)), 'honmei_pos': order.get(hm, ''),
            'cost': cost, 'ret': ret, 'detail': json.dumps(detail, ensure_ascii=False)}


def settle_pending(limit=15):
    """まだ結果が入っていない予想の答え合わせをして、スプレッドシートに書き込む"""
    if not SHEET_URL or not _settle_lock.acquire(blocking=False):
        return 0
    try:
        rows = sheet_call('pending').get('rows', [])
        now = jst_now()
        done = []
        for row in rows:
            try:
                st = datetime.datetime.strptime(row['start'], '%Y/%m/%d %H:%M') if row.get('start') else None
            except ValueError:
                st = None
            if st and now < st + datetime.timedelta(minutes=15):
                continue   # まだ結果が出ていない
            res = _safe(settle_one, row)
            if res:
                done.append(res)
            if len(done) >= limit:
                break
        if done:
            sheet_call('settle', results=done)
        print(f"[sheet] 答え合わせ {len(done)}件", flush=True)
        return len(done)
    except Exception as e:
        print(f"[sheet] 答え合わせに失敗: {e}", flush=True)
        return 0
    finally:
        _settle_lock.release()


def stats_text():
    """「成績」と送ったときの返事"""
    if not SHEET_URL:
        return "成績の記録はまだ設定されていません（SHEET_URLが未設定です）。"
    settle_pending(limit=30)
    rows = [r for r in sheet_call('all').get('rows', []) if r.get('status') == '確定']
    if not rows:
        return "📈 まだ結果の出た予想がありません。発走前に予想を出すと、レース後に自動で記録されます。"
    n = len(rows)
    pos = [int(r['honmei_pos']) for r in rows if str(r.get('honmei_pos', '')).isdigit()]
    cost = sum(float(r.get('cost') or 0) for r in rows)
    ret = sum(float(r.get('ret') or 0) for r in rows)
    by = {}
    for r in rows:
        try:
            for k, (c, g) in json.loads(r.get('detail') or '{}').items():
                b = by.setdefault(k, [0, 0, 0, 0])
                b[0] += 1; b[1] += g > 0; b[2] += c; b[3] += g
        except Exception:
            pass
    pc = lambda a, b: f"{a / b * 100:.0f}%" if b else '―'
    lines = [f"📈 シェイクユアハート 成績（{n}レース）", "",
             f"◎の勝率 {pc(sum(1 for p in pos if p == 1), n)} ／ 複勝率 {pc(sum(1 for p in pos if p <= 3), n)}",
             f"買い目すべて（各100円）", f"　投資 {cost:,.0f}円 → 払戻 {ret:,.0f}円（回収率 {pc(ret, cost)}）", "",
             "【買い目別】"]
    for k, (races, hit, c, g) in by.items():
        lines.append(f"{k}：的中 {pc(hit, races)} ／ 回収 {pc(g, c)}")
    lines += ["", "直近の結果"]
    for r in rows[-5:][::-1]:
        lines.append(f"{r['race']}　◎{r.get('honmei_pos', '?')}着　{float(r.get('ret') or 0) - float(r.get('cost') or 0):+,.0f}円")
    return "\n".join(lines)


# ═════════════════════════════════════════
# LINE
# ═════════════════════════════════════════
@app.route("/")
def health():
    return "ok"


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """LINEにはすぐ「受け取った」と返し、予想づくりは裏で行う"""
    text = event.message.text.strip()
    print(f'[recv] {text[:80]}', flush=True)
    base = public_base_url()
    threading.Thread(target=process_message, args=(event.reply_token, text, base), daemon=True).start()


def build_messages(text, base):
    """予想を作って、LINEに送るメッセージのリストと、返信後にやる処理を返す"""
    if text in ('成績', '成績確認'):
        return [TextSendMessage(text=stats_text())], None
    msgs, race_arr = _build_prediction(text, base)
    after = None
    if race_arr:
        def after():
            record_prediction(*race_arr)
            settle_pending(limit=10)
    return msgs, after


def _build_prediction(text, base):
    if not ("netkeiba.com" in text or re.fullmatch(r'\d{12}', text)):
        return [TextSendMessage(text="netkeibaの出馬表のURL（またはレースID12桁）を送ってください！\n"
                                     "「成績」と送ると、これまでの予想の成績を確認できます。")], None
    try:
        result, err = analyze(text)
    except requests.HTTPError as e:
        result, err = None, f"netkeibaへのアクセスに失敗しました（{e.response.status_code}）。"
    except Exception as e:
        traceback.print_exc()
        result, err = None, f"予想中にエラーが発生しました。\n詳細: {e}"
    if err:
        return [TextSendMessage(text=err)], None

    race, arr = result
    try:
        # 画像で送る ＋ 買い目だけ文字でも送る（馬券を買うときにコピーしやすいように）
        if not fonts_ok(20):
            raise RuntimeError('サーバー起動直後で画像用の文字データを準備中です。1〜2分後にもう一度送ってください')
        messages = []
        with _render_lock:  # 画像づくりは1件ずつ（フォントの同時使用でサーバーが落ちるのを防ぐ）
            imgs = [render_image(race, arr)] + render_reasons_images(race, arr)
            keys = [save_image(im) for im in imgs]
        for key in keys:
            messages.append(ImageSendMessage(original_content_url=f"{base}/img/{key}.png",
                                             preview_image_url=f"{base}/img/{key}_pv.jpg"))
        messages.append(TextSendMessage(text=bets_text(race, arr)))
        return messages, (race, arr)
    except Exception as e:
        # 画像づくりに失敗したら、今までどおり文章で送る（原因も添える）
        traceback.print_exc()
        return [TextSendMessage(text=f"⚠画像を作れませんでした（{type(e).__name__}: {e}）\n\n"
                                     + format_reply(race, arr))], (race, arr)


def process_message(reply_token, text, base):
    t0 = time.time()
    after = None
    try:
        messages, after = build_messages(text, base)
    except Exception as e:
        traceback.print_exc()
        messages = [TextSendMessage(text=f"予想中にエラーが発生しました。\n詳細: {e}")]
    print(f'[time] 予想づくり {time.time() - t0:.1f}秒', flush=True)
    try:
        line_bot_api.reply_message(reply_token, messages)
        print(f'[reply] 送信OK（{len(messages)}件・{time.time() - t0:.1f}秒）', flush=True)
    except Exception as e:
        print(f'[reply] 返信に失敗: {e}', flush=True)
    if after:   # 返信を送ったあとで、成績の記録と答え合わせ（予想の速さには影響しない）
        _safe(after)


@app.route('/test')
def test_page():
    """ブラウザで動作確認するためのページ。
    https://〇〇.onrender.com/test?id=レースID12桁      → 予想画像を表示
    https://〇〇.onrender.com/test?id=レースID12桁&t=1  → 文章版を表示"""
    rid = request.args.get('id', '')
    try:
        result, err = analyze(rid)
        if err:
            return err, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        if request.args.get('t'):
            return format_reply(*result), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        if not fonts_ok(20):
            return '文字データを準備中です。1〜2分後に開き直してください', 200, {'Content-Type': 'text/plain; charset=utf-8'}
        with _render_lock:
            key = save_image(render_image(*result))
        return send_from_directory(IMG_DIR, key + '.png')
    except Exception:
        return traceback.format_exc(), 500, {'Content-Type': 'text/plain; charset=utf-8'}


threading.Thread(target=preload_fonts, daemon=True).start()


if __name__ == "__main__":
    # 動作確認： python main.py <URL>        → LINEに返す文章を表示
    #           python main.py <URL> debug  → 読み取った出走馬・過去走も表示
    if len(sys.argv) > 1:
        if len(sys.argv) > 2 and sys.argv[2] == 'debug':
            race_id, base = parse_input(sys.argv[1])
            soup = fetch_soup(f"{base}/race/shutuba_past.html?race_id={race_id}")
            print(parse_race_info(soup, race_id, base))
            for h in parse_shutuba_past(soup):
                print(h['n'], h['name'], h['jockey'], h['wt'], h['st'], h['sire'], len(h['lines']), '走')
                for l in h['lines']:
                    print('   ', l['d'], l['p'], l['r'], l['dist'], l['cond'], l['pos'], '着', l['m'])
        print(generate_prediction(sys.argv[1]))
        res, err = analyze(sys.argv[1])
        if res:
            render_image(*res).save('preview.png')
            for k, im in enumerate(render_reasons_images(*res), 1):
                im.save(f'preview_reasons{k}.png')
            print('画像を preview.png / preview_reasons1.png などに保存しました')
    else:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
