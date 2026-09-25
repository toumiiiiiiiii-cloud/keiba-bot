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

    fm = re.search(r'(\d+)頭\s*(\d+)番\s*(\d*)人?\s*(\S+)\s+(\d{2}(?:\.\d)?)', txt)
    pm = re.search(r'([\d]+(?:-[\d]+)+|\d+)\s*\(([\d.]+)\)\s*(\d{3})\s*\(([+\-]?\d+)\)', txt)
    mm = re.findall(r'\(([+\-]?\d+\.\d)\)', txt)
    if not mm:
        return None
    win_a = [x for x in td.find_all('a') if HORSE_HREF.search(x.get('href', ''))]
    return {
        'd': f'{m.group(1)}.{m.group(2)}.{m.group(3)}', 'p': m.group(4), 'pos': pos, 'r': rname,
        'dist': dist, 'cond': cond,
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

        jk_td = tr.select_one('td.Jockey')
        jockey, wt = '', 0.0
        if jk_td:
            ja = jk_td.find('a', href=re.compile(r'/jockey/'))
            jockey = clean(ja.get_text()) if ja else ''
            wm = re.search(r'(\d{2}\.\d)', jk_td.get_text(' '))
            wt = float(wm.group(1)) if wm else 0.0
        if not jockey:
            ja = next((x for x in tr.find_all('a', href=re.compile(r'/jockey/'))
                       if not x.find_parent('td', class_='Past')), None)
            jockey = clean(ja.get_text()) if ja else '不明'
        jockey = re.sub(r'^[▲△☆★◇]+', '', jockey) or '不明'

        past_tds = tr.select('td.Past') or [td for td in tds if re.search(r'\d{4}\.\d{2}\.\d{2}', td.get_text())]
        lines = [x for x in (parse_past_cell(td) for td in past_tds) if x]

        cancelled = 'Cancel' in tr.get('class', []) or bool(re.search(r'取消|除外', tr.get_text()[:200]))
        horses.append({'w': waku, 'n': umaban, 'name': name, 'sire': clean(sire),
                       'dam': f'{clean(dam)}({damsire})' if damsire else clean(dam),
                       'damsire': damsire, 'st': st, 'jockey': jockey, 'wt': wt,
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

        same = [l for l in ls if l['dist'][:1] == race['surf'] and abs(dist_num(l['dist']) - race['dist']) <= 100]
        fit = 2
        if same:
            t3 = sum(1 for l in same if l['pos'] <= 3)
            avg = sum(mv(l) for l in same) / len(same)
            fit = (5 if t3 >= 2 else 4) if (t3 >= 1 and avg <= 1.0) else 3 if avg <= 1.5 else 2
        elif ls and all(l['dist'][:1] != race['surf'] for l in ls):
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
            why.append(f"同条件{len(same)}走で3着内{t3}回" if same else "同条件の経験なし")

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
    wet = [l for l in h['lines'] if l['pos'] > 0 and l['dist'].startswith('芝') and re.search(r'[稍重不]', l['cond'])]
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
    wts = [1, .9, .8, .7, .6]
    rows = [l for l in h['lines'] if l['pos'] > 0 and is_flat(l) and l['dist'][:1] == race['surf']
            and abs(dist_num(l['dist']) - race['dist']) <= tol]
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
    for l in [x for x in h['lines'] if x['pos'] > 0]:
        shared = class_of_text(l['r'], l['p'])
        for o in keys.get(line_key(l), []):
            if o['name'] == h['name'] or o['pos'] <= 0 or o['name'] in seen:
                continue
            best = None
            for f in o['horse']['lines']:
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
    return '|'.join(str(l.get(k)) for k in ('d', 'p', 'dist', 'r', 'f'))


def ranked(race, horses, odds):
    going, pace = race['going'], race['pace']
    shr = 1 - (1 - CLASS_SHRINK.get(going, 1)) * SETTINGS['mudw']
    adj = PACE.get(pace, {})
    keys = {}
    for h in horses:
        for l in h['lines']:
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
        h['a'] = round(1 + (h['s'] - 1) * shr + adj.get(h['st'], 0) + jb + mb + ana + rl, 3)
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


def bets(arr):
    n = [h['n'] for h in arr]
    out = []
    if len(n) >= 3:
        out.append(f"本線 馬連 {n[0]}-{n[1]}, {n[0]}-{n[2]}")
        out.append(f"本線 ワイド {n[0]}-{n[1]}")
    if len(n) >= 6:
        out.append(f"本線 3連複 {n[0]} - {n[1]},{n[2]} - {','.join(map(str, n[1:6]))}")
    evs = [h for h in arr[:8] if h['ev'] and h['ev'] >= 1.15]
    if evs:
        b = max(evs, key=lambda h: h['ev'])
        out.append(f"妙味 単複 {b['n']}（期待値{b['ev']:.2f}）")
    anas = [h for h in arr if h['anaFlag']][:2]
    if anas:
        out.append("穴 ワイド " + ', '.join(f"{n[0]}-{h['n']}" for h in anas))
    t = trifecta_high(arr)
    if t:
        out.append(t)
    return out


def trifecta_high(arr):
    """高目狙いの3連単フォーメーション。
    1着：◎＋期待値が一番高い馬 ／ 2着：1着候補＋○▲＋穴馬・高期待値の馬 ／ 3着：2着の馬＋△△△☆"""
    if len(arr) < 5:
        return None
    top8 = arr[:8]
    value = sorted([h for h in top8[1:] if h['ev'] and h['ev'] >= 1.3], key=lambda h: -h['ev'])
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

    pts = sum(1 for a in first for b in second for c in third if len({a['n'], b['n'], c['n']}) == 3)
    j = lambda hs: ','.join(str(h['n']) for h in hs)
    return f"高目 3連単 1着{j(first)} → 2着{j(second)} → 3着{j(third)}（{pts}点）"


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

    auto_heuristics(race, horses)
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
    note2 = '道悪：血統・馬格・実績から道悪適性を加点' if race['going'] != '良' else f"ペース根拠：{race['pace_from']}"
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
        fit_t = f"今回と同じ{race['surf']}{race['dist']}m前後で{h['sameN']}走して3着以内{h['sameT3']}回"
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

    R.append(('血統・厩舎', f"父{h['sire'] or '不明'}" + (f"・母父{h['damsire']}" if h.get('damsire') else '')))
    return R


SHORT_HEAD = {'総合評価': '総合評価', '過去レースのレベル': 'レース格', '対戦相手のその後': '対戦相手',
              '近走': '近走', 'コース・距離適性': '適性', '脚質・展開': '展開', '騎手・条件': '騎手・斤量',
              '道悪適性': '道悪', '穴馬チェック': '穴馬', '血統・厩舎': '血統'}


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
    """予想を作って、LINEに送るメッセージのリストを返す"""
    if not ("netkeiba.com" in text or re.fullmatch(r'\d{12}', text)):
        return [TextSendMessage(text="netkeibaの出馬表のURL（またはレースID12桁）を送ってください！")]
    try:
        result, err = analyze(text)
    except requests.HTTPError as e:
        result, err = None, f"netkeibaへのアクセスに失敗しました（{e.response.status_code}）。"
    except Exception as e:
        traceback.print_exc()
        result, err = None, f"予想中にエラーが発生しました。\n詳細: {e}"
    if err:
        return [TextSendMessage(text=err)]

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
        return messages
    except Exception as e:
        # 画像づくりに失敗したら、今までどおり文章で送る（原因も添える）
        traceback.print_exc()
        return [TextSendMessage(text=f"⚠画像を作れませんでした（{type(e).__name__}: {e}）\n\n"
                                     + format_reply(race, arr))]


def process_message(reply_token, text, base):
    t0 = time.time()
    try:
        messages = build_messages(text, base)
    except Exception as e:
        traceback.print_exc()
        messages = [TextSendMessage(text=f"予想中にエラーが発生しました。\n詳細: {e}")]
    print(f'[time] 予想づくり {time.time() - t0:.1f}秒', flush=True)
    try:
        line_bot_api.reply_message(reply_token, messages)
        print(f'[reply] 送信OK（{len(messages)}件・{time.time() - t0:.1f}秒）', flush=True)
    except Exception as e:
        print(f'[reply] 返信に失敗: {e}', flush=True)


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
