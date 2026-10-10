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
from concurrent.futures import ThreadPoolExecutor, wait
from collections import defaultdict
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, VideoSendMessage
import glob
import time
import uuid
import random
import threading
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
HORSE_HREF = re.compile(r'/horse/[0-9a-z]{10}')

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
# 地方競馬のクラス（中央の格に合わせた目安の値）。番号9以降
CLS += [("地方C3", 1.2), ("地方C2", 1.4), ("地方C1", 1.7), ("地方B3", 2.0), ("地方B2", 2.2), ("地方B1", 2.4),
        ("地方A2", 2.7), ("地方A1", 3.0), ("地方OP", 3.2), ("地方重賞", 3.6), ("地方S2", 4.0), ("地方S1", 4.4),
        ("地方2歳", 1.1), ("地方A3", 2.6), ("地方A4", 2.5), ("地方B4", 1.9), ("地方C4", 1.1), ("地方D", 1.0)]
NAR_CLS = {k: i for i, (k, _) in enumerate(CLS) if k.startswith('地方') and k != '地方・その他'}


def nar_class(t):
    """地方競馬のレース名・条件からクラスを判定（高いクラスから順に見る）"""
    t = norm_digits(t).translate(str.maketrans('ＡＢＣＳＩＯＰ', 'ABCSIOP'))
    t = re.sub(r'[ー－−‐―]', '-', t)
    if re.search(r'S(?:1|Ⅰ|I)(?![IⅠ0-9])', t): return NAR_CLS['地方S1']
    if re.search(r'S(?:2|Ⅱ|II)(?![IⅠ0-9])', t): return NAR_CLS['地方S2']
    if re.search(r'S(?:3|Ⅲ|III)|重賞', t): return NAR_CLS['地方重賞']
    if re.search(r'オープン|OP|特別選抜', t): return NAR_CLS['地方OP']
    for k in ('A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4', 'C1', 'C2', 'C3', 'C4'):
        if re.search(r'(?<![A-Z])' + k[0] + r'\s*-?\s*' + k[1], t):
            return NAR_CLS['地方' + k]
    if re.search(r'(^|[^A-Z])D(?![A-Z])', t): return NAR_CLS['地方D']
    for k in ('A', 'B', 'C'):
        if re.search(r'(^|[^A-Z])' + k + r'(?![A-Z])', t):
            return NAR_CLS['地方' + k + '2']
    if re.search(r'2歳|新馬|未勝利|認定', t): return NAR_CLS['地方2歳']
    return 8

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
NAR_PLACE = {"30": "門別", "35": "盛岡", "36": "水沢", "42": "浦和", "43": "船橋", "44": "大井", "45": "川崎",
             "46": "金沢", "47": "笠松", "48": "名古屋", "50": "園田", "51": "姫路", "54": "高知", "55": "佐賀",
             "65": "帯広"}
NAR_VENUES = set(NAR_PLACE.values())


def is_nar_id(rid):
    try:
        return int(str(rid)[4:6]) > 10
    except ValueError:
        return False


def site_of(rid):
    return 'https://nar.netkeiba.com' if is_nar_id(rid) else 'https://race.netkeiba.com'


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
SESSION.mount('https://', requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))
BG = ThreadPoolExecutor(max_workers=8)   # 予想と同時に進める補助の取得用

# ── netkeibaへのアクセスの交通整理 ──
#   ・サイト（ホスト）ごとに同時アクセス数と間隔を制限（弾かれないように）
#   ・タイムアウト・接続エラー・429・5xx は、間を空けて最大3回までやり直す
#   ・作り置き（GitHub Actions）では SYH_BATCH=1 にして、さらにゆっくり取りに行く
BATCH = bool(os.environ.get('SYH_BATCH'))
HOST_LIMIT = {'db.netkeiba.com': 2 if BATCH else 4}          # それ以外のホストは下の既定値
HOST_GAP = 1.0 if BATCH else 0.15                              # 同じホストへの最低間隔（秒）
_host_sem, _host_last, _host_lock = {}, {}, threading.Lock()
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


def _host_of(url):
    m = re.match(r'https?://([^/]+)', url)
    return m.group(1) if m else ''


def http_get(url, timeout=15, retries=3, **kw):
    """netkeibaへのGET。同時数・間隔の制限とやり直し付き。最後まで失敗したら例外"""
    host = _host_of(url)
    with _host_lock:
        sem = _host_sem.setdefault(host, threading.Semaphore(HOST_LIMIT.get(host, 2 if BATCH else 4)))
    last_err = None
    for attempt in range(retries + 1):
        with sem:
            with _host_lock:
                wait_s = _host_last.get(host, 0) + HOST_GAP - time.time()
                _host_last[host] = max(time.time(), _host_last.get(host, 0) + HOST_GAP)
            if wait_s > 0:
                time.sleep(wait_s)
            try:
                res = SESSION.get(url, headers=HEADERS, timeout=timeout, **kw)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                res = None
        if res is not None:
            if res.status_code not in RETRY_STATUS:
                res.raise_for_status()        # 400/403/404 などはやり直しても同じなので、すぐ失敗
                return res
            last_err = requests.HTTPError(f'{res.status_code}', response=res)
            ra = res.headers.get('Retry-After')
            if ra and ra.isdigit():
                time.sleep(min(30, int(ra)))
        if attempt < retries:
            time.sleep((1.5 ** attempt) * (2 if BATCH else 0.6) + random.random() * 0.5)
    print(f"[fetch] 失敗（{retries + 1}回）: {url} {last_err}", flush=True)
    if isinstance(last_err, requests.HTTPError):
        raise last_err
    raise FetchError(str(last_err))


def fetch_soup(url, timeout=15):
    res = http_get(url, timeout=timeout)
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
    if (place or '') in NAR_VENUES: return nar_class_tiered(t, place)   # 地方の「重賞」は地方の物差しで
    if '重賞' in t: return 4
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
def is_abroad_id(rid):
    """海外レースのIDは5桁目が英字（例：2026C8010105＝パリロンシャン）"""
    return len(str(rid)) == 12 and str(rid)[4].isalpha()


def parse_input(text):
    m = (re.search(r'race_id=([0-9A-Za-z]{12})', text) or re.search(r'/race/([0-9A-Za-z]{12})', text)
         or re.search(r'\b(\d{12})\b', text) or re.search(r'\b(\d{4}[A-Z][0-9A-Za-z]{7})\b', text))
    if not m:
        return None, None
    rid = m.group(1)
    if is_abroad_id(rid):
        return rid, 'https://race.netkeiba.com'
    base = 'https://nar.netkeiba.com' if ('nar.' in text or is_nar_id(rid)) else 'https://race.netkeiba.com'
    return rid, base


def going_override(text):
    """URLの後ろに「重」などと書いたら、その馬場で計算する（海外の馬場を自分で指定したいとき用）"""
    rest = re.sub(r'https?://\S+', ' ', text)
    m = re.search(r'(?:^|\s)(良|稍重|稍|重|不良|不)(?:\s|$)', rest)
    return {'稍': '稍重', '不': '不良'}.get(m.group(1), m.group(1)) if m else None


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
    m_est = False
    if not mm:
        # 海外のレースは着差が載っていない。着順から大まかに見積もる（1着0秒、以下1着ごとに0.2秒）
        mm = ['0.0' if pos == 1 else (f'{min(1.6, 0.2 * (pos - 1)):.1f}' if pos > 0 else '9.9')]
        m_est = True
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
        'm': float(mm[-1]), 'mEst': m_est,
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
    cm = re.search(r'[(（]([^)）]*)[)）]', d1t)
    r['courseNote'] = cm.group(1) if cm else ''          # 例「右 外 A」（動画のコース形状に使う）
    tm = re.search(r'(\d{1,2}:\d{2})発走', d1t)
    r['time'] = tm.group(1) if tm else ''
    gm = re.search(r'馬場\s*[:：]\s*(良|稍重?|重|不良?)', d1t)
    g = gm.group(1) if gm else '良'
    r['going'] = {'稍': '稍重', '不': '不良'}.get(g, g)
    r['going_known'] = bool(gm)

    d2 = soup.select_one('.RaceData02')
    r['condText'] = norm_digits(d2.get_text(' ', strip=True)) if d2 else ''
    title_t = soup.title.get_text() if soup.title else ''
    r['titleName'] = re.split(r'\s*(5走表示|9走表示|出馬表|\|)', title_t)[0].strip()
    r['clsIdx'] = class_of_text(r['titleName'] + ' ' + r['name'] + ' ' + r['condText'], '')

    if 'nar.' in base:
        r['venue'] = NAR_PLACE.get(race_id[4:6]) or (re.search(r'\d+回\s*(\S+?)\s*\d+日目', r['condText']) or [None, ''])[1] or ''
        r['clsIdx'] = class_of_text(r['titleName'] + ' ' + r['name'] + ' ' + r['condText'], r['venue'])
        r['banei'] = r['venue'] == '帯広'
    elif is_abroad_id(race_id):
        vm = re.search(r'\d+日\s*(\S+?)\d+R', title_t)
        r['venue'] = vm.group(1) if vm else '海外'
        r['abroad'] = True
    else:
        r['venue'] = JRA_PLACE.get(race_id[4:6], '')
    r['R'] = int(race_id[-2:])

    pm = re.search(r'ペース\s*([HMS])', soup.get_text(' '))
    r['pace'] = {'H': 'fast', 'M': 'base', 'S': 'slow'}[pm.group(1)] if pm else None
    return r


def parse_shutuba_past(soup):
    horses = []
    rows = soup.select('table.Shutuba_Past5_Table tr.HorseList') or soup.select('tr.HorseList') or \
        [tr for tr in soup.find_all('tr') if tr.find('a', href=HORSE_HREF) and re.search(r'\d{4}\.\d{2}\.\d{2}', tr.get_text())]
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
        hm_ = re.search(r'/horse/([0-9a-z]{10})', a.get('href', ''))
        hid = hm_.group(1) if hm_ else None

        tds = tr.find_all('td')
        nums = [int(t) for t in (clean(td.get_text()) for td in tds[:3]) if t.isdigit()]
        waku = nums[0] if len(nums) >= 2 else None
        umaban = nums[1] if len(nums) >= 2 else (nums[0] if nums else None)
        sa_m = re.search(r'(牡|牝|セ|騸)\s*(\d+)', tr.get_text(' '))
        sex, age = (sa_m.group(1), int(sa_m.group(2))) if sa_m else (None, None)

        def txt(sel):
            el = info.select_one(sel)
            return el.get_text(' ', strip=True) if el else ''
        sire = txt('.Horse01')
        dam = txt('.Horse03')
        damsire = txt('.Horse04').strip('()（） ')
        info_text = info.get_text(' ', strip=True)
        if not sire:   # 海外の出馬表などで欄の目印が無いとき：「父 馬名 母 (母父)」の並びから読む
            before, _, after = info_text.partition(a.get_text(' ', strip=True))
            sire = before.strip().split('  ')[-1].strip() if before.strip() else ''
            dm_ = re.match(r'\s*(.+?)\s*[(（]([^)）]*)[)）]', after)
            if dm_:
                dam, damsire = dm_.group(1).strip(), dm_.group(2).strip()
        tm_ = re.search(r'(美浦|栗東|[^\s・()（）\d]{2,6})・\S+', txt('.Horse05') or info_text)
        trainer_area = tm_.group(1) if tm_ else ''
        ta_ = info.find('a', href=re.compile(r'/trainer/'))
        tr_m = re.search(r'/trainer/(?:result/)?(?:recent/)?([0-9a-z]{5})', ta_.get('href', '')) if ta_ else None
        trid = tr_m.group(1) if tr_m else None
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
            jm = re.search(r'/jockey/(?:result/)?(?:recent/)?([0-9a-z]{5})', ja.get('href', '')) if ja else None
            jid = jm.group(1) if jm else None
            wm = re.search(r'(\d{2}\.\d)', jk_td.get_text(' '))
            wt = float(wm.group(1)) if wm else 0.0
        if not jockey:
            ja = next((x for x in tr.find_all('a', href=re.compile(r'/jockey/'))
                       if not x.find_parent('td', class_='Past')), None)
            jockey = clean(ja.get_text()) if ja else '不明'
        if jockey in ('', '不明') and jk_td:   # 地方の5走表示は騎手名がリンクになっていない（例「牡8鹿 山田義 52.0」）
            t_ = re.sub(r'(牡|牝|セ|騸)\s*\d+\S*', ' ', jk_td.get_text(' '))
            t_ = re.sub(r'\d{2}\.\d', ' ', t_)
            cand = [x for x in re.split(r'\s+', t_) if x and not re.fullmatch(r'[▲△☆★◇]+', x)]
            jockey = cand[0] if cand else ''
        jockey = re.sub(r'^[▲△☆★◇]+', '', jockey) or '不明'

        past_tds = tr.select('td.Past') or [td for td in tds if re.search(r'\d{4}\.\d{2}\.\d{2}', td.get_text())]
        if not wt:   # 斤量の欄に目印が無いとき：「牡3鹿 … 56.5」の形の欄から読む
            for td in tds:
                if td in past_tds:
                    continue
                wm = re.search(r'(?:牡|牝|セ)\d+\S*.*?(\d{2}\.\d)', td.get_text(' '))
                if wm:
                    wt = float(wm.group(1))
                    if jk_td is None:
                        jk_td = td
                    break
        lines = [x for x in (parse_past_cell(td) for td in past_tds) if x]

        # ページに出ている単勝オッズ（地方で多い）
        odds_pg = None
        for td in tds:
            if td in past_tds:
                continue
            om = re.search(r'(\d+\.\d)\s*\(\s*\d+\s*人気\s*\)', td.get_text(' '))
            if om:
                odds_pg = float(om.group(1))
                break

        # 当日の馬体重（発表後のみ）。過去走の欄は除いて探す
        bw_now = bw_diff = None
        for td in tds:
            if td in past_tds:
                continue
            bm = re.search(r'(\d{3})\s*(?:kg)?\s*\(\s*([+\-]?\d+)\s*\)', td.get_text(' '))
            if bm:
                bw_now, bw_diff = int(bm.group(1)), int(bm.group(2))
                break

        cancelled = 'Cancel' in tr.get('class', []) or bool(re.search(r'取消|除外', tr.get_text()[:200]))
        horses.append({'w': waku, 'n': umaban, 'name': name, 'sire': re.sub(r'\s+', ' ', sire).strip(),
                       'dam': f'{clean(dam)}({damsire})' if damsire else clean(dam),
                       'damsire': re.sub(r'\s+', ' ', damsire).strip(), 'st': st, 'jockey': jockey, 'wt': wt, 'jid': jid, 'hid': hid, 'area': trainer_area,
                       'weeks': weeks, 'bwNow': bw_now, 'bwDiff': bw_diff, 'oddsPage': odds_pg,
                       'sex': sex, 'age': age, 'gate': waku, 'trid': trid,
                       'lines': lines, 'cancelled': cancelled})
    return horses


def fill_nar_jockeys(race_id, base, horses):
    """地方の5走表示には騎手のリンクが無いので、通常の出馬表ページから騎手名とIDを補う"""
    soup = fetch_soup(f"{base}/race/shutuba.html?race_id={race_id}")
    by_n = {h['n']: h for h in horses}
    for tr in soup.select('tr.HorseList'):
        tds = tr.find_all('td')
        nums = [int(t) for t in (clean(td.get_text()) for td in tds[:3]) if t.isdigit()]
        n = nums[1] if len(nums) >= 2 else (nums[0] if nums else None)
        ja = tr.find('a', href=re.compile(r'/jockey/'))
        h = by_n.get(n)
        if not h or not ja:
            continue
        jm = re.search(r'/jockey/(?:result/)?(?:recent/)?([0-9a-z]{5})', ja.get('href', ''))
        if jm and not h.get('jid'):
            h['jid'] = jm.group(1)
        name = re.sub(r'^[▲△☆★◇]+', '', clean(ja.get_text()))
        if name and h.get('jockey') in ('', '不明'):
            h['jockey'] = name


def _parse_win_odds(js):
    data = js.get('data') if isinstance(js, dict) else None
    if not isinstance(data, dict):
        return {}
    win = (data.get('odds') or {}).get('1') or {}
    out = {}
    for k, v in win.items():
        val = to_float(v[0]) if v else None
        if val:
            out[int(k)] = val
    return out


def fetch_win_odds(race_id, base):
    """単勝オッズ。発売前などで取れなければ空。
    取れないことがあったので、①いつもの取得 ②クッキー無しの新しい接続 の2通りで、間を空けて試す。
    取れなかった理由はログに出す（Renderの Logs で「[odds]」を探す）"""
    site = 'https://nar.netkeiba.com' if 'nar.' in base else 'https://race.netkeiba.com'
    api = 'api_get_nar_odds.html' if 'nar.' in base else 'api_get_jra_odds.html'
    url = f"{site}/api/{api}?race_id={race_id}&type=1&action=update"
    hdr = dict(HEADERS, Referer=f"{site}/odds/index.html?race_id={race_id}", **{'X-Requested-With': 'XMLHttpRequest'})
    why = []
    for k in range(3):
        try:
            if k == 0:
                res = http_get(url, timeout=10, retries=1, **{})
            else:
                time.sleep(1.0 * k)
                res = requests.get(url, headers=hdr, timeout=10)
                res.raise_for_status()
            js = res.json()
            out = _parse_win_odds(js)
            if out:
                print(f"[odds] {race_id} 単勝オッズ{len(out)}頭（{js.get('status')}・{k + 1}回目）", flush=True)
                return out
            why.append(f"{k + 1}回目：中身なし（status={js.get('status')} reason={js.get('reason')}）")
        except Exception as e:
            why.append(f"{k + 1}回目：{type(e).__name__} {str(e)[:80]}")
    print(f"[odds] {race_id} 単勝オッズを取れず：" + ' ／ '.join(why), flush=True)
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
        past = [(class_of_text(l['r'], l['p']), eff_m(h, l) if l['pos'] != 1 else l['m']) for l in ls[:3]]
        cl = calc_level(past, rcv)
        is_new = not ls
        lv = cl['rating'] if cl else (SIRE_CLASS.get(h['sire'], 3) if is_new else 3)

        def mv(l):
            if l['pos'] == 1:
                return 0
            lcv = CLS[class_of_text(l['r'], l['p'])][1]
            relief = min(1, rcv / max(1, lcv))   # 格上レースの着差は軽く見る（クラス補填）
            return eff_m(h, l) * relief

        form = 3
        if ls:
            wl = [.5, .3, .2]
            use = ls[:3]
            tw = sum(wl[:len(use)])
            avg = sum(wl[i] * mv(l) for i, l in enumerate(use)) / tw
            form = 5 if avg <= 0.3 else 4 if avg <= 0.7 else 3 if avg <= 1.2 else 2 if avg <= 2.0 else 1
            if h.get('flowLastStrong'):   # 前走が流れに逆らった好走なら、近走は「やや好調」以上とみる
                form = max(form, 4)

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
        if race.get('abroad'):   # 海外の定量戦は斤量差を年齢・性別の評価（海外モード）で別に扱う
            jk = 3

        t3 = sum(1 for l in same if l['pos'] <= 3)
        why = []
        if is_new:
            why.append(f"新馬。父{h['sire'] or '不明'}の産駒レベルで評価")
        elif ls:
            l0 = ls[0]
            if l0.get('mEst'):   # 海外のレースは着差が載っていない
                res = '勝ち' if l0['pos'] == 1 else '着差の記載なし'
            else:
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
                    if fc != 8 and CLS[fc][1] > CLS[shared][1] and (not best or CLS[fc][1] > CLS[best[0]][1]):
                        best = (fc, f)
            if not best:
                continue
            won, tie = l['pos'] < o['pos'], l['pos'] == o['pos']
            gain = min(2, CLS[best[0]][1] - CLS[shared][1]) * (0.35 if won else 0.2 if tie else 0.12) * 0.05
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
    T = SETTINGS['T'] * (1 + 0.15 * race.get('chalk', 0))
    ex = [math.exp(T * (h['a'] - 1)) for h in arr]
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
    if apply_model(race, arr, odds):
        # 印（◎○▲…）の順番は「AIだけの評価」（オッズを見ない）。勝率・期待値・買い目の見込みは「オッズ＋AI」のまま。
        # オッズ＋AIの順で並べると、市場の評価が土台なので◎がほぼ1番人気になってしまうため
        arr.sort(key=lambda h: (-h['pAI'], -h['p']))
        N = len(arr)
        for i, h in enumerate(arr):
            h['rk'] = i + 1
            r_ = h['pAI'] * N
            h['rank'] = 'S' if r_ >= 2.0 else 'A' if r_ >= 1.3 else 'B' if r_ >= 0.8 else 'C' if r_ >= 0.5 else 'D'
            h['anaFlag'] = h['rk'] >= 4 and h['anaScore'] >= 0.02
    fav = min((h for h in arr if h.get('o')), key=lambda h: h['o'], default=None)
    race['fav'] = fav
    race['aiPickNote'] = (f"AI本命（1番人気は{fav['n']}番{fav['name']}）" if fav and arr and fav is not arr[0] else
                          'AI本命＝1番人気' if fav and arr else '')
    return arr


# ═════════════════════════════════════════
# 新方式：オッズ＋AI（過去5年分のデータで学習した重み data/model_weights.json）
#   ・オッズ（市場の評価）を土台に、AIの材料で補正して勝率を出す
#   ・材料の計算は model_row() の1か所だけ。バックテストもBotも同じものを使う
#   ・オッズが半分以上そろっていないとき（前日など）や海外レースは、今までのAIだけで計算する
# ═════════════════════════════════════════
MODEL_BASE = ['lv', 'form', 'fit', 'jk', 'paceAdj', 'mudb', 'ana', 'rl', 'bTime', 'bAgari', 'bDraw', 'bTrack',
              'bJockey', 'bCond', 'bCareer', 'bPos', 'bFlow', 'bHL']
MODEL = None
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'model_weights.json'), encoding='utf-8') as _f:
        MODEL = json.load(_f)
    print(f"[model] オッズ＋AIの重みを読み込み：材料{len(MODEL['names'])}個（学習 {MODEL.get('trained_range', '')}）", flush=True)
except Exception as _e:
    print(f"[model] 重みのファイルなし（今までのAIだけで計算）: {_e}", flush=True)


def model_row(race, h, N):
    """新方式の材料（オッズ以外）を作る。値はすべて数字。バックテストとBotで共通"""
    f = {}
    for k in MODEL_BASE:
        v = h.get(k)
        f[k] = float(v) if isinstance(v, (int, float)) else 0.0
    si = h.get('si')
    f['si'] = float(si) if si is not None else 0.0
    f['si_missing'] = 0.0 if si is not None else 1.0
    ag = h.get('agRank')
    f['agRankR'] = (ag - 1) / max(1, N - 1) if ag else 0.5
    lines = [l for l in h.get('lines', []) if is_flat(l)]
    l0 = lines[0] if lines else None
    ran = bool(l0 and l0['pos'] > 0)
    # 前走の負け方と人気の落ち方
    f['lastGap'] = clamp((l0['pos'] - l0['pop']) / 10, -1.5, 1.5) if ran and l0.get('pop') else 0.0
    f['lastPosR'] = (l0['pos'] - 1) / max(1, (l0.get('f') or 16) - 1) if ran else 0.5
    f['lastMargin'] = min(max(0.0, l0['m']), 3.0) if ran and not l0.get('mEst') else 1.0
    # 条件の変わり目
    d0 = dist_num(l0['dist']) if l0 else race['dist']
    f['distChg'] = (race['dist'] - d0) / 1000
    f['distChgAbs'] = abs(f['distChg'])
    f['surfChg'] = 1.0 if l0 and l0['dist'][:1] != race['surf'] else 0.0
    f['venueChg'] = 1.0 if l0 and l0['p'] != race['venue'] else 0.0
    f['clsChg'] = clamp(CLS[race['clsIdx']][1] - CLS[class_of_text(l0['r'], l0['p'])][1], -3, 3) if l0 else 0.0
    # 間隔・馬体重・斤量・キャリア・年齢・性別
    w = h.get('weeksCalc', h.get('weeks'))
    f['offLog'] = math.log1p(w) if w is not None else math.log1p(4)
    f['noRun'] = 0.0 if lines else 1.0
    f['bwd'] = clamp((h.get('bwDiff') or 0) / 10, -3, 3)
    f['bwdAbs'] = abs(f['bwd'])
    f['wtChg'] = clamp((h['wt'] - l0['w']) / 2, -4, 4) if l0 and l0.get('w') and h.get('wt') else 0.0
    f['careerLog'] = math.log1p(len(h.get('career') or h.get('lines') or []))
    f['age'] = float((h.get('age') or 4) - 4)
    f['female'] = 1.0 if h.get('sex') == '牝' else 0.0
    # 枠（短距離ほど効く）
    dp = draw_pos(h, N)
    f['draw'] = dp
    f['drawShort'] = dp if race['dist'] <= 1400 else 0.0
    # 騎手の「売れすぎ度」（勝った数－オッズから見た勝ちの見込み）。学習データから作った表
    f['jkBias'] = float(h.get('jkBias') or 0.0)
    # 調教師の売れすぎ度、調教師×騎手のコンビの売れすぎ度（学習データから作った表）
    f['trBias'] = float(h.get('trBias') or 0.0)
    f['jkFront'] = float(h.get('jkFront') or 0.0)
    f['tjBias'] = float(h.get('tjBias') or 0.0)
    # 昇級初戦：前走を勝って、今回クラスが上がった。勝ち方（2着との差、秒）も
    up = bool(ran and l0['pos'] == 1 and CLS[race['clsIdx']][1] > CLS[class_of_text(l0['r'], l0['p'])][1] + 0.1)
    f['upFirst'] = 1.0 if up else 0.0
    f['upFirstMargin'] = clamp(-l0['m'], 0.0, 1.5) if up and not l0.get('mEst') else 0.0
    # 当日の馬場：前残り度×この馬の予想位置、内有利度×この馬の枠（終わったレースが多いほど確か）
    tb = race.get('trackBias')
    if tb:
        conf = min(1.0, tb.get('races', 0) / 6)
        er_ = h.get('earlyRatio')
        f['tbFront'] = tb['front'] * conf * (0.5 - (er_ if er_ is not None else 0.5))
        f['tbInner'] = tb['inner'] * conf * (0.5 - dp)
    else:
        f['tbFront'], f['tbInner'] = 0.0, 0.0

    # ── 不利があったらしい前走（不利の記録は無いので、通過順と上がりから推測する） ──
    # ① 出遅れの推定：前走の最初のコーナーの位置が、その馬のふだん（2〜5走前）よりずっと後ろ
    def first_ratio(l):
        mm = re.match(r'(\d+)', l.get('ps') or '')
        n_ = l.get('f') or 0
        return (int(mm.group(1)) - 1) / (n_ - 1) if mm and n_ > 1 else None
    r0 = first_ratio(l0) if ran else None
    usual = [x for x in (first_ratio(l) for l in lines[1:5]) if x is not None]
    if r0 is not None and usual:
        dev = r0 - sum(usual) / len(usual)
        f['startDev'] = clamp(dev, -1.0, 1.0)
        f['lateStart'] = 1.0 if dev >= 0.35 and sum(usual) / len(usual) <= 0.5 else 0.0
    else:
        f['startDev'], f['lateStart'] = 0.0, 0.0
    # ② 脚を余した負けの推定：前走の上がりがメンバー3位以内なのに、5着以下
    f['l3GoodLoss'] = 0.0
    c0 = next((l for l in (h.get('career') or []) if l['pos'] > 0 and is_flat(l)), None)
    if c0 and c0.get('rid') and c0['pos'] >= 5:
        res = (parse_result_full(c0['rid']) if os.environ.get('SYH_BACKTEST')
               else (_cache.get(('res', c0['rid'])) or (0, None))[1])   # Botでは取得済みの結果だけ使う（待たない）
        if res:
            me = next((r for r in res['rows'] if r['n'] == c0.get('n')), None)
            l3s = sorted(r['l3'] for r in res['rows'] if r.get('l3'))
            if me and me.get('l3') and l3s and l3s.index(me['l3']) <= 2:
                f['l3GoodLoss'] = (c0['pos'] - 1) / max(1, res['N'] - 1)
    # ③ 人気の落ち方：今回の人気－前走の人気（＋は人気を落とした）
    pn = h.get('popNow')
    f['popDrop'] = clamp((pn - l0['pop']) / 10, -1.5, 1.5) if pn and ran and l0.get('pop') else 0.0

    # ── 展開：予想位置（0＝先頭〜1＝最後方）と予想ペースの組み合わせ ──
    er = h.get('earlyRatio')
    f['early'] = float(er) if er is not None else 0.5
    pace = race.get('pace')
    f['earlyFast'] = f['early'] if pace == 'fast' else 0.0
    f['earlySlow'] = f['early'] if pace == 'slow' else 0.0
    f['soloLead'] = 1.0 if h.get('bPos', 0) > 0 else 0.0
    f['hanaFight'] = 1.0 if h.get('bPos', 0) < 0 else 0.0
    # コース・距離ごとの「前の馬・内枠の馬が実際どれだけ有利だったか」（5年分の結果から）× この馬の位置・枠
    f['courseFront'] = float(h.get('courseFront') or 0.0) * (0.5 - f['early'])
    f['courseDraw'] = float(h.get('courseDraw') or 0.0) * (0.5 - dp)
    # ── 調教 ──
    # ── ハイレベル戦：過去3走のハイレベル度の最大と、ハイレベル戦での走りぶり ──
    hs = [(v, p) for v, p in (h.get('hlScores') or []) if v is not None]
    f['hlMax'] = clamp(max(v for v, _ in hs), -2, 4) if hs else 0.0
    f['hlFinish'] = clamp(max(v * (1.0 if p and p <= 3 else 0.5 if p and p <= 5 else 0.2) for v, p in hs), -2, 4) if hs else 0.0
    f['hlMissing'] = 0.0 if hs else 1.0
    good = [v for v, p in hs if p and p <= 3]
    f['hlLowGood'] = clamp(min(good), -4, 0) if good else 0.0
    sc = TRAIN_SCORE.get(h.get('trainGrade'))
    f['trainScore'] = float(sc) if sc is not None else 0.0
    f['trainMissing'] = 0.0 if sc is not None else 1.0
    return f


def apply_model(race, arr, odds):
    """オッズ＋AIの勝率に置き換える。置き換えたら True"""
    if not MODEL or os.environ.get('SYH_BACKTEST') or race.get('abroad'):
        return False
    have = [h for h in arr if odds.get(h['n'])]
    if len(have) < max(5, math.ceil(len(arr) * 0.8)):
        race['modelNote'] = 'オッズ未発表のため、AIだけで計算'
        return False
    mx = max(odds[h['n']] for h in have)
    inv = {h['n']: 1 / (odds.get(h['n']) or mx * 1.5) for h in arr}
    tot = sum(inv.values())
    names, mean, std, beta = MODEL['names'], MODEL['mean'], MODEL['std'], MODEL['beta']
    jt = MODEL.get('jockey_bias', {})
    N = len(arr)
    for k, h in enumerate(sorted(arr, key=lambda h: odds.get(h['n']) or 9999), 1):
        h['popNow'] = k
    zs = []
    for h in arr:
        h['jkBias'] = jt.get(h.get('jid') or '', 0.0)
        ct = MODEL.get('course_tables', {}).get(f"{race['venue']}|{race['surf']}|{race['dist']}")
        h['courseFront'], h['courseDraw'] = (ct if ct else (0.0, 0.0))
        h['trBias'] = MODEL.get('trainer_bias', {}).get(h.get('trid') or '', 0.0)
        h['tjBias'] = MODEL.get('combo_bias', {}).get(f"{h.get('trid')}|{h.get('jid')}", 0.0)
        row = model_row(race, h, N)
        x = [math.log(inv[h['n']] / tot)] + [row.get(n, 0.0) for n in names[1:]]
        zs.append(sum(b * (v - m) / s for b, v, m, s in zip(beta, x, mean, std)))
    zmax = max(zs)
    ex = [math.exp(z - zmax) for z in zs]
    tot2 = sum(ex)
    for h, e in zip(arr, ex):
        h['pAI'] = h['p']
        h['p'] = e / tot2
        h['ev'] = h['p'] * h['o'] if h.get('o') else None
    race['model'] = True
    race['modelNote'] = f"勝率＝オッズ＋AI（{MODEL.get('trained_range', '過去データ')}で学習）"
    return True


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


def win_pick(arr):
    """単勝の推奨馬。基本は◎。ただし◎のオッズが安すぎて割に合わず（期待値0.7未満）、
    ○▲に期待値1.0以上の馬がいれば、そちらを推す"""
    top = arr[0]
    if top.get('ev') is not None and top['ev'] < 0.7:
        alt = [h for h in arr[1:3] if h.get('ev') and h['ev'] >= 1.0]
        if alt:
            return max(alt, key=lambda h: h['ev']), '期待値で◎より上'
    return top, '◎'


VERIFY_EV = 1.1      # 検証枠の基準（期待値がこれ以上の単勝を記録）
VERIFY_LABEL = '検証'


# ── 組み合わせの「当たる見込み」と「予想配当」 ──
# 単勝オッズから各馬の勝つ見込みを出し、Harville（ハービル）の式で組み合わせの見込みに広げる
TAKE = {'単勝': .20, '複勝': .20, '馬連': .225, 'ワイド': .225, '馬単': .25, '3連複': .25, '3連単': .275}   # JRAの控除率
MIN_ODDS = {'馬連': 4.0, 'ワイド': 1.8, '馬単': 6.0, '3連複': 8.0, '3連単': 30.0}   # これ未満の予想配当は買わない
HIGH_MAX = {'3連単': 18, '3連複': 10}                                                # 高目の点数の上限


def prob_tables(arr):
    """pa：予想の勝率（AI／オッズ＋AI）、pm：オッズ（市場）から見た勝率"""
    pa = {h['n']: max(1e-6, h['p']) for h in arr}
    have = [h for h in arr if h.get('o')]
    if len(have) >= max(3, math.ceil(len(arr) * 0.8)):
        mx = max(h['o'] for h in have)
        inv = {h['n']: 1 / (h.get('o') or mx * 1.5) for h in arr}
        t = sum(inv.values())
        pm = {k: v / t for k, v in inv.items()}
    else:
        pm = dict(pa)
    return pa, pm


def combo_prob(kind, c, p):
    def ex(i, j):
        return p[i] * p[j] / max(1e-9, 1 - p[i])

    def tri(i, j, k):
        return ex(i, j) * p[k] / max(1e-9, 1 - p[i] - p[j])

    def trio(i, j, k):
        return tri(i, j, k) + tri(i, k, j) + tri(j, i, k) + tri(j, k, i) + tri(k, i, j) + tri(k, j, i)
    if kind == '単勝':
        return p[c[0]]
    if kind == '複勝':
        i = c[0]
        others = [x for x in p if x != i]
        return p[i] + sum(ex(j, i) for j in others) + sum(tri(j, k, i) for j in others for k in others if j != k)
    if kind == '馬単':
        return ex(c[0], c[1])
    if kind == '馬連':
        return ex(c[0], c[1]) + ex(c[1], c[0])
    if kind == '3連単':
        return tri(*c)
    if kind == '3連複':
        return trio(*c)
    if kind == 'ワイド':
        i, j = c
        return sum(trio(i, j, k) for k in p if k not in (i, j))
    return 0.0


def est_odds(kind, c, pm):
    """予想配当（倍）。実際の配当は売れ方で変わるので目安"""
    return min(99999.0, (1 - TAKE.get(kind, .25)) / max(combo_prob(kind, c, pm), 1e-6))


def value_horse(arr, pa, pm, lo=3, hi=8):
    """AIがオッズより高く評価している馬（妙味の相手）"""
    cands = [h for h in arr[lo:hi] if pa[h['n']] / pm[h['n']] >= 1.05]
    return max(cands, key=lambda h: pa[h['n']] / pm[h['n']]) if cands else None


def _keep(kind, combos, pm):
    keep = [c for c in combos if est_odds(kind, c, pm) >= MIN_ODDS.get(kind, 0)]
    return keep, len(combos) - len(keep)


def pair_pick(arr, kind, pm, vh=None):
    """馬連・ワイドの1点：◎-○。配当が安すぎれば◎-▲、次に◎-妙味の相手"""
    n = [h['n'] for h in arr]
    cands = [(n[0], n[1]), (n[0], n[2])] + ([(n[0], vh['n'])] if vh and vh['n'] not in n[:3] else [])
    return next(([c] for c in cands if est_odds(kind, c, pm) >= MIN_ODDS.get(kind, 0)), [])


def _note(dropped):
    return f"・配当の安い{dropped}点を除外" if dropped else ''


def bet_plan(arr):
    """おすすめ買い目。各要素は {'label','text','parts':[(券種, [組み合わせ,...]), ...]}
    予想配当が安すぎる組み合わせは外し、人気馬ばかりのときは妙味のある相手を入れる"""
    n = [h['n'] for h in arr]
    plan = []
    if not n:
        return plan
    pa, pm = prob_tables(arr)
    w = arr[0]   # 本命（◎）＝軸。単勝はこの馬だけ
    ev_t = f"・期待値{w['ev']:.2f}" if w.get('ev') else ''
    plan.append({'label': '推奨', 'text': f"単勝 {w['n']}（◎軸{ev_t}）", 'parts': [('単勝', [(w['n'],)])]})
    pop_top3 = set(sorted(pm, key=lambda k: -pm[k])[:3])
    fav_all = len(n) >= 3 and set(n[:3]) <= pop_top3      # ◎○▲が3頭とも上位人気
    vh = value_horse(arr, pa, pm)
    if len(n) >= 3:
        for kind in ('馬連', 'ワイド'):
            c1 = pair_pick(arr, kind, pm, vh)
            plan.append({'label': '本線', 'text': (f"{kind} {c1[0][0]}-{c1[0][1]}（◎-{'○' if c1[0][1] == n[1] else '▲' if c1[0][1] == n[2] else '妙味の相手'}）"
                                                  if c1 else f"{kind} 見送り（人気どうしで配当が安すぎる）"),
                         'parts': [(kind, c1)]})
    if len(n) >= 6:
        partners = n[1:6]
        swap = ''
        if fav_all and vh and vh['n'] not in partners:
            partners = n[1:5] + [vh['n']]
            swap = f"・人気馬ばかりのため{vh['n']}番を相手に追加"
        p_ = ','.join(map(str, partners))
        fuku = sorted({tuple(sorted((n[0], b, c))) for b in n[1:3] for c in partners if b != c})
        fuku, d2 = _keep('3連複', fuku, pm)
        plan.append({'label': '本線', 'text': (f"3連複 {n[0]} - {n[1]},{n[2]} - {p_}（{len(fuku)}点{_note(d2)}{swap}）" if fuku
                     else "3連複 見送り（人気どうしで配当が安すぎる）"), 'parts': [('3連複', fuku)]})
        tan = [(n[0], b, c) for b in n[1:3] for c in partners if b != c]
        tan, d3 = _keep('3連単', tan, pm)
        plan.append({'label': '本線', 'text': (f"3連単 1着{n[0]} → 2着{n[1]},{n[2]} → 3着{p_}（{len(tan)}点{_note(d3)}{swap}）" if tan
                     else "3連単 見送り（人気どうしで配当が安すぎる）"), 'parts': [('3連単', tan)]})
    evs = [h for h in arr[1:8] if h.get('ev') and h['ev'] >= 1.15]
    if evs:
        b = max(evs, key=lambda h: h['ev'])
        plan.append({'label': '妙味', 'text': f"単複 {b['n']}（期待値{b['ev']:.2f}）",
                     'parts': [('単勝', [(b['n'],)]), ('複勝', [(b['n'],)])]})
    anas = [h for h in arr if h['anaFlag']][:1]   # 穴のワイドも1点
    if anas:
        aw, _ = _keep('ワイド', [(n[0], h['n']) for h in anas], pm)
        if aw:
            plan.append({'label': '穴', 'text': "ワイド " + ', '.join(f"{a}-{b}" for a, b in aw), 'parts': [('ワイド', aw)]})
    two = two_axis_trio(arr, pm)
    if two:
        plan.append(two)
    # 検証枠：新方式（オッズ＋AI）で期待値1.1以上の単勝。本当に効くかを記録で確かめるためのもの（買わない）
    if any('pAI' in h for h in arr):
        ver = [h for h in arr if h.get('ev') and h['ev'] >= VERIFY_EV]
        if ver:
            plan.append({'label': '検証', 'text': "単勝 " + ','.join(str(h['n']) for h in ver)
                         + f"（期待値{VERIFY_EV}以上・記録用）", 'parts': [('単勝', [(h['n'],) for h in ver])]})
    return plan


def bets(arr):
    return [f"{b['label']} {b['text']}" for b in bet_plan(arr)]


TWO_AXIS_LABEL = '2頭軸'


def two_axis_trio(arr, pm, mates=5):
    """3連複の2頭軸：◎-○を軸に、相手5頭（▲以下の印順）。配当が安すぎる組み合わせは、次の馬と入れ替えて5頭にそろえる"""
    if len(arr) < 4:
        return None
    a, b = arr[0]['n'], arr[1]['n']
    picked, skipped = [], 0
    for h in arr[2:]:
        c = tuple(sorted((a, b, h['n'])))
        if est_odds('3連複', c, pm) >= MIN_ODDS['3連複']:
            picked.append(h['n'])
        else:
            skipped += 1
        if len(picked) >= mates:
            break
    if not picked:
        return {'label': TWO_AXIS_LABEL, 'text': '3連複 見送り（人気どうしで配当が安すぎる）', 'parts': [('3連複', [])]}
    combos = [tuple(sorted((a, b, x))) for x in picked]
    note = f"・配当の安い{skipped}頭を次の馬と入れ替え" if skipped else ''
    return {'label': TWO_AXIS_LABEL,
            'text': f"3連複 軸{a}-{b} 相手{','.join(map(str, picked))}（◎○軸・{len(combos)}点{note}）",
            'parts': [('3連複', combos)]}


# ── イチ推しの買い方：6つの買い方（単勝・馬連・ワイド・3連複・3連単・3連複2頭軸流し）から1つ ──
BEST_LABEL = 'イチ推し'
BEST_MIN_HIT = 0.08      # 当たる見込みがこれ未満の買い方は選ばない（夢馬券で収支がぶれないように）
BEST_KINDS = [('推奨', '単勝', '単勝'), ('本線', '馬連', '馬連'), ('本線', 'ワイド', 'ワイド'),
              ('本線', '3連複', '3連複'), ('本線', '3連単', '3連単'), (TWO_AXIS_LABEL, '3連複', '3連複2頭軸流し')]


def best_bet(arr, plan=None):
    """出している買い方のうち、どれが一番おすすめかを1つ選ぶ。買い方ごとに全部の点数をまとめて評価する。
    ・的中確率：その買い方のどれかが当たる見込み　・予想配当：当たったときの平均の払い戻し（点数分の投資に対して何倍か）
    ・期待値：払い戻しの見込み÷投資　・妙味：AIの見込みが市場（オッズ）の見込みの何倍か
    物差しはケリー基準の「資金の伸び」：期待値が高いほど、当たる見込みが高いほど大きい。
    期待値だけだと当たりにくい買い方、的中率だけだと配当の安い買い方に偏るので、その間を取る"""
    if plan is None:
        plan = bet_plan(arr)
    pa, pm = prob_tables(arr)
    cands = []
    for label, kind, name in BEST_KINDS:
        b = next((x for x in plan if x['label'] == label and any(k == kind and cs for k, cs in x['parts'])), None)
        if not b:
            continue
        combos = next(cs for k, cs in b['parts'] if k == kind)
        n = len(combos)
        ps = [combo_prob(kind, c, pa) for c in combos]
        pms = [combo_prob(kind, c, pm) for c in combos]
        o_real = {h['n']: h['o'] for h in arr if h.get('o')}
        ods = [o_real.get(c[0]) or est_odds(kind, c, pm) if kind == '単勝' else est_odds(kind, c, pm) for c in combos]
        hit = min(0.99, sum(ps))                     # 組み合わせどうしは同時に当たらない（ワイドは1点）
        ret = sum(p_ * o for p_, o in zip(ps, ods))  # 1点100円あたりの払い戻しの見込み（点数分の合計）
        ev = ret / n
        pay = ret / hit / n if hit > 0 else 0.0      # 当たったときの払い戻し÷投資
        edge = sum(ps) / max(sum(pms), 1e-9)
        f = (ev - 1) / (pay - 1) if ev > 1 and pay > 1 else 0.0
        f = min(f, 0.99)
        g = hit * math.log(1 + f * (pay - 1)) + (1 - hit) * math.log(1 - f) if f > 0 else 0.0
        cands.append({'label': label, 'kind': kind, 'name': name, 'combos': combos, 'n': n, 'cost': n * 100,
                      'hit': hit, 'pay': pay, 'odds': sum(ods) / n, 'ev': ev, 'edge': edge, 'g': g, 'text': b['text']})
    if not cands:
        return None
    if not any(h.get('o') for h in arr):   # オッズ未発表：期待値は計算できないので、当たる見込みが一番高い買い方
        best = max(cands, key=lambda x: (x['hit'] / x['n'], x['hit']))
        best['noOdds'] = True
        best['why'] = 'オッズ未発表のため、1点あたりの当たる見込みで選択（期待値はオッズ発表後に）'
        best['all'] = cands
        return best
    pool = [x for x in cands if x['hit'] >= BEST_MIN_HIT] or cands
    plus = [x for x in pool if x['ev'] > 1.0]
    if plus:
        best = max(plus, key=lambda x: (x['g'], x['ev']))
        best['why'] = '期待値1超の買い方の中で、当たる見込みと配当のつり合いが最も良い'
    else:
        best = max(pool, key=lambda x: (x['ev'], x['hit']))
        best['why'] = '期待値1超の買い方が無いため、最も損の少ない買い方（見送りも検討）'
    best['all'] = sorted(cands, key=lambda x: -x['g'] if x['ev'] > 1 else 1 - x['ev'])
    return best


def best_bet_text(bb):
    if not bb:
        return ''
    if bb.get('noOdds'):
        return f"{bb['name']}（{bb['n']}点・{bb['cost']:,}円）　的中約{bb['hit'] * 100:.0f}%（オッズ未発表のため期待値は未計算）"
    return (f"{bb['name']}（{bb['n']}点・{bb['cost']:,}円）　的中約{bb['hit'] * 100:.0f}%・"
            f"当たれば約{bb['pay']:.1f}倍・期待値{bb['ev']:.2f}・妙味{bb['edge']:.2f}倍")


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
_cache_lock = threading.Lock()
_key_locks = {}
CACHE_MAX = 4000     # これを超えたら古いものから捨てる（Renderのメモリ対策）


def cached(key, ttl, fn, fail_ttl=300):
    """取得結果を ttl 秒覚えておく。取れなかった（None・エラー）ときは fail_ttl 秒（5分）だけ覚えて、
    同じ遅いページを何度も待たないようにする。同じキーを同時に取りに行かない（2本目は1本目の結果を待つ）"""
    def fresh():
        hit = _cache.get(key)
        return hit if hit and time.time() - hit[0] < (ttl if hit[1] is not None else fail_ttl) else None
    hit = fresh()
    if hit:
        return hit[1]
    with _cache_lock:
        lk = _key_locks.setdefault(key, threading.Lock())
    with lk:
        hit = fresh()
        if hit:
            return hit[1]
        try:
            val = fn()
        except Exception:
            with _cache_lock:
                _cache[key] = (time.time(), None)
            raise
        finally:
            with _cache_lock:
                _key_locks.pop(key, None)
        with _cache_lock:
            _cache[key] = (time.time(), val)
            if len(_cache) > CACHE_MAX:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[:len(_cache) - CACHE_MAX + 200]:
                    _cache.pop(k, None)
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


# 実データの基準タイム（GitHub Actions が作る data/std_times.json）
STD_REAL = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'std_times.json'), encoding='utf-8') as _f:
        STD_REAL = json.load(_f)
    print(f"[std] 実データの基準タイムを読み込み：{STD_REAL.get('n_races')}レース（{STD_REAL.get('first')}〜{STD_REAL.get('last')}）", flush=True)
except Exception as _e:
    print(f"[std] 実データの基準タイムなし（目安の値を使います）: {_e}", flush=True)


def jra_cls_group(l):
    """中央のクラスを基準タイム表の区分に（0=新馬未勝利 1〜3=1〜3勝 4=OP 5=重賞）。地方は None"""
    ci = class_of_text(l['r'], l['p'])
    return {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 5, 7: 5}.get(ci)


def std_time_real(l):
    """実データから、そのレースの条件（競馬場・芝ダ・距離・馬場・クラス）の基準勝ちタイムと、
    その日の馬場の速さを考えた基準タイムを返す。無ければ None"""
    if not STD_REAL or l['p'] not in JRA_PLACE.values():
        return None
    surf, dist, cond = l['dist'][:1], dist_num(l['dist']), (l.get('cond') or '良')[:1]
    cg = jra_cls_group(l)
    km = dist / 1000
    ex, base = STD_REAL.get('exact', {}), STD_REAL.get('base', {})
    cls_off = STD_REAL.get('cls_off', {}).get(surf, {})
    cond_off = STD_REAL.get('cond_off', {}).get(surf, {})
    st_ = None
    e = ex.get(f"{l['p']}|{surf}|{dist}|{cond}|{cg}")
    if e and e[1] >= 5:
        st_ = e[0]
    else:
        b = base.get(f"{l['p']}|{surf}|{dist}|{cond}")
        if b and b[1] >= 5:
            st_ = b[0] + cls_off.get(str(cg), 0) * km
        else:
            b = base.get(f"{l['p']}|{surf}|{dist}|良")
            if b and b[1] >= 5:
                st_ = b[0] + (cond_off.get(cond, 0) if cond != '良' else 0) * km + cls_off.get(str(cg), 0) * km
    if st_ is None:
        return None
    v = STD_REAL.get('variant', {}).get(f"{l['d']}|{l['p']}|{surf}")
    if v:
        st_ += v[0] * km   # 時計のかかる日は基準を遅く、速い日は速く
    return st_


def speed_index(l, fstd=None):
    """1走分の簡易タイム指数（目安タイムより1000mあたり1秒速いと＋10）。
    地方など目安タイムが無い競馬場は、出走馬の過去走から作った目安（fstd）を使う"""
    surf, dist = l['dist'][:1], dist_num(l['dist'])
    if not l.get('t') or not dist:
        return None
    st_ = std_time_real(l)
    if st_ is not None:
        l['_stdReal'] = True
    else:
        st_ = std_time(surf, dist, l['p'], l['cond']) if surf in STD_TIME else None
    if not st_ and fstd:
        st_ = fstd.get((l['p'], surf, dist))
    if not st_:
        return None
    return (st_ - l['t']) / (dist / 1000) * 10


def rate_by_diff(d, steps):
    a, b = steps
    return 5 if d >= b else 4 if d >= a else 3 if d > -a else 2 if d > -b else 1


def time_factors(race, horses):
    """③ 走破タイム（簡易指数）と上がり3ハロン"""
    surf, dist = race['surf'], race['dist']
    # 目安タイムが無い競馬場用に、出走馬の過去走（同じ競馬場・距離）の中央値を目安にする
    pool = {}
    for h in horses:
        for l in h.get('career', h['lines']):
            if l['pos'] > 0 and l.get('t'):
                pool.setdefault((l['p'], l['dist'][:1], dist_num(l['dist'])), []).append(l['t'])
    fstd = {k: sorted(v)[len(v) // 2] for k, v in pool.items() if len(v) >= 3}
    for h in horses:
        near = [l for l in h['lines'] if l['pos'] > 0 and l['dist'][:1] == surf
                and abs(dist_num(l['dist']) - dist) <= 600][:4]
        idx = sorted([x for x in (speed_index(l, fstd) for l in near) if x is not None], reverse=True)
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
        real = sum(1 for l in h['lines'][:4] if l.get('_stdReal'))
        h['siReal'] = real
        h['bTime'] = (h['siRate'] - 3) / 2 * (0.03 if real >= 2 else 0.02)
        h['bAgari'] = (h['agRate'] - 3) / 2 * 0.012 * closer * pace_k


def draw_pos(h, N):
    return ((h['n'] or 1) - 1) / max(1, N - 1)   # 0=最内、1=大外


def draw_factors(race, horses):
    """④-1 コースの枠順の有利不利"""
    N = len(horses)
    key = (race['venue'], race['surf'], race['dist'])
    bias = 0 if (race.get('banei') or 'nar.' in race.get('base', '')) else COURSE_DRAW.get(key)
    if race.get('abroad'):
        # 海外はゲート番で別に評価する（海外モード）。ここでは使わない
        bias = 0
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
        res = http_get(url, timeout=10)
        return sorted(set(re.findall(r'race_id=(\d{12})', res.text)))
    return cached(('list', date), 600, load)


def parse_result_full(race_id):
    """終わったレースの結果：ペース(H/M/S)・頭数・芝ダ・全馬の(着順,馬番,最初と最後のコーナー位置,上がり,着差)"""
    def load():
        soup = fetch_soup(f"{site_of(race_id)}/race/result.html?race_id={race_id}", timeout=8)
        d1 = soup.select_one('.RaceData01')
        sm = re.search(r'(芝|ダ)', d1.get_text()) if d1 else None
        table = soup.select_one('table#All_Result_Table') or soup.select_one('table.RaceTable01')
        if not table:
            return None
        header = table.select_one('tr.Header') or table.find('tr')
        cols = [clean(c.get_text()) for c in header.find_all(['th', 'td'])]
        col = lambda *ks: next((i for i, c in enumerate(cols) if any(k in c for k in ks)), None)
        i_n, i_c, i_l3 = col('馬番'), col('通過'), col('後3F', '上り', '上がり')
        rows = []
        for tr in table.select('tr.HorseList') or table.find_all('tr')[1:]:
            tds = tr.find_all('td')
            if not tds or i_n is None or i_n >= len(tds):
                continue
            rk = clean(tds[0].get_text())
            n = to_float(tds[i_n].get_text())
            corners = [int(x) for x in re.findall(r'\d+', tds[i_c].get_text())] if i_c is not None and i_c < len(tds) else []
            l3 = to_float(tds[i_l3].get_text()) if i_l3 is not None and i_l3 < len(tds) else None
            if n and rk not in ('取消', '除外'):
                rows.append({'pos': int(rk) if rk.isdigit() else 99, 'n': int(n),
                             'c1': corners[0] if corners else None, 'c4': corners[-1] if corners else None, 'l3': l3})
        if not rows or not any(r['pos'] == 1 for r in rows):
            return None
        pm = re.search(r'ペース\s*[:：]?\s*([HMS])(?![a-zA-Z])', soup.get_text(' '))
        return {'surf': sm.group(1) if sm else None, 'N': len(rows), 'rows': rows, 'pace': pm.group(1) if pm else None}
    return cached(('res', race_id), 7 * 86400, load)


def parse_result_brief(race_id):
    r = parse_result_full(race_id)
    if not r:
        return None
    return {'surf': r['surf'], 'N': r['N'], 'top3': [(x['pos'], x['n'], x['c4']) for x in r['rows'] if x['pos'] <= 3]}


def track_bias(race, race_id, date):
    """④-2 当日の同じ競馬場・同じ芝ダの、終わったレースの上位馬から馬場の傾向を読む"""
    if race.get('banei') or race.get('abroad'):
        return None
    # 同じ日・同じ競馬場のレースIDは、最後の2桁（R）だけが違う
    ids = [race_id[:10] + f"{r_:02d}" for r_ in range(1, int(race_id[-2:]))]
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


def parse_jockey_tables(soup):
    """騎手ページの「年度別成績」の表（上から中央・地方）を読む。
    見出しは「年度・順位・1着・2着・3着・4着〜・騎乗回数…・勝率・連対率・複勝率」"""
    year = str(jst_today().year)
    out = []
    for table in soup.find_all('table'):
        head = table.find('tr')
        if not head:
            continue
        cols = [clean(c.get_text()) for c in head.find_all(['th', 'td'])]
        if '1着' not in cols or not any('複勝率' in c for c in cols):
            continue
        i1, i2, i3 = cols.index('1着'), cols.index('2着'), cols.index('3着')
        i4 = next((i for i, c in enumerate(cols) if c.startswith('4着') or c == '着外'), None)
        if i4 is None:
            continue
        rows = {}
        for tr in table.find_all('tr')[1:]:
            cells = [clean(c.get_text()) for c in tr.find_all(['th', 'td'])]
            if not cells or len(cells) <= i4:
                continue
            v = [int((to_float(cells[i].replace(',', '')) or 0)) for i in (i1, i2, i3, i4)]
            rides = sum(v)
            if rides:
                rows[cells[0]] = {'rides': rides, 'win': v[0] / rides, 'fuku': sum(v[:3]) / rides}
        pick = None
        for want, span in ((year, '今年'), ('累計', '通算')):
            r = rows.get(want)
            if r and r['rides'] >= 30:
                pick = dict(r, span=span)
                break
        out.append(pick)
    return out


def fetch_jockey_stats(jid):
    """騎手の成績（騎乗数・勝率・複勝率）を中央・地方それぞれ。取れなければ None"""
    def load():
        soup = fetch_soup(f"https://db.netkeiba.com/jockey/{jid}/", timeout=8)
        tables = parse_jockey_tables(soup)
        if not tables or not any(tables):
            return None
        return {'jra': tables[0] if len(tables) > 0 else None,
                'nar': tables[1] if len(tables) > 1 else None}
    return cached(('jk', jid), 86400, load)


def pick_jockey_stats(both, is_nar):
    """レースに合わせて中央・地方の成績を選ぶ（少なければもう一方を使う）"""
    if not both:
        return None
    first, second = (both.get('nar'), both.get('jra')) if is_nar else (both.get('jra'), both.get('nar'))
    s = first or second
    if s and s is second:
        s = dict(s, span=s['span'] + ('・中央' if is_nar else '・地方'))
    return s


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
        h['change'] = bool(prev) and h['jockey'] not in ('', '不明') and norm(prev) != norm(h['jockey'])
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
    early = race.pop('_early', None)
    if early is None:
        early = {'tb': BG.submit(_safe, track_bias, race, race_id, race['date']),
                 'jk': {j: BG.submit(_safe, fetch_jockey_stats, j) for j in {h['jid'] for h in horses if h.get('jid')}}}
    # 待つのは最大10秒まで。間に合わなかった分は使わずに先へ進む（予想全体を遅らせない）
    t0 = time.time()
    futs = [early['tb']] + list(early['jk'].values()) + ([early['tr']] if early.get('tr') else [])
    wait(futs, timeout=10)
    late = sum(1 for f in futs if not f.done())
    if late:
        print(f"[extra] {late}件の取得が間に合わず省略（{time.time() - t0:.1f}秒待機）", flush=True)
    race['trackBias'] = early['tb'].result() if early['tb'].done() else None
    stats = {j: (f.result() if f.done() else None) for j, f in early['jk'].items()}
    is_nar = 'nar.' in race.get('base', '')
    for h in horses:
        h['jkStats'] = pick_jockey_stats(stats.get(h.get('jid')), is_nar)
        f = early['jk'].get(h.get('jid'))
        h['jkStatus'] = ('OK' if h['jkStats'] else
                         '騎手のIDが取れず' if not h.get('jid') else
                         '時間切れ（10秒で打ち切り）' if f is not None and not f.done() else
                         '騎手のページを読めず')
    time_factors(race, horses)
    draw_factors(race, horses)
    bias_factors(race, horses)
    jockey_factors(race, horses)
    condition_factors(race, horses)
    career_factors(race, horses)
    course_factors(race, horses)
    class_move_factors(race, horses)
    abroad_factors(race, horses)
    ft = early.get('tr')
    train_factors(race, horses, race.get('trainTable') or (ft.result() if ft is not None and ft.done() else None))
    if race.get('course'):   # 騎手の腕が出やすい競馬場は、騎手データの効きを強く
        for h in horses:
            h['bJockey'] *= race['course']['jockey']
    for h in horses:
        h['xBonus'] = h['bTime'] + h['bAgari'] + h['bDraw'] + h['bTrack'] + h['bJockey'] + h['bCond'] + h['bCareer'] + h.get('bPos', 0) + h.get('bFlow', 0) + h.get('bCourse', 0) + h.get('bMove', 0) + h.get('bAbroad', 0) + h.get('bHL', 0) + h.get('bTrain', 0)


# ═════════════════════════════════════════
# 調教（netkeibaの「調教タイム・追い切り」ページの評価 A〜D と短評）
# ═════════════════════════════════════════
TRAIN_SCORE = {'A': 2, 'B': 1, 'C': 0, 'D': -1, 'E': -2}


def parse_oikiri(soup):
    """調教ページから {馬ID または 'n馬番': (評価, 短評)} を作る"""
    out = {}
    for tr in soup.find_all('tr'):
        tds = tr.find_all('td')
        if len(tds) < 4:
            continue
        cells = [clean(td.get_text()) for td in tds]
        gi = next((i for i in range(len(cells) - 1, -1, -1) if re.fullmatch(r'[A-E]', cells[i])), None)
        if gi is None:
            continue
        grade = cells[gi]
        comment = next((cells[i] for i in range(gi - 1, -1, -1) if cells[i] and not cells[i].isdigit()
                        and not re.fullmatch(r'[A-E]', cells[i]) and not tds[i].find('a', href=HORSE_HREF)), '')
        a = tr.find('a', href=HORSE_HREF)
        hm = re.search(r'/horse/([0-9a-z]{10})', a.get('href', '')) if a else None
        nums = [int(c) for c in cells[:3] if c.isdigit()]
        if hm:
            out[hm.group(1)] = (grade, comment)
        if len(nums) >= 2:
            out[f'n{nums[1]}'] = (grade, comment)
    return out


def fetch_oikiri(race_id):
    def load():
        return parse_oikiri(fetch_soup(f"https://race.netkeiba.com/race/oikiri.html?race_id={race_id}", timeout=8)) or None
    return cached(('oikiri', race_id), 3 * 3600, load)


def train_factors(race, horses, table):
    """調教の評価を各馬にのせる。今までのAI用には小さな加点（新方式では学習した重みで効く）"""
    for h in horses:
        t = (table or {}).get(h.get('hid') or '') or (table or {}).get(f"n{h['n']}")
        h['trainGrade'], h['trainComment'] = (t if t else (None, ''))
        sc = TRAIN_SCORE.get(h['trainGrade'])
        h['bTrain'] = {2: 0.012, 1: 0.004, 0: 0.0, -1: -0.008, -2: -0.012}.get(sc, 0.0) if sc is not None else 0.0


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
                soup = fetch_soup(url, timeout=10)
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
        if not (any(c in ('日付', 'レース名') for c in cols) and any('着順' in c for c in cols) and any('距離' in c for c in cols)):
            continue
        col = lambda *ks: next((i for i, c in enumerate(cols) if any(c == k or c.startswith(k) for k in ks)), None)
        I = {'d': col('日付'), 'p': col('開催'), 'r': col('レース名'), 'f': col('頭数'), 'n': col('馬番'),
             'pop': col('人気'), 'pos': col('着順'), 'j': col('騎手'), 'w': col('斤量'), 'dist': col('距離'),
             'cond': col('馬場'), 't': col('タイム'), 'm': col('着差'), 'ps': col('通過'), 'l3': col('上り'),
             'bw': col('馬体重'), 'win': col('勝ち馬'), 'pace': col('ペース')}
        out = []
        for tr in table.find_all('tr')[1:]:
            tds = tr.find_all('td')
            g = lambda k: (clean(tds[I[k]].get_text()) if I.get(k) is not None and I[k] < len(tds) else '')
            dm = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', g('d'))
            venue_txt, rname = g('p'), g('r')
            r_td = tds[I['r']] if I.get('r') is not None and I['r'] < len(tds) else None
            if not dm and r_td is not None:   # スマホ版：「26/09/13 阪神 12R レース名」
                sm_ = re.match(r'(\d{2})/(\d{2})/(\d{2})\s*(\D+?)\s*\d+R', r_td.get_text(' ', strip=True))
                if sm_:
                    dm = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', f"20{sm_.group(1)}/{sm_.group(2)}/{sm_.group(3)}")
                    venue_txt = sm_.group(4)
                    ra = r_td.find('a')
                    rname = clean(ra.get_text()) if ra else rname
            dist_m = re.match(r'(芝|ダ|障)\D*(\d{3,4})', g('dist'))
            if not dm or not dist_m:
                continue
            ra = r_td.find('a', href=re.compile(r'/race/')) if r_td is not None else None
            rid_m = re.search(r'/race/([0-9a-zA-Z]{12})', ra.get('href', '')) if ra else None
            pc = re.match(r'(\d{2}\.\d)-(\d{2}\.\d)', g('pace'))
            pos_t = g('pos')
            pos = int(pos_t) if pos_t.isdigit() else 0
            m = to_float(g('m'))
            if m is not None and g('m').startswith('-'):
                m = -m
            if m is None:   # 着差なし（海外のレースなど）は着順から見積もる
                m = 0.0 if pos == 1 else (min(1.6, 0.2 * (pos - 1)) if pos > 0 else 9.9)
            tt = re.match(r'(\d):(\d{2}\.\d)', g('t'))
            cond = (g('cond') or '良')[:1]
            out.append({
                'd': f"{dm.group(1)}.{int(dm.group(2)):02d}.{int(dm.group(3)):02d}",
                'p': re.sub(r'\d', '', venue_txt) or '?', 'pos': pos, 'r': rname,
                'dist': dist_m.group(1) + dist_m.group(2), 'cond': cond if cond in '良稍重不' else '良',
                't': int(tt.group(1)) * 60 + float(tt.group(2)) if tt else 0,
                'f': int(to_float(g('f')) or 0) or 16, 'n': int(to_float(g('n')) or 0),
                'pop': int(to_float(g('pop')) or 0), 'j': g('j'), 'w': to_float(g('w')) or 0,
                'ps': g('ps'), 'l3': to_float(g('l3')) or 0, 'bw': g('bw'), 'win': g('win'), 'm': m,
                'rid': rid_m.group(1) if rid_m else None,
                'pf': float(pc.group(1)) if pc else None, 'pb': float(pc.group(2)) if pc else None,
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
# ハイレベル戦（出世レース）の見極め
#   ・名前で分かる出世レース ・勝ち時計が基準より大幅に速いレース ・対戦相手がその後に重賞で好走したレース
#   ハイレベル戦で4〜8着に負けたのは「相手が強かった」として着差を割り引き、3着以内なら加点
# ═════════════════════════════════════════
HIGH_LEVEL_NAMES = ('伏竜', '東風', '共同通信', '毎日杯', '白百合', 'ヒヤシンス', 'プリンシパル')
# パソコンで過去のデータから見つけたハイレベル戦の一覧（出走馬全員のその後と勝ち時計から判定。data/highlevel_races.json）
HL_TABLE = {}      # ハイレベル戦と判定したレース → [点数, 理由]
HL_LOW = {}        # 低レベル戦と判定したレース → [点数, 理由]
HL_SCORES = {}     # 判定したすべてのレース → 点数（0が平均、＋ほどハイレベル）
try:
    if os.environ.get('SYH_BACKTEST'):   # バックテストでは読まない（未来の結果が入っているため）
        raise RuntimeError('バックテストでは使いません')
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'highlevel_races.json'), encoding='utf-8') as _f:
        _hl = json.load(_f)
    HL_TABLE, HL_SCORES = _hl.get('races', {}), _hl.get('scores', {})
    HL_LOW = _hl.get('low', {})
    print(f"[highlevel] ハイレベル戦の一覧を読み込み：{len(HL_TABLE)}レース（点数あり{len(HL_SCORES)}レース）", flush=True)
except Exception as _e:
    print(f"[highlevel] ハイレベル戦の一覧なし: {_e}", flush=True)


HL_THRESHOLD = 1.0


def hl_lookup(h, l):
    """過去走のハイレベル戦の点数と内訳。バックテストでは「前日までの結果」で計算した値（h['_hlInfo']）を使う"""
    m = h.get('_hlInfo')
    if m is not None:
        return m.get(l.get('rid'), (None, ''))
    v = HL_TABLE.get(l.get('rid') or '')
    return (v[0], v[1]) if v else (None, '')


def highlevel_factors(race, horses):
    keys = {}
    for h in horses:
        for l in h.get('career', h['lines']):
            keys.setdefault(line_key(l), []).append(h)
    for h in horses:
        h['bHL'], h['hlNotes'], h['hlLines'] = 0.0, [], []
        # 新方式の材料：過去3走のハイレベル度（バックテストでは前日までの結果で計算した値が入っている）
        if 'hlScores' not in h:
            h['hlScores'] = [(HL_SCORES.get(l.get('rid') or ''), l['pos'])
                             for l in [x for x in h.get('career', h['lines']) if x['pos'] > 0 and is_flat(x)][:3] if l.get('rid')]
        b = 0.0
        for l in [x for x in h.get('career', h['lines']) if x['pos'] > 0 and is_flat(x)][:3]:
            lo = HL_LOW.get(l.get('rid') or '')
            if lo and l['pos'] <= 3:
                b -= 0.006
                h['hlNotes'].append(f"{short_date(l['d'])}{l['p']}{l['r']}は低レベル戦（{lo[1]}）。ここでの{l['pos']}着は割り引き")
        for l in [x for x in h.get('career', h['lines']) if x['pos'] > 0 and is_flat(x)][:5]:
            why = ''
            hs, hr = hl_lookup(h, l)
            if hs is not None and hs >= HL_THRESHOLD:
                why = f"ハイレベル戦：{hr}"
            name_hit = next((k for k in HIGH_LEVEL_NAMES if k in l['r']), None)
            if not why and name_hit:
                why = '出世レース'
            if not why and l.get('t') and l['p'] in JRA_PLACE.values():
                win_t = l['t'] - max(0, l['m'])
                ll = dict(l, t=win_t)
                si_w = speed_index(ll)
                if si_w is not None and ll.get('_stdReal') and si_w >= 10:   # 実データの基準がある時だけ
                    why = f'勝ち時計が基準より速い（指数{si_w:+.0f}）'
            if not why:
                shared = CLS[class_of_text(l['r'], l['p'])][1]
                for o in keys.get(line_key(l), []):
                    if o is h:
                        continue
                    later = [f for f in o.get('career', o['lines']) if f['pos'] > 0 and f['d'] > l['d'] and f['pos'] <= 2]
                    best = max((CLS[class_of_text(f['r'], f['p'])][1] for f in later), default=0)
                    if best >= max(5, shared + 1):   # その後に重賞で2着以内
                        why = f'対戦した{o["name"]}がその後重賞で好走'
                        break
            if not why:
                continue
            tag = f"{short_date(l['d'])}{l['p']}{l['r']}（{why}）"
            if l['pos'] <= 3:
                b += 0.006
                h['hlNotes'].append(f"{tag}で{l['pos']}着と好走")
            elif l['pos'] <= 8:
                h.setdefault('flowRelief', {})
                h['flowRelief'][line_key(l)] = min(h['flowRelief'].get(line_key(l), 1.0), 0.5)
                h['hlNotes'].append(f"{tag}で{l['pos']}着。相手が強かったので負けは度外視")
            h['hlLines'].append(l)
        h['bHL'] = clamp(b, -0.015, 0.02)


# ═════════════════════════════════════════
# 海外モード（凱旋門賞など）
# ═════════════════════════════════════════
# 凱旋門賞と同じパリロンシャン芝2400mで行われる前哨戦（netkeibaでは名前が途中で切れていることがある）
ARC_TRIALS = ('ニエル', 'ヴェルメイ', 'フォワ', 'パリ大賞')
# 凱旋門賞につながる主要G1（欧州の2000〜2400mと日本の大レース）
MAJOR_G1 = ('キングジョ', 'アイリッシ', 'インターナ', 'ヨークシャ', '英ダービ', '愛ダービ', '仏ダービ',
            '英オークス', '愛オークス', '仏オークス', 'ジャンロマ', 'サンクルー', 'バーデン大', 'ベルリン大',
            'プリンスオ', 'エクリプス', 'コロネーシ', '宝塚記念', '天皇賞', 'ジャパンC', '有馬記念', 'ドバイシー')


# 前哨戦の格（凱旋門賞との結びつき）。ヴェルメイユ賞（G1）＞フォワ賞・パリ大賞＞ニエル賞
ARC_TRIAL_W = {'ヴェルメイ': 1.0, 'フォワ': 0.9, 'パリ大賞': 0.9, 'ニエル': 0.6}

# 現地ブックメーカーの単勝オッズ（BookmakerFan掲載：ウィリアムヒル／bet365／1xBet、2026/9/29 14:00時点）
# レースIDごと・netkeibaの馬名で。Renderの環境変数 BOOK_ODDS_JSON で上書きできる
BOOK_ODDS = {
    '2026C8010105': {
        '_updated': '2026/9/29 14:00',
        'ダリズ': [2.5, 2.62, 2.7], 'モルティーズクロス': [5.5, 5.5, 6], 'カルパナ': [8, 8, 8],
        'ダイヤモンドネックレス': [9, 9, 21], 'サンダリングオン': [10, 9, 8], 'ヴァランディール': [17, 15, 15],
        'ミニーホーク': [17, 17, 21], 'ベイシティローラー': [26, 26, 17], 'ベンヴェヌートチェッリーニ': [26, 21, 21],
        'メイショウタバル': [34, 34, 26], 'フレンドリーソウル': [34, 34], 'サダッド': [41, 34, 26],
        'パールドマジェスティ': [41, 41, 50], 'サンリー': [51, 51, 21], 'アドマイヤテラ': [51, 51, 34],
        'ブライトライト': [101, 101, 34], 'アローイーグル': [101, 101, 65], 'タティコラム': [101, 101, 80],
        'ライトザゴースト': [151, 101, 80], 'チェスナットロケット': [201, 151, 100],
    },
}
try:
    _bo = json.loads(os.environ.get('BOOK_ODDS_JSON', '{}'))
    for _rid, _tab in _bo.items():
        BOOK_ODDS.setdefault(_rid, {}).update(_tab)
except Exception as _e:
    print(f"[book] BOOK_ODDS_JSON を読めません: {_e}", flush=True)


def book_probs(race, horses):
    """現地オッズ（複数社の中央値）から、出走馬の中での勝率を出す。表に無い馬は大穴（201倍）扱い"""
    tab = BOOK_ODDS.get(race.get('id'))
    if not tab:
        return None
    odds = {}
    for h in horses:
        v = tab.get(h['name'])
        if v:
            vs = sorted(v)
            odds[h['n']] = vs[len(vs) // 2]
        else:
            odds[h['n']] = None
    inv = {n: 1 / (o or 201) for n, o in odds.items()}
    tot = sum(inv.values())
    race['bookUpdated'] = tab.get('_updated', '')
    return {n: (inv[n] / tot, odds[n]) for n in inv}


def abroad_factors(race, horses):
    """海外モード：前哨戦、同じコースの実績、ゲート番、年齢、日本馬の馬体重と馬場、現地オッズ"""
    for h in horses:
        h['bAbroad'], h['abroadNotes'] = 0.0, []
    if not race.get('abroad'):
        return
    heavy = race['going'] in ('重', '不良')
    N = len(horses)
    bp = book_probs(race, horses)
    race['bookProbs'] = bp
    for h in horses:
        b, notes = 0.0, []
        ls = [l for l in h['lines'] if l['pos'] > 0]
        # 前哨戦・主要G1（前走・2走前）
        for i, l in enumerate(ls[:2]):
            w = 1.0 if i == 0 else 0.6
            ago = '前走' if i == 0 else '2走前'
            tk = next((k for k in ARC_TRIAL_W if k in l['r']), None)
            if tk:
                add = {1: 0.02, 2: 0.012, 3: 0.006}.get(l['pos'], 0) * w * ARC_TRIAL_W[tk]
                if add:
                    b += add
                    notes.append(f"{ago}の同舞台の前哨戦{l['r']}で{l['pos']}着")
            elif any(k in l['r'] for k in MAJOR_G1):
                add = (0.012 if l['pos'] == 1 else 0.006 if l['pos'] <= 3 else 0) * w
                if add:
                    b += add
                    notes.append(f"{ago}の主要G1{l['r']}で{l['pos']}着")
        # 同じ競馬場・距離での実績（勝ち数が多いほど）
        cs = h.get('career', h['lines'])
        wins_here = sum(1 for l in cs if l['p'] == race['venue'] and l['pos'] == 1)
        top3_here = any(l['p'] == race['venue'] and abs(dist_num(l['dist']) - race['dist']) <= 100 and 0 < l['pos'] <= 3 for l in cs)
        if wins_here or top3_here:
            b += min(0.012, 0.003 * wins_here) + (0.004 if top3_here else 0)
            notes.append(f"{race['venue']}で{wins_here}勝" + ("、同距離で3着以内の実績あり" if top3_here else ''))
        # ゲート番（凱旋門賞：2019年以降、1〜5番が好成績、11番以降は不振）
        g = h.get('gate')
        if g and not race.get('provisional') and N >= 12:
            if g <= 5:
                b += 0.012; notes.append(f"{g}番ゲートの内枠（内が有利な傾向）")
            elif g >= 11:
                b -= 0.012; notes.append(f"{g}番ゲートの外枠（外は不振の傾向）")
        # 年齢（4歳が最も好成績、6歳以上は3着以内なし。3歳は斤量増以降やや苦戦）
        a = h.get('age')
        if a:
            if a >= 6:
                b -= 0.02; notes.append(f"{a}歳（6歳以上は近年3着以内なし）")
            elif a == 4:
                b += 0.006; notes.append('4歳（年齢別で最も好成績）')
        # 日本馬：馬体重（480kg以上は好走なし）と重い馬場
        if h.get('area') in ('日本', '美浦', '栗東'):
            bw_l = next((l for l in h['lines'] if re.match(r'^\d{3}', l.get('bw') or '')), None)
            bw = int(bw_l['bw'][:3]) if bw_l else None
            if bw and bw >= 480:
                b -= 0.015; notes.append(f"日本馬で馬体重{bw}kg（480kg以上の日本馬は凱旋門賞で好走なし）")
            elif bw and bw <= 469:
                b += 0.005; notes.append(f"日本馬で馬体重{bw}kgの小柄な馬（好走は小柄な馬に集中）")
            if heavy:
                b -= 0.01; notes.append('日本馬は重い欧州の馬場で苦戦してきた歴史がある')
        # 現地オッズ（欧州の市場の評価）。出走頭数に対する勝率の高さで加点・減点
        if bp and h['n'] in bp:
            p, o = bp[h['n']]
            h['pBook'], h['oBook'] = p, o
            add = clamp(0.012 * math.log(max(p * N, 1e-3)), -0.02, 0.03)
            b += add
            notes.append(f"現地オッズ{o}倍（現地で見た勝率{p * 100:.1f}%）" if o else '現地オッズに名前なし（大穴扱い）')
        h['bAbroad'] = clamp(b, -0.05, 0.07)
        h['abroadNotes'] = notes


# ═════════════════════════════════════════
# 地方競馬の格付け（競馬口コミダービー「地方競馬会場の格付け表」を参考に整理）
#   ・競馬場のレベル：S＝南関東、A＝門別・兵庫・高知、B＝名古屋・岩手、C＝金沢・笠松・佐賀
#   ・地区ごとのクラスを共通の物差し（Tier）にそろえる。中央競馬 ＞ 地方競馬 になるよう値を置く
#     （中央：未勝利1・1勝2・2勝3・3勝4・OP4.5・G3以上5〜7）
# ═════════════════════════════════════════
CIRCUIT = {'大井': '南関東', '船橋': '南関東', '川崎': '南関東', '浦和': '南関東', '門別': '北海道',
           '園田': '兵庫', '姫路': '兵庫', '高知': '高知', '名古屋': '名古屋', '笠松': '笠松',
           '盛岡': '岩手', '水沢': '岩手', '金沢': '金沢', '佐賀': '佐賀', '帯広': 'ばんえい'}
VENUE_RANK = {'大井': 'S', '船橋': 'S', '川崎': 'S', '浦和': 'S', '門別': 'A', '園田': 'A', '姫路': 'A',
              '高知': 'A', '名古屋': 'B', '盛岡': 'B', '水沢': 'B', '金沢': 'C', '笠松': 'C', '佐賀': 'C'}
RANK_NUM = {'S': 4, 'A': 3, 'B': 2, 'C': 1}
# Tier（S＝交流重賞でも通用〜7＝最下級）→ 物差しの値。いちばん上でも中央の3勝クラス(4)未満
TIER_VAL = {0: 3.8, 1: 3.3, 2: 2.9, 3: 2.5, 4: 2.1, 5: 1.7, 6: 1.35, 7: 1.1}
# 地区ごとの「クラス → Tier」対応（記事の対応表をもとに。中間は .5）
CIRCUIT_TIER = {
    '南関東': {'A1': 0, 'A2': 1, 'B1': 2, 'B2': 3, 'B3': 4, 'C1': 5, 'C2': 6, 'C3': 7},
    '北海道': {'A1': 0, 'A2': 1, 'A3': 2, 'A4': 2, 'B1': 3, 'B2': 4, 'B3': 4, 'B4': 4.5, 'C1': 5, 'C2': 5.5, 'C3': 6, 'C4': 6},
    '兵庫':   {'A1': 0, 'A2': 1, 'B1': 2, 'B2': 3, 'C1': 4, 'C2': 5, 'C3': 6},
    '高知':   {'A': 1, 'B': 2, 'C1': 3.5, 'C2': 5, 'C3': 6.5},
    '名古屋': {'A': 2, 'B': 4, 'C': 5.5},
    '笠松':   {'A': 3.5, 'B': 5, 'C': 6.5},
    '岩手':   {'A': 2.5, 'B1': 4, 'B2': 5, 'C1': 6, 'C2': 7},
    '金沢':   {'A1': 2, 'A2': 3, 'B1': 4, 'B2': 5, 'C1': 6, 'C2': 7},
    '佐賀':   {'A1': 2, 'A2': 3, 'B': 4.5, 'B1': 4.5, 'C1': 6, 'C2': 7},
}
# 地区の重賞・オープンの目安（Tier）
CIRCUIT_TOP = {'南関東': (0, 0.5), '北海道': (0.5, 1), '兵庫': (0.5, 1), '高知': (1, 1.5), '名古屋': (1.5, 2),
               '岩手': (1.5, 2), '笠松': (2.5, 3), '金沢': (2, 2.5), '佐賀': (2, 2.5)}
_CLS_IDX = {}


def tier_value(tier):
    lo = int(tier)
    hi = min(7, lo + 1)
    return TIER_VAL[lo] + (TIER_VAL[hi] - TIER_VAL[lo]) * (tier - lo)


def cls_index(label, val):
    """地区つきのクラス（例：大井B1）を CLS に登録して番号を返す"""
    key = (label, round(val, 2))
    if key not in _CLS_IDX:
        CLS.append((label, round(val, 2)))
        _CLS_IDX[key] = len(CLS) - 1
    return _CLS_IDX[key]


def nar_class_tiered(t, venue):
    """地方のレース名・条件から、地区ごとのクラスを共通の物差しの値にする"""
    circuit = CIRCUIT.get(venue)
    if circuit in (None, 'ばんえい'):
        return nar_class(t)
    t = norm_digits(t).translate(str.maketrans('ＡＢＣＤＳＩＯＰ', 'ABCDSIOP'))
    t = re.sub(r'[ー－−‐―]', '-', t)   # 長音記号やダッシュを「-」にそろえる
    top, op = CIRCUIT_TOP.get(circuit, (2, 2.5))
    if re.search(r'S(?:1|Ⅰ|I)(?![IⅠ0-9])', t):
        return cls_index(f'{venue}重賞SI', tier_value(max(0, top - 0.5)) + 0.2)
    if re.search(r'S(?:2|Ⅱ|II)(?![IⅠ0-9])|S(?:3|Ⅲ|III)|重賞', t):
        return cls_index(f'{venue}重賞', tier_value(top))
    if re.search(r'オープン|OP|特別選抜', t):
        return cls_index(f'{venue}OP', tier_value(op))
    if re.search(r'2歳|新馬|未勝利|認定|能力', t) and not re.search(r'[ABC]\s*\d', t):
        return cls_index(f'{venue}2歳', 1.5 if circuit == '北海道' else 1.4 if circuit == '南関東' else 1.1)
    table = CIRCUIT_TIER.get(circuit, {})
    for k in ('A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4', 'C1', 'C2', 'C3', 'C4'):
        if re.search(r'(?<![A-Z])' + k[0] + r'\s*-?\s*' + k[1], t):
            tier = table.get(k)
            if tier is None:   # その地区に無い細分（例：名古屋のB1）は、同じ文字の段で代用
                tier = table.get(k[0], next((v for kk, v in table.items() if kk[0] == k[0]), 4))
            return cls_index(f'{venue}{k}', tier_value(tier))
    for k in ('A', 'B', 'C'):
        if re.search(r'(^|[^A-Z])' + k + r'(?=\s*-|\s*組|\s|$|[^A-Z0-9])', t):
            tier = table.get(k)
            if tier is None:
                vals = [v for kk, v in table.items() if kk[0] == k]
                tier = sum(vals) / len(vals) if vals else 4
            return cls_index(f'{venue}{k}', tier_value(tier))
    if re.search(r'(^|[^A-Z])D(?![A-Z])', t):
        return cls_index(f'{venue}D', 1.0)
    # 読めないときは、競馬場のレベルに合わせた中くらいの値
    return cls_index(f'{venue}一般', tier_value({'S': 4, 'A': 4.5, 'B': 5, 'C': 5.5}.get(VENUE_RANK.get(venue), 5)))


def is_jra_venue(p):
    return p in JRA_PLACE.values()


def class_move_factors(race, horses):
    """降級・昇級・転入（地区のレベル差、中央からの転入）を評価する（地方のレースのみ）"""
    for h in horses:
        h['bMove'], h['moveNotes'] = 0.0, []
    if 'nar.' not in race.get('base', '') or race.get('banei'):
        return
    today_v = CLS[race['clsIdx']][1]
    venue = race['venue']
    for h in horses:
        ls = [l for l in h['lines'] if l['pos'] > 0 or l.get('pos') == 0]
        if not ls:
            continue
        l0 = ls[0]
        last_v = CLS[class_of_text(l0['r'], l0['p'])][1]
        b, notes = 0.0, []
        if h.get('area') in ('美浦', '栗東'):
            pass   # 中央所属の馬の遠征（交流重賞など）。転入ではないので加点しない
        elif is_jra_venue(l0['p']):
            b += 0.01
            notes.append(f"中央からの転入（前走{l0['p']}{l0['r']}）。中央＞地方の力関係で上位")
        elif CIRCUIT.get(l0['p']) and CIRCUIT.get(l0['p']) != CIRCUIT.get(venue):
            r0, r1 = RANK_NUM.get(VENUE_RANK.get(l0['p']), 2), RANK_NUM.get(VENUE_RANK.get(venue), 2)
            if r0 > r1:
                b += 0.006 * (r0 - r1)
                notes.append(f"レベルの高い{l0['p']}からの転入（{VENUE_RANK.get(l0['p'])}→{VENUE_RANK.get(venue)}ランク）")
            elif r0 < r1:
                b -= 0.006 * (r1 - r0)
                notes.append(f"{l0['p']}からの転入で、相手が強くなる（{VENUE_RANK.get(l0['p'])}→{VENUE_RANK.get(venue)}ランク）")
        else:
            if last_v > today_v + 0.15:
                b += 0.008
                notes.append(f"降級初戦（前走{CLS[class_of_text(l0['r'], l0['p'])][0]}→今回{CLS[race['clsIdx']][0]}）。メンバー上位の可能性")
            elif l0['pos'] == 1 and last_v < today_v - 0.15:
                b -= 0.006
                notes.append(f"昇級初戦（前走{CLS[class_of_text(l0['r'], l0['p'])][0]}を勝ち上がり）。クラスの壁に注意")
        h['bMove'] = clamp(b, -0.015, 0.02)
        h['moveNotes'] = notes


# ═════════════════════════════════════════
# 地方競馬場ごとのコース特性（馬券名人養成プログラム「地方競馬場の特徴と攻略法」を参考に整理）
#   style：脚質ごとの有利不利（＋有利／－不利）、draw：＋内枠有利／－外枠有利、
#   chalk：＋堅い決着が多い／－荒れやすい、jockey：騎手の腕が結果に出やすいほど大きく
# ═════════════════════════════════════════
FRONT = {'逃': 0.9, '先': 0.7, '差': -0.4, '追': -0.8}      # 逃げ・先行がはっきり有利
FRONT_MID = {'逃': 0.5, '先': 0.6, '差': -0.1, '追': -0.6}  # 前有利だが好位も届く
NEUTRAL = {'逃': 0, '先': 0, '差': 0, '追': 0}


def nar_course(race):
    v, d = race['venue'], race['dist']
    P = {
        '大井': dict(style={'逃': 0.4, '先': 0.2, '差': 0, '追': -0.1} if d <= 1400 else NEUTRAL, draw=-0.2, chalk=-0.5,
                   note='外回りは大きく脚質の有利不利は小さめ。短距離は逃げ馬が強く、多頭数で荒れやすい'),
        '船橋': dict(style={'逃': 0.3, '先': 0.3, '差': 0, '追': -0.3}, draw=-0.5, chalk=0.3,
                   note='スパイラルカーブで能力通りの決着が多く、内枠はやや不利で外枠がやや有利'),
        '川崎': dict(style=FRONT_MID, draw=0.9, chalk=-0.2, jockey=1.3,
                   note='コーナーが特にきつく内枠が有利。最初のコーナーまでの位置取りで展開が決まる'),
        '浦和': dict(style={'逃': 0.1, '先': 0.6, '差': 0.3, '追': -0.7}, draw=0.3, chalk=0.0,
                   note='1周1200mの小回り。3〜4角から叩き合いになり、好位から運べる自在型が有利'),
        '門別': dict(style={'逃': 0, '先': 0, '差': 0.3, '追': 0.4}, draw=0, chalk=-0.3 if race.get('N', 0) >= 12 else 0,
                   note='外回りは直線が長く差し・追込も届く。枠の有利不利は小さく、多頭数は荒れやすい',
                   sire={'サウスヴィグラス': 0.006} if d <= 1400 else {}),
        '盛岡': dict(style=({'逃': 0, '先': 0, '差': 0.3, '追': 0.2} if d >= 2000 else NEUTRAL), draw=0, chalk=0.3,
                   note='コーナーがゆったりで坂があり、能力通りの決着が多い。地方で唯一芝コースがある'),
        '水沢': dict(style=FRONT, draw=0, chalk=0.1, jockey=1.2,
                   note='平坦な小回りで逃げ・先行が有利。長い距離も3角で前にいる馬が勝ちやすい'),
        '金沢': dict(style=(FRONT if d <= 1500 else FRONT_MID), draw=0, chalk=0.2 if d <= 1500 else 0.4,
                   note='楕円形の小回りで先行有利。距離が延びるほど粘れる馬が減り、実績の比較が大事'),
        '笠松': dict(style={'逃': 0.4, '先': 0.8, '差': -0.2, '追': -0.7},
                   draw=(0 if d in (1400,) or d >= 1900 else 0.3), chalk=0.1,
                   note='直線が短く、好位から抜け出す形が有利。1400mや1900m以上は最内枠が有利とは限らない'),
        '名古屋': dict(style=FRONT, draw=0, chalk=0.6,
                    note='直線が短く逃げ・先行が大きく有利。人気馬が堅く決まりやすい（2022年に弥富の新コースへ移転済み）'),
        '園田': dict(style=({'逃': 0.2, '先': 0.3, '差': 0.2, '追': -0.1} if d == 1400 else FRONT_MID), draw=0.2, chalk=0.0,
                   note='小回りで内枠の逃げ・先行が有利。1400mは捲りや差しにも注意'),
        '姫路': dict(style=FRONT, draw=0, chalk=0.4,
                   note='スパイラルカーブがなく、先行型が圧倒的に有利'),
        '高知': dict(style=(FRONT if d <= 1400 else {'逃': 0.3, '先': 0.3, '差': 0.2, '追': 0}),
                   draw=(-0.4 if d <= 1400 else 0.3), chalk=0.3, jockey=1.3,
                   note='内側の砂が深く各馬が外を回る。短距離は先行・やや外枠、1600m以上は内〜中枠が有利'),
        '佐賀': dict(style={'逃': 0.9, '先': 0.5, '差': -0.3, '追': -0.8}, draw=-0.5, chalk=0.1, jockey=1.2,
                   note='直線が短い下り坂で逃げ有利・追込不利。内側の砂が深く、外枠がやや有利',
                   sire={'サウスヴィグラス': 0.006} if d <= 1400 else {}),
        '帯広': dict(style=NEUTRAL, draw=0, chalk=-0.3,
                   note='ばんえい。障害を越える力勝負で、負担重量に対する実績を重視'),
    }.get(v)
    if P:
        P.setdefault('jockey', 1.0)
        P.setdefault('sire', {})
    return P


def course_factors(race, horses):
    """地方競馬場のコース特性を、脚質・枠・血統に反映する"""
    race['N'] = len(horses)
    P = nar_course(race) if 'nar.' in race.get('base', '') else None
    race['course'] = P
    race['chalk'] = P['chalk'] if P else 0
    if P and race['venue'] == '高知' and 'ファイナル' in race.get('name', ''):
        race['chalk'] = -0.8   # 不振馬どうしの敗者復活戦。着順が入れ替わりやすい
        P = dict(P, note=P['note'] + '。一発逆転ファイナルは不振馬どうしで荒れやすく、調子と展開を重視')
        race['course'] = P
    for h in horses:
        b = 0.0
        if P:
            b += P['style'].get(h['st'], 0) * 0.012
            if not race.get('banei'):
                b += P['draw'] * 0.008 * (0.5 - draw_pos(h, len(horses))) * 2
            b += P['sire'].get(h.get('sire'), 0)
            # 水沢：盛岡で先行して失速した馬は、平坦な水沢で巻き返しやすい
            if race['venue'] == '水沢' and h['lines']:
                l0 = h['lines'][0]
                ps_ = [int(x) for x in re.findall(r'\d+', l0.get('ps') or '')]
                if l0['p'] == '盛岡' and l0['pos'] >= 4 and ps_ and (ps_[0] - 1) / max(1, (l0.get('f') or 12) - 1) <= 0.35:
                    b += 0.006
                    h['courseNote'] = '前走は盛岡で先行して失速。平坦な水沢での巻き返しに期待'
        h['bCourse'] = b


# ═════════════════════════════════════════
# 近走の中身（展開に逆らった好走を見抜く）
# ═════════════════════════════════════════
FLOW_W = [1.0, 0.7, 0.5]          # 前走・2走前・3走前の重み


def pace_label(res, l):
    """そのレースのペース（H/M/S）。結果ページの表示を優先し、無ければ前後半3Fから推定"""
    if res and res.get('pace'):
        return res['pace']
    if l.get('pf') and l.get('pb'):
        d, surf = dist_num(l['dist']), l['dist'][:1]
        exp = (2.6 if d <= 1400 else 1.4 if d <= 1800 else 0.4) if surf == 'ダ' else (1.2 if d <= 1400 else 0.3 if d <= 1800 else -0.4)
        dev = (l['pb'] - l['pf']) - exp
        return 'H' if dev >= 0.8 else 'S' if dev <= -1.2 else 'M'
    return None


def flow_analysis(race, horses):
    """前走から3走分、「ハイペースや差し決着を先行して粘った」「スローや前残りを後ろから追い込んだ」など、
    流れに逆らった好走を見つけて評価する"""
    for h in horses:
        h['bFlow'], h['flowNotes'], h['flowRelief'], h['flowLastStrong'] = 0.0, [], {}, False
    if race.get('banei'):
        return
    targets = {}
    for h in horses:
        if not h.get('careerOk'):
            continue
        h['_flowLines'] = [l for l in h['career'] if l['pos'] > 0 and is_flat(l)][:3]
        for l in h['_flowLines']:
            if l.get('rid') and not is_abroad_id(l['rid']):   # 海外のレースは結果ページが無い
                targets[l['rid']] = None
    with ThreadPoolExecutor(max_workers=8) as ex:
        for rid, res in zip(list(targets), ex.map(lambda r: _safe(parse_result_full, r), list(targets))):
            targets[rid] = res
    rcv = CLS[race['clsIdx']][1]
    for h in horses:
        score = 0.0
        for i, l in enumerate(h.get('_flowLines', [])):
            res = targets.get(l.get('rid'))
            N = (res or {}).get('N') or l.get('f') or 16
            me = next((r for r in (res or {}).get('rows', []) if r['n'] == l.get('n')), None)
            ps_ = [int(x) for x in re.findall(r'\d+', l.get('ps') or '')]
            c1 = (me or {}).get('c1') or (ps_[0] if ps_ else None)
            c4 = (me or {}).get('c4') or (ps_[-1] if ps_ else None)
            if not c1 or N < 5:
                continue
            e1, e4 = (c1 - 1) / (N - 1), (c4 - 1) / (N - 1)
            pace = pace_label(res, l)
            rows = (res or {}).get('rows', [])
            top3 = [r for r in rows if r['pos'] <= 3 and r.get('c4')]
            sashi = len(top3) >= 3 and sum(1 for r in top3 if (r['c4'] - 1) / (N - 1) > 0.35) >= 2
            zenzan = len(top3) >= 3 and sum(1 for r in top3 if (r['c4'] - 1) / (N - 1) <= 0.25) >= 2
            good_pos = l['pos'] <= max(5, round(N * 0.35))
            q, why, harsh = 0.0, '', ''
            # ① 前に厳しい流れを先行して粘った
            if (pace == 'H' or sashi) and e1 <= 0.35:
                harsh = 'ハイペースや差し決着を先行'
                front = [r for r in rows if r.get('c1') and (r['c1'] - 1) / (N - 1) <= 0.35]
                best_front = bool(front) and l['pos'] <= min(r['pos'] for r in front)
                q = 1.0 if l['pos'] <= 3 else 0.85 if (best_front and good_pos) else 0.6 if good_pos else 0.5 if best_front else 0
                ctx = '・'.join(x for x in (('ハイペース' if pace == 'H' else ''), ('差し決着' if sashi else '')) if x)
                why = f"{ctx}を{c1}番手から先行して{l['pos']}着" + ('（先行勢で最先着）' if best_front else '')
                q *= 1.2 if (pace == 'H' and sashi) else 1.0
            # ② 後ろに厳しい流れを追い込んだ
            elif (pace == 'S' or zenzan) and e4 >= 0.5:
                harsh = 'スローや前残りを後方から'
                back = [r for r in rows if r.get('c4') and (r['c4'] - 1) / (N - 1) >= 0.5]
                best_back = bool(back) and l['pos'] <= min(r['pos'] for r in back)
                q = 1.0 if l['pos'] <= 3 else 0.7 if good_pos else 0.5 if (best_back and l['m'] <= 0.6) else 0
                ctx = '・'.join(x for x in (('スロー' if pace == 'S' else ''), ('前残り' if zenzan else '')) if x)
                why = f"{ctx}の流れを{c4}番手から追い込んで{l['pos']}着" + ('（後方勢で最先着）' if best_back else '')
                q *= 1.2 if (pace == 'S' and zenzan) else 1.0
            # ③ メンバー上位の上がりで掲示板
            if q < 0.5 and rows and good_pos and me and me.get('l3'):
                l3s = sorted(r['l3'] for r in rows if r.get('l3'))
                if l3s and l3s.index(me['l3']) <= 1:
                    q, why = 0.5, f"上がり{me['l3']}（メンバー{l3s.index(me['l3']) + 1}位）で{l['pos']}着"
            ago = ['前走', '2走前', '3走前'][i]
            if q <= 0:
                # 流れが向かなかった負けは「展開負け」として着差を少し割り引く（度外視）
                if harsh:
                    h['flowRelief'][line_key(l)] = 0.7
                    h['flowNotes'].append(f"{ago}{short_date(l['d'])}{l['p']}{l['dist']}：{harsh}で{l['pos']}着の展開負け（着差を割り引き）")
                continue
            cf = clamp(CLS[class_of_text(l['r'], l['p'])][1] / max(rcv, 1), 0.8, 1.25)
            score += q * cf * FLOW_W[i]
            h['flowNotes'].append(f"{ago}{short_date(l['d'])}{l['p']}{l['dist']}：{why}")
            if q >= 0.5:
                h['flowRelief'][line_key(l)] = 0.4
            if i == 0 and q >= 0.85:
                h['flowLastStrong'] = True   # 前走が特に中身の濃い内容
        h['bFlow'] = min(score, 1.6) / 1.6 * 0.06
        h.pop('_flowLines', None)


def eff_m(h, l):
    """着差の評価用の値。流れに逆らって好走したレースは、負けた着差を軽く見る"""
    m = max(0, l['m']) * h.get('flowRelief', {}).get(line_key(l), 1.0)
    return m * 0.7 if l.get('p') in NAR_VENUES else m   # 地方は少頭数・力差で着差が開きやすい


# ═════════════════════════════════════════
# 展開予想（誰が逃げるか・隊列）
# ═════════════════════════════════════════
GROUPS = ['逃げ', '先行', '中団', '後方']
STYLE_RATIO = {'逃': 0.03, '先': 0.22, '差': 0.6, '追': 0.85}


def predict_positions(race, horses):
    """近走の「最初のコーナーの位置」から、各馬の位置取りと隊列、ペースを予想する"""
    N = len(horses)
    if race.get('banei') or race.get('abroad'):   # ばんえい・海外は通過順の情報が無いので隊列は出さない
        race.update({'lineup': None, 'leader': None, 'hana': '', 'strongLeaders': 0, 'pace': 'base'})
        for h in horses:
            h['bPos'], h['posGroup'] = 0, None
        return
    wts = [1, .8, .6, .5, .4]
    for h in horses:
        src = [l for l in h['lines'] if l['pos'] > 0 and is_flat(l) and l.get('ps')] \
            or [l for l in h.get('career', []) if l['pos'] > 0 and is_flat(l) and l.get('ps')]
        rs, ws, leads = [], [], 0
        for i, l in enumerate(src[:5]):
            first = re.match(r'(\d+)', l['ps'])
            if not first:
                continue
            p1, f = int(first.group(1)), max(2, l.get('f') or 16)
            rs.append((p1 - 1) / (f - 1))
            ws.append(wts[i])
            leads += p1 == 1
        if rs:
            ratio = sum(r * w for r, w in zip(rs, ws)) / sum(ws)
            h['leadRate'] = leads / len(rs)
            h['posFrom'] = f"近{len(rs)}走の最初のコーナー平均{ratio * (N - 1) + 1:.1f}番手相当"
        else:
            ratio = STYLE_RATIO.get(h['st'], 0.5)
            h['leadRate'] = 1.0 if h['st'] == '逃' else 0.0
            h['posFrom'] = 'netkeibaの脚質から推定' if h['st'] in STYLE_RATIO else '情報なし'
        # 内枠の方が前に行きやすい
        ratio += 0.06 * (draw_pos(h, N) - 0.5)
        ratio -= float(h.get('jkFront') or 0.0)       # 前に行かせる騎手なら前へ、控える騎手なら後ろへ
        h['earlyRatio'] = clamp(ratio, 0, 1)

    order = sorted(horses, key=lambda h: (h['earlyRatio'], -h['leadRate']))
    strong = [h for h in order if h['leadRate'] >= 0.4 or h['earlyRatio'] <= 0.08]
    n_front, n_mid = max(2, round(N * 0.33)), max(3, round(N * 0.7))
    for i, h in enumerate(order):
        h['posGroup'] = '逃げ' if i == 0 else '先行' if i < n_front else '中団' if i < n_mid else '後方'
        if h['st'] == '?':   # 脚質の表示が無い馬は予想位置から補う
            h['st'] = {'逃げ': '逃', '先行': '先', '中団': '差', '後方': '追'}[h['posGroup']]
    race['lineup'] = {g: [h for h in order if h['posGroup'] == g] for g in GROUPS}
    race['leader'] = order[0] if order else None

    # ハナ争いか、単騎逃げか
    if len(strong) >= 2:
        race['hana'] = f"ハナ争い：{'・'.join(str(h['n']) for h in strong[:3])}番"
        est = 'fast' if len(strong) >= 3 or len(race['lineup']['先行']) >= N * 0.4 else 'base'
    elif len(strong) == 1:
        race['hana'] = f"{strong[0]['n']}番の単騎逃げ濃厚"
        est = 'slow'
    else:
        race['hana'] = f"決め手を欠く先手争い（{order[0]['n']}番が押し出される形）" if order else ''
        est = 'slow'
    race['strongLeaders'] = len(strong)
    if race.get('pace_from') != 'netkeiba展開予想':
        race['pace'] = est
        race['pace_from'] = '近走の位置取りから予想'

    # 単騎逃げは残りやすく、ハナ争いは前の馬が共倒れしやすい
    for h in horses:
        b = 0
        if len(strong) == 1 and h is strong[0]:
            b += 0.01
        elif len(strong) >= 2 and h in strong[:3]:
            b -= 0.006
        h['bPos'] = b


# ═════════════════════════════════════════
# レースシミュレーションの結果（パソコンで1000回走らせた結果。data/simulations.json。海外と重賞のみ）
# ═════════════════════════════════════════
SIMS = {}
try:
    if os.environ.get('SYH_BACKTEST'):
        raise RuntimeError('バックテストでは使いません')
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'simulations.json'), encoding='utf-8') as _f:
        SIMS = json.load(_f)
    print(f"[sim] シミュレーションの結果を読み込み：{len(SIMS)}レース", flush=True)
except Exception as _e:
    print(f"[sim] シミュレーションの結果なし: {_e}", flush=True)


def apply_simulation(race, horses):
    """シミュレーションで一番多かった展開（ペース・逃げ馬・隊列）を、このレースの展開予想として使う"""
    sim = SIMS.get(race.get('id'))
    if not sim:
        return False
    by_n = {h['n']: h for h in horses}
    for h in horses:
        hs = sim['horses'].get(str(h['n']))
        if not hs:
            continue
        h['sim'] = hs
        h['earlyRatio'] = hs['early']
        h['posGroup'] = hs['group']
        h['bPos'] = 0.0
    to_bot = {'先頭': '逃げ', '先団': '先行', '中団': '中団', '後方': '後方'}   # シミュレーションの呼び方 → Botの呼び方
    for h in horses:
        if h.get('sim'):
            h['posGroup'] = to_bot.get(h['sim']['group'], h['sim']['group'])
    order = sorted([h for h in horses if h.get('sim')], key=lambda h: h['sim']['early'])
    race['lineup'] = {g: [h for h in order if h['posGroup'] == g] for g in GROUPS}
    ld = by_n.get(sim['leader'])
    race['leader'] = ld
    share = sim['pace_share'].get(sim['pace'], 0)
    race['pace'] = sim['pace']
    race['pace_from'] = f"シミュレーション{sim['runs']}回で最多（{share * 100:.0f}%）"
    race['hana'] = (f"シミュ{sim['runs']}回：{ld['n']}番が逃げ（{sim['leader_share'] * 100:.0f}%）・"
                    f"{PACE_LABEL[sim['pace']]}ペース{share * 100:.0f}%") if ld else ''
    if ld and sim['pace'] == 'slow' and sim['leader_share'] >= 0.5:
        ld['bPos'] = 0.01     # 単騎で逃げられそう
    race['nige'] = sum(1 for h in horses if h.get('sim') and h['sim']['group'] == '先頭')
    race['simulation'] = sim
    return True


# ═════════════════════════════════════════
# まとめ：LINEに返す文章を作る
# ═════════════════════════════════════════
def analyze(text):
    race_id, base = parse_input(text)
    if not race_id:
        return None, "URLからレースIDを読み取れませんでした。"
    abroad = is_abroad_id(race_id)
    page = 'shutuba_past_abroad.html' if abroad else 'shutuba_past.html'
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_page = ex.submit(fetch_soup, f"{base}/race/{page}?race_id={race_id}")
        f_odds = ex.submit(lambda: {} if abroad else fetch_win_odds(race_id, base))
        soup = f_page.result()
        odds = f_odds.result()
    race = parse_race_info(soup, race_id, base)
    all_h = [h for h in parse_shutuba_past(soup) if not h['cancelled']]
    if abroad and all_h and not any(h['n'] for h in all_h):
        # 出馬投票・枠順の確定前は馬番が空欄。仮の番号（並び順）を振る
        for i, h in enumerate(all_h, 1):
            h['n'] = i
        race['provisional'] = True
    horses = [h for h in all_h if h['n']]
    ov = going_override(text)
    if ov:
        race['going'], race['going_known'], race['going_manual'] = ov, True, True
    if not horses:
        return None, "出走馬を読み取れませんでした。ページの形式が変わった可能性があります。"
    if 'nar.' in base and any(not h.get('jid') for h in horses):
        _safe(fill_nar_jockeys, race_id, base, horses)
    if not odds:   # APIで取れなければページの表示から
        odds = {h['n']: h['oddsPage'] for h in horses if h.get('oddsPage')}

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
    if MODEL:   # 騎手の前に行く癖（学習データから作った表）。展開予想の前にのせる
        for h in horses:
            h['jkFront'] = MODEL.get('jockey_front', {}).get(h.get('jid') or '', 0.0)
    t_start = time.time()
    early = {'tb': BG.submit(_safe, track_bias, race, race_id, race['date']),
             'jk': {j: BG.submit(_safe, fetch_jockey_stats, j) for j in {h['jid'] for h in horses if h.get('jid')}},
             'tr': BG.submit(_safe, fetch_oikiri, race_id) if not (abroad or 'nar.' in base) else None}
    race['_early'] = early
    load_careers(race, horses)
    print(f"[time] 全成績 {time.time() - t_start:.1f}秒", flush=True)
    flow_analysis(race, horses)
    predict_positions(race, horses)
    race['nige'] = sum(1 for h in horses if h['st'] == '逃')
    apply_simulation(race, horses)      # シミュレーションの結果があれば、その展開を使う（海外・重賞）

    highlevel_factors(race, horses)
    auto_heuristics(race, horses)
    extra_factors(race, horses, soup, race_id)
    arr = ranked(race, horses, odds)
    if race.get('bookProbs'):
        for h in arr:
            if h.get('pBook') is not None:
                h['evModel'] = h.get('ev')
                h['ev'] = h['pBook'] * h['o'] if h.get('o') else None
    return (race, arr), None


def format_reply(race, arr):
    pct, label = chaos_info(arr)
    N = len(arr)
    out = [f"🐎 シェイクユアハート",
           f"{race['venue']}{race['R']}R {race['name']}" + ("" if CLS[race['clsIdx']][0] in race['name'] else f"（{CLS[race['clsIdx']][0]}）"),
           f"{race['surf']}{race['dist']}m／{race['going']}{'' if race['going_known'] else '（未発表→良で計算）'}／{N}頭",
           f"波乱度 {pct}%（{label}）",
           *(["※枠順の確定前のため、馬番は仮の番号（出馬表の並び順）です"] if race.get('provisional') else []),
           f"判定：{verdict(arr)}", ""]

    # 展開・相手関係
    pace = race['pace']
    tenkai = {'fast': '逃げ馬が多くペースが上がりやすい→差し・追込有利',
              'slow': '逃げ馬が少なくスローになりやすい→前に行く馬が有利',
              'base': '平均ペース想定→脚質の有利不利は小さめ'}[pace]
    out.append(f"【展開】{PACE_LABEL[pace]}（{race['pace_from']}）")
    out.append(tenkai)
    if race.get('lineup'):
        out.append(race.get('hana', ''))
        out.append("　".join(f"{g}:{','.join(str(h['n']) for h in race['lineup'][g]) or 'ー'}" for g in GROUPS))
    if race['going'] != '良':
        out.append("道悪のため、道悪適性（血統・馬格・実績）を加点し、実力差を少し縮めて評価")
    if race.get('simulation'):
        sm = race['simulation']
        top = '・'.join(str(n) for n in sm['ranking'][:5])
        out.append(f"【シミュレーション{sm['runs']}回】平均着順の上位：{top}")
    if race.get('course'):
        out.append(f"【{race['venue']}の特徴】{race['course']['note']}")
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
    out.append("")
    for b in bets(arr):
        out += [b, ""]
    out.append("")
    if race.get('model'):
        out.append(f"※{race['modelNote']}。評価は平均的な馬の何倍勝ちやすいか（S≥2.0倍／A≥1.3／B≥0.8／C≥0.5／D）")
    else:
        out.append("※スコア×1.00が基準。S≥1.08／A≥1.03／B≥1.00／C≥0.96／D")
        if race.get('modelNote'):
            out.append(f"※{race['modelNote']}")
    if race.get('bookProbs'):
        out.append(f"※期待値は現地オッズから見た勝率×日本のオッズ（現地オッズ{race.get('bookUpdated', '')}時点）")
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
    row_h = 96
    brows = bet_rows_for(arr)
    bet_h = 92 + bet_section_height(brows, W - PAD * 2 - 120) - 80   # 下の「box_h = 80 + bet_h」に合わせる
    LU_H = lineup_height(race)
    H = 610 + LU_H + 64 + row_h * len(arr) + 40 + 90 + bet_h + 120 + 22

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
    if race.get('provisional'):
        note2 = '※枠順の確定前のため、馬番は仮の番号（出馬表の並び順）です'
    elif tb:
        note2 = f"今日の{race['surf']}の傾向：{tb['text']}（終わった{tb['races']}レースから）"
    elif race.get('course'):
        note2 = f"{race['venue']}の特徴：{race['course']['note']}"
    elif race['going'] != '良':
        note2 = '道悪：血統・馬格・実績から道悪適性を加点'
    else:
        note2 = f"ペース根拠：{race['pace_from']}"
    ctext(d, W / 2, 546, fit_text(d, note2, F('sans_r', 22), W - PAD * 2), F('sans_r', 22), MUTED)

    # ── 展開予想 ──
    y = 590
    if LU_H:
        draw_lineup(d, race, PAD, y, W - PAD)
        y += LU_H

    # ── 表 ──
    y += 10
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
                         ('初', (38, 160, 80)) if h['isNew'] else (None, None),
                         ('注', (210, 140, 30)) if h.get('bFlow', 0) >= 0.02 else (None, None)):
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
    if race.get('aiPickNote'):
        ctext(d, W / 2, y - 22, fit_text(d, f"◎{arr[0]['n']} {arr[0]['name']}：{race['aiPickNote']}", F('sans_b', 24), W - PAD * 2),
              F('sans_b', 24), (255, 200, 120))
        y += 22
    axis_t = f"― おすすめ買い目　軸 ◎{arr[0]['n']} {arr[0]['name']} ―"
    box_h = draw_bet_section(d, PAD, y, W - PAD, brows, {h['n']: h.get('w') for h in arr}, title=axis_t)
    y += box_h + 24

    if race.get('model'):
        ctext(d, W / 2, y, '印と評価＝AIだけの評価（平均的な馬の何倍勝ちやすいか　S≥2.0倍 ／ A≥1.3 ／ B≥0.8 ／ C≥0.5 ／ D）', F('sans_r', 20), MUTED)
        foot = f"※{race['modelNote']}。オッズは取得時点のもの"
    else:
        ctext(d, W / 2, y, 'スコア×1.00基準　S≥1.08 ／ A≥1.03 ／ B≥1.00 ／ C≥0.96 ／ D', F('sans_r', 20), MUTED)
        foot = (f"※期待値＝現地オッズから見た勝率×日本のオッズ（現地オッズ{race.get('bookUpdated', '')}時点）"
                if race.get('bookProbs') else
                f"※{race['modelNote']}" if race.get('modelNote') else '※AIシミュレーションの参考値です。オッズは取得時点のもの')
    ctext(d, W / 2, y + 30, fit_text(d, foot, F('sans_r', 20), W - 72), F('sans_r', 20), MUTED)
    return img


# ═════════════════════════════════════════
# 買い目の表示（券種ごとに色分けしたカード）と予算の割り振り
# ═════════════════════════════════════════
CAT_COLOR = {'推奨': (226, 112, 40), '本線': (201, 160, 70), '妙味': (40, 165, 85), '穴': (216, 62, 62), '高目': (150, 85, 205),
             '2頭軸': (150, 85, 205), 'イチ推し': (230, 60, 90), '検証': (90, 110, 140)}
KIND_COLOR = {'3連複2頭軸流し': (140, 105, 225), '単勝': (224, 86, 86), '複勝': (234, 140, 70), '馬連': (60, 135, 225), 'ワイド': (40, 175, 160),
              '馬単': (90, 110, 230), '3連複': (140, 105, 225), '3連単': (214, 72, 160), '枠連': (120, 140, 160)}
CAT_W = {'推奨': 0.15, '本線': 0.40, '妙味': 0.10, '穴': 0.10, '2頭軸': 0.25}      # 予算の割合（項目ごと）
SUB_W = {'本線': {'馬連': 0.30, 'ワイド': 0.30, '3連複': 0.25, '3連単': 0.15},
         '妙味': {'単勝': 0.5, '複勝': 0.5}, '2頭軸': {'3連複': 1.0}}


def bet_rows_for(arr):
    """bet_plan を、画像に描くための行（1行＝1券種）に直す。先頭はイチ推しの1点"""
    rows = []
    plan = bet_plan(arr)
    bb = best_bet(arr, plan)
    if bb:
        body = re.sub(r'（[^（）]*）', '', re.sub(r'^(単勝|馬連|ワイド|3連複|3連単)\s*', '', bb['text'])).strip()
        rows.append({'label': BEST_LABEL, 'kind': bb['name'] if bb['name'] != '単勝' else '単勝', 'combos': bb['combos'],
                     'body': body, 'n': bb['n'],
                     'note': (f"的中約{bb['hit'] * 100:.0f}%・オッズ未発表のため期待値は未計算" if bb.get('noOdds') else
                              f"的中約{bb['hit'] * 100:.0f}%・当たれば約{bb['pay']:.1f}倍・期待値{bb['ev']:.2f}・妙味{bb['edge']:.2f}倍"
                              + ('・見送りも検討' if bb['ev'] <= 1.0 else '')),
                     'per': None, 'amount': None})
    for b in plan:
        text = b['text']
        note = ''
        nm = re.search(r'（([^（）]*(?:期待値|◎|除外|追加|見送り|入れ替え)[^（）]*)）', text)
        if nm:
            note = nm.group(1)
            text = text.replace(nm.group(0), '')
        text = re.sub(r'（\d+点）', '', text).strip()
        body = re.sub(r'^(単勝|複勝|単複|馬連|ワイド|馬単|3連複|3連単)\s*', '', text)
        for kind, combos in b['parts']:
            rows.append({'label': b['label'], 'kind': kind, 'combos': combos, 'body': body, 'note': note,
                         'n': len(combos), 'per': None, 'amount': None})
    return rows


KIND_ORDER = ['単勝', '複勝', '馬連', 'ワイド', '3連複', '3連単']


MAX_PTS = {'単勝': 1, '複勝': 1, '馬連': 1, 'ワイド': 1, '3連複': 6, '3連単': 6}
MIN_ODDS_BUDGET = dict(MIN_ODDS, 単勝=1.0, 複勝=1.0)   # 単勝・複勝は軸1頭なので外さない


def candidate_combos(arr, kind, top=7):
    """予算の買い目の候補：すべて◎（軸）を含む組み合わせ。相手は印の上位から"""
    ax = arr[0]['n']
    mates = [h['n'] for h in arr[1:top]]
    if kind in ('単勝', '複勝'):
        return [(ax,)]
    if kind in ('馬連', 'ワイド'):
        return [tuple(sorted((ax, m))) for m in mates]
    if kind == '3連複':
        return [tuple(sorted((ax, a, b))) for i, a in enumerate(mates) for b in mates[i + 1:]]
    if kind == '3連単':
        return [(ax, a, b) for a in mates for b in mates if a != b]   # ◎1着固定
    return []


def allocate_by_ev(arr, budget):
    """期待値（当たる見込み×予想配当）が1を超える組み合わせだけに、期待値の高さに応じて配分する（ケリー基準の考え方）。
    すべて軸（◎）を含む。期待値1超が無い券種は出さない"""
    budget = budget // 100 * 100
    pa, pm = prob_tables(arr)
    out = []
    for kind in KIND_ORDER:
        if kind in ('馬連', 'ワイド'):
            pk = pair_pick(arr, kind, pm, value_horse(arr, pa, pm))
            cands = [tuple(sorted(pk[0]))] if pk else []
        else:
            cands = candidate_combos(arr, kind)
        pool = []
        for c in cands:
            o = est_odds(kind, c, pm)
            pr = combo_prob(kind, c, pa)
            ev = pr * o
            if ev > 1.0 and o >= MIN_ODDS_BUDGET.get(kind, 0) and o > 1:
                pool.append({'c': c, 'odds': o, 'p': pr, 'ev': ev, 'k': (ev - 1) / (o - 1)})
        pool.sort(key=lambda x: -x['ev'])
        items = pool[:MAX_PTS[kind]]
        while items and 100 * len(items) > budget:
            items.pop()
        if not items:
            continue
        tot_k = sum(x['k'] for x in items)
        for x in items:
            x['stake'] = max(100, int(budget * x['k'] / tot_k // 100) * 100)
        while sum(x['stake'] for x in items) > budget:
            big = max((x for x in items if x['stake'] > 100), key=lambda x: x['stake'], default=None)
            if not big:
                break
            big['stake'] -= 100
        left = budget - sum(x['stake'] for x in items)
        best = max(items, key=lambda x: x['k'])
        best['stake'] += left // 100 * 100
        for x in items:
            x['ret'] = x['stake'] * x['odds']
        items.sort(key=lambda x: x['c'])
        out.append({'kind': kind, 'items': items, 'per': None, 'total': sum(x['stake'] for x in items),
                    'hit': min(0.99, sum(x['p'] for x in items)), 'dropped': 0})
    return out, budget


def allocate_by_kind(arr, budget, unit=None, ev_mode=False):
    if ev_mode:
        return allocate_by_ev(arr, budget)
    """券種ごとに「その券種だけで予算を使うなら」の買い目。1点の金額はそろえて、
    期待値（当たる見込み×予想配当）の高い組み合わせから点数分だけ選ぶ。
    unit：1点の金額（指定なしなら、予算を使い切れる点数を最大6点の中から選ぶ）"""
    budget = budget // 100 * 100
    pa, pm = prob_tables(arr)
    out = []
    for kind in KIND_ORDER:
        pool = []
        if kind in ('馬連', 'ワイド'):   # 本線と同じ1点（◎-○が基本）
            pk = pair_pick(arr, kind, pm, value_horse(arr, pa, pm))
            cands = [tuple(sorted(pk[0]))] if pk else []
        else:
            cands = candidate_combos(arr, kind)
        for c in cands:
            o = est_odds(kind, c, pm)
            if o >= MIN_ODDS_BUDGET.get(kind, 0):
                pool.append({'c': c, 'odds': o, 'p': combo_prob(kind, c, pa)})
        if not pool:
            continue
        pool.sort(key=lambda x: -x['p'] * x['odds'])
        cap = min(MAX_PTS[kind], len(pool))
        if unit:
            per = max(100, unit // 100 * 100)
            n_pts = min(cap, budget // per) if budget >= per else 0
        else:
            # 予算をいちばん使い切れる点数（同じなら多い方）
            best = None
            for n_ in range(1, cap + 1):
                per_ = budget // n_ // 100 * 100
                if per_ < 100:
                    break
                left = budget - per_ * n_
                if best is None or left < best[0] or (left == best[0] and n_ > best[1]):
                    best = (left, n_, per_)
            if not best:
                continue
            _, n_pts, per = best
        if n_pts <= 0:
            continue
        items = pool[:n_pts]
        for x in items:
            x['stake'] = per
            x['ret'] = per * x['odds']
        items.sort(key=lambda x: x['c'])
        if kind in ('複勝', 'ワイド'):   # 同時に2つ以上当たることがあるので「どれか1つ以上当たる見込み」で出す
            miss = 1.0
            for x in items:
                miss *= 1 - min(0.99, x['p'])
            hit = 1 - miss
        else:                          # 当たりは1つだけなので、見込みの合計
            hit = sum(x['p'] for x in items)
        out.append({'kind': kind, 'items': items, 'per': per, 'total': per * len(items),
                    'hit': min(0.99, hit), 'dropped': 0})
    return out, budget


def combo_str(kind, c):
    return ('→' if kind in ('馬単', '3連単') else '-').join(map(str, c))


def _row_lines(d, r, maxw):
    if not r['combos']:
        return wrap(d, r['body'] or '見送り', F('sans_b', 27), maxw)
    simple = all(len(c) <= 2 for c in r['combos']) and r['n'] <= 4
    return [] if simple else wrap(d, r['body'], F('sans_b', 27), maxw)


def bet_section_height(rows, inner_w):
    tmp = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    h = 0
    for r in rows:
        lines = _row_lines(tmp, r, inner_w)
        h += 24 + 46 + (54 if not lines else 12 + 38 * len(lines)) + (36 if r['note'] else 0) + 14
    return h


def draw_bet_section(d, x0, y0, x1, rows, wmap, title='― おすすめ買い目 ―', sub=''):
    """買い目をカードで描く。1行＝1券種。左に項目（推奨・本線…）の色帯、券種のバッジ、組み合わせ、金額"""
    inner_w = x1 - x0 - 120
    box_h = 92 + (34 if sub else 0) + bet_section_height(rows, inner_w)
    d.rounded_rectangle((x0, y0, x1, y0 + box_h), radius=18, fill=(12, 19, 38))
    gold_frame(d, (x0, y0, x1, y0 + box_h), r=18)
    ctext(d, (x0 + x1) / 2, y0 + 16, fit_text(d, title, F('serif_b', 36), x1 - x0 - 40), F('serif_b', 36), GOLD)
    y = y0 + 72
    if sub:
        ctext(d, (x0 + x1) / 2, y - 4, sub, F('sans_b', 24), SILVER)
        y += 34
    for r in rows:
        lines = _row_lines(d, r, inner_w)
        rh = 24 + 46 + (54 if not lines else 12 + 38 * len(lines)) + (36 if r['note'] else 0)
        cc = CAT_COLOR.get(r['label'], GOLD_D)
        # カード本体と左の色帯
        d.rounded_rectangle((x0 + 18, y, x1 - 18, y + rh), radius=14, fill=(22, 32, 58))
        d.rounded_rectangle((x0 + 18, y, x0 + 30, y + rh), radius=6, fill=cc)
        # 見出し：項目の札＋券種のバッジ
        cx, cy = x0 + 46, y + 14
        lw = max(84, tw(d, r['label'], F('sans_b', 24)) + 28)
        d.rounded_rectangle((cx, cy, cx + lw, cy + 40), radius=20, fill=cc)
        ctext(d, cx + lw / 2, cy + 5, r['label'], F('sans_b', 24), (255, 255, 255))
        kc = KIND_COLOR.get(r['kind'], (110, 120, 140))
        kw = tw(d, r['kind'], F('sans_b', 26)) + 30
        kx = cx + lw + 12
        d.rounded_rectangle((kx, cy, kx + kw, cy + 40), radius=8, fill=kc)
        ctext(d, kx + kw / 2, cy + 4, r['kind'], F('sans_b', 26), (255, 255, 255))
        d.text((kx + kw + 14, cy + 7), f"{r['n']}点" if r['n'] else '見送り', font=F('sans_r', 24), fill=MUTED)
        # 右側：金額（予算モード）
        if r.get('amount'):
            amt = f"{r['amount']:,}円"
            d.text((x1 - 40 - tw(d, amt, F('sans_b', 34)), cy - 2), amt, font=F('sans_b', 34), fill=GOLD)
            per = f"1点{r['per']:,}円"
            d.text((x1 - 40 - tw(d, per, F('sans_r', 20)), cy + 42), per, font=F('sans_r', 20), fill=MUTED)
        # 組み合わせ
        by = y + 70
        if not lines:
            bx = x0 + 50
            for c in r['combos']:
                for k, num in enumerate(c):
                    bg, fg = WAKU.get(wmap.get(num) or 0, ((245, 245, 245), (20, 20, 20)))
                    d.ellipse((bx, by, bx + 44, by + 44), fill=bg, outline=GOLD_D, width=2)
                    ctext(d, bx + 22, by + 6, str(num), F('sans_b', 24), fg)
                    bx += 48
                    if k < len(c) - 1:
                        d.text((bx, by + 4), '-', font=F('sans_b', 28), fill=MUTED)
                        bx += 18
                bx += 30
            by += 54
        else:
            for k, ln in enumerate(lines):
                d.text((x0 + 50, by + 6 + k * 38), ln, font=F('sans_b', 27), fill=SILVER)
            by += 12 + 38 * len(lines)
        if r['note']:
            d.text((x0 + 50, by), fit_text(d, r['note'], F('sans_r', 22), x1 - x0 - 100), font=F('sans_r', 22),
                   fill=(255, 190, 200) if r['label'] == BEST_LABEL else (130, 215, 150) if '期待値' in r['note'] else MUTED)
        y += rh + 14
    return box_h


def render_budget_image(race, arr, budget, unit=None, ev_mode=False):
    """予算の配分の画像（券種ごとに、その券種だけで予算を使う場合。1点の金額はそろえる／期待値モード）"""
    plans, budget = allocate_by_kind(arr, budget, unit, ev_mode)
    W, PAD = 1080, 36
    wmap = {h['n']: h.get('w') for h in arr}
    LH = 40
    heights = [24 + 46 + 12 + LH * len(pl['items']) + (30 if pl['dropped'] else 0) + 16 for pl in plans]
    H = 290 + sum(h_ + 16 for h_ in heights) + 100 + (110 if not plans else 0)
    img = gradient_bg(W, H)
    d = ImageDraw.Draw(img)
    gold_frame(d, (14, 14, W - 14, H - 14), r=22, w=3)
    ctext(d, W / 2, 40, '期待値配分' if ev_mode else '資金配分プラン', F('serif_b', 52), GOLD)
    title = f"{race['venue']}{race['R']}R  {race['name']}"
    ctext(d, W / 2, 112, fit_text(d, title, F('sans_b', 34), W - PAD * 2), F('sans_b', 34), SILVER)
    ctext(d, W / 2, 162, f"予算 {budget:,}円　軸 ◎{arr[0]['n']} {arr[0]['name']}", F('sans_b', 30), GOLD)
    sub_t = ('期待値1を超える組み合わせだけに、期待値が高いほど多く配分（軸入り）' if ev_mode else
             '全部の買い目に軸が入ります。券種を1つ選んで買う想定で、相手は期待値の高い順です')
    ctext(d, W / 2, 206, sub_t, F('sans_r', 22), MUTED)
    if not plans:
        ctext(d, W / 2, 300, '期待値1を超える組み合わせがありません（見送りがおすすめ）', F('sans_b', 30), SILVER)
    y = 254
    for pl, hh in zip(plans, heights):
        kc = KIND_COLOR.get(pl['kind'], (110, 120, 140))
        d.rounded_rectangle((PAD, y, W - PAD, y + hh), radius=14, fill=(22, 32, 58))
        d.rounded_rectangle((PAD, y, PAD + 12, y + hh), radius=6, fill=kc)
        kw = tw(d, pl['kind'], F('sans_b', 28)) + 32
        d.rounded_rectangle((PAD + 28, y + 16, PAD + 28 + kw, y + 58), radius=8, fill=kc)
        ctext(d, PAD + 28 + kw / 2, y + 20, pl['kind'], F('sans_b', 28), (255, 255, 255))
        info = (f"1点{pl['per']:,}円×{len(pl['items'])}点　当たる見込み 約{pl['hit'] * 100:.0f}%" if pl.get('per')
                else f"{len(pl['items'])}点　当たる見込み 約{pl['hit'] * 100:.0f}%")
        d.text((PAD + 44 + kw, y + 24), info, font=F('sans_r', 24), fill=MUTED)
        amt = f"{pl['total']:,}円"
        d.text((W - PAD - 24 - tw(d, amt, F('sans_b', 34)), y + 16), amt, font=F('sans_b', 34), fill=GOLD)
        yy = y + 82
        for x in pl['items']:
            d.text((PAD + 44, yy), combo_str(pl['kind'], x['c']), font=F('sans_b', 28), fill=SILVER)
            st = f"{x['stake']:,}円"
            d.text((PAD + 400 - tw(d, st, F('sans_b', 28)), yy), st, font=F('sans_b', 28), fill=GOLD)
            d.text((PAD + 430, yy + 4), f"→ 当たれば 約{x['ret']:,.0f}円（予想{x['odds']:.1f}倍" + (f"・期待値{x['ev']:.2f}" if x.get('ev') else '') + "）",
                   font=F('sans_r', 24), fill=MUTED)
            yy += LH
        if pl['dropped']:
            d.text((PAD + 44, yy + 2), f"※予算に入りきらない{pl['dropped']}点は、見込みの低い順に外しました",
                   font=F('sans_r', 22), fill=MUTED)
        y += hh + 16
    if not plans:
        y += 110
    ctext(d, W / 2, y + 6, '※予想配当は今の単勝オッズからの目安です（実際の配当は売れ方で変わります）', F('sans_r', 20), MUTED)
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
            res = '勝ち' if l['pos'] == 1 else ('着差の記載なし' if l.get('mEst') else f"{l['m']:.1f}秒差")
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
    pos_t = f"予想位置は{h.get('posGroup', '?')}（{h.get('posFrom', '')}）。" if h.get('posGroup') else ''
    lead_t = ''
    if h.get('bPos', 0) > 0:
        lead_t = f"単騎で逃げられそうで加点（+{h['bPos']:.3f}）。"
    elif h.get('bPos', 0) < 0:
        lead_t = f"ハナ争いで共倒れの心配（{h['bPos']:.3f}）。"
    R.append(('脚質・展開', f"{pos_t}{lead_t}脚質は{st}。{PACE_LABEL[race['pace']]}ペース想定で{eff}"))

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
        kind = '実データの基準タイムで測った' if h.get('siReal', 0) >= 2 else '簡易'
        R.append(('タイム', f"近走の{kind}タイム指数{h['si']:+.0f}（メンバー内の評価「{['低い','やや低い','標準','やや高い','高い'][h['siRate'] - 1]}」）"
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
    if h.get('trainGrade'):
        R.append(('調教', f"評価{h['trainGrade']}" + (f"（{h['trainComment']}）" if h.get('trainComment') else '') + sgn(h.get('bTrain', 0))))
    if h.get('condNotes'):
        R.append(('状態', '、'.join(h['condNotes']) + sgn(h.get('bCond', 0))))
    if h.get('hlNotes'):
        R.append(('ハイレベル戦', '。'.join(h['hlNotes'][:3]) + sgn(h.get('bHL', 0))))
    if h.get('abroadNotes'):
        R.append(('海外レース', '。'.join(h['abroadNotes']) + sgn(h.get('bAbroad', 0))))
    if h.get('moveNotes'):
        R.append(('格付け', '。'.join(h['moveNotes']) + sgn(h.get('bMove', 0))))
    P = race.get('course')
    if P and abs(h.get('bCourse', 0)) >= 0.002:
        st_ = h['st'] if h['st'] != '?' else '不明'
        R.append(('コース特性', f"{race['venue']}：{P['note']}。脚質{st_}・{h['n']}番の条件で"
                  f"{'有利' if h['bCourse'] > 0 else '不利'}"
                  + (f"。{h['courseNote']}" if h.get('courseNote') else '') + sgn(h['bCourse'])))
    if h.get('sim'):
        hs = h['sim']
        R.append(('シミュレーション', f"{race['simulation']['runs']}回走らせて平均{hs['avg']:.1f}着（{hs['rank']}位）・"
                  f"勝率{hs['win'] * 100:.1f}%・3着内{hs['top3'] * 100:.1f}%。位置取りは主に{hs['group']}"))
    if h.get('flowNotes'):
        R.append(('近走の中身', '。'.join(h['flowNotes']) + sgn(h.get('bFlow', 0))))
    if h.get('careerNotes'):
        R.append(('全成績から', '、'.join(h['careerNotes']) + sgn(h.get('bCareer', 0))))

    R.append(('血統・厩舎', f"父{h['sire'] or '不明'}" + (f"・母父{h['damsire']}" if h.get('damsire') else '')))
    return R


def sgn(v):
    return f"（{v:+.3f}）" if abs(v) >= 0.001 else ''


SHORT_HEAD = {'総合評価': '総合評価', '過去レースのレベル': 'レース格', '対戦相手のその後': '対戦相手',
              '近走': '近走', 'コース・距離適性': '適性', '脚質・展開': '展開', '騎手・条件': '騎手・斤量',
              '道悪適性': '道悪', '穴馬チェック': '穴馬', '血統・厩舎': '血統', 'タイム': 'タイム',
              '上がり': '上がり', '枠順': '枠順', '当日の馬場': '当日馬場', '騎手データ': '騎手成績', '状態': '状態', '全成績から': '全成績', '近走の中身': '近走の中身', 'コース特性': 'コース', '格付け': '格付け', '海外レース': '海外', 'ハイレベル戦': 'ハイレベル', '調教': '調教', 'シミュレーション': 'シミュ'}


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
    targets = [(i, h) for i, h in enumerate(arr[:7])] + \
              [(99, h) for h in arr[7:] if h['anaFlag'] or h.get('bFlow', 0) >= 0.02]
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
    ctext(d, W / 2, 40, f'シェイクユアハート ／ 根拠 {page}/{pages}' if page else 'シェイクユアハート ／ 詳しい根拠',
          F('serif_b', 44), GOLD)
    title = f"{race['venue']}{race['R']}R {race['name']}"
    ctext(d, W / 2, 106, fit_text(d, title, F('sans_b', 34), W - PAD * 2), F('sans_b', 34), SILVER)
    d.line((PAD + 120, 166, W - PAD - 120, 166), fill=GOLD_D, width=2)

    y = 190
    for i, h, rows, height in cards:
        d.rounded_rectangle((PAD, y, W - PAD, y + height), radius=16, fill=(14, 22, 42))
        gold_frame(d, (PAD, y, W - PAD, y + height), r=16)
        mark = MARKS[i] if i < len(MARKS) else ('穴' if h['anaFlag'] else '注')
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


# ═════════════════════════════════════════
# 全頭短評（2枚目）：1頭2〜3行で、強みと不安をひとことに
# ═════════════════════════════════════════
def _cut(t, n=10):
    return re.split(r'[（(]', t or '')[0].strip()[:n]


def short_comment(race, h):
    """1行（長くても2行）の短評。強み2つ＋不安1つを、根拠の加点・減点の大きさから選ぶ"""
    pace = PACE_LABEL[race['pace']]
    g = h.get
    cond = _cut((g('condNotes') or [''])[0], 12)
    car = _cut((g('careerNotes') or [''])[0], 10)
    f = [  # (点数, 強み, 不安)
        (g('bPos', 0), '単騎逃げ濃厚', 'ハナ争い'),
        (g('paceAdj', 0), f'{pace}の展開向く', f'{pace}の展開不向き'),
        (g('bTime', 0), '時計上位', '時計見劣り'),
        (g('bAgari', 0), f"上がり{g('agRank')}位の末脚", '末脚見劣り'),
        (g('rl', 0) * 0.4, '相手骨っぽい中で好走', ''),
        (g('bHL', 0), 'ハイレベル戦好走', 'ハイレベル戦で凡走'),
        (g('bJockey', 0), '鞍上好調', '騎手データ'),
        (g('bTrain', 0), '調教良好', '調教いまひとつ'),
        (g('bCond', 0), '叩き2戦目' if '叩き' in cond else '状態上向き', '休み明け' if '休み明け' in cond else '状態面'),
        (g('bFlow', 0), '近走内容濃い', '近走内容薄い'),
        (g('bCareer', 0), car or '条件好転', car or '全成績'),
        (g('bDraw', 0), '枠順有利', '枠順不利'),
        (g('bTrack', 0), '今日の馬場◎', '今日の馬場合わず'),
        (g('bCourse', 0), 'コース向く', 'コース合わず'),
        ((g('form', 3) - 3) * 0.012, '近走好調', '近走不振'),
    ]
    if race.get('going') != '良':
        f.append((g('mudb', 0), '道悪巧者', '道悪苦手'))
    if g('distSum', 0) > 0:
        f.append((min(g('distSum'), 2.5) * 0.012, '距離実績', ''))
    if g('cl'):
        f.append((g('cl')['gap'] * 0.02, '格上相手に実績', '相手強化'))
    pos = [x[1] for x in sorted([x for x in f if x[0] >= 0.004 and x[1]], key=lambda x: -x[0])[:2]]
    neg = [x[2] for x in sorted([x for x in f if x[0] <= -0.004 and x[2]], key=lambda x: x[0])[:1]]
    s = ''
    if g('isNew'):
        s += '新馬。血統から評価。'
    if g('anaFlag'):
        s += '穴条件あり。'
    if pos:
        s += '＋'.join(pos) + '。'
    if neg:
        s += f'{neg[0]}が課題。'
    if g('o'):
        ev = h['p'] * h['o']
        s += '妙味大。' if ev >= 1.3 else '過剰人気気味。' if ev < 0.7 else ''
    return s or '判断材料が少ない。'


def short_mark(i, h):
    """◎○▲△△△☆ のあとは、穴の条件があれば「注」、ランクB以上は「押」、それ以外は「消」"""
    if i < len(MARKS):
        return MARKS[i]
    if h.get('anaFlag'):
        return '注'
    return '押' if h.get('rank') in ('S', 'A', 'B') else '消'


MARK_STYLE = {'◎': ((170, 30, 40), (255, 255, 255)), '○': ((30, 70, 170), (255, 255, 255)),
              '▲': ((190, 150, 40), (255, 255, 255)), '△': ((60, 66, 84), (255, 255, 255)),
              '☆': ((150, 110, 30), (255, 240, 200)), '注': ((70, 76, 96), (255, 255, 255)),
              '押': ((30, 110, 60), (255, 255, 255)), '消': ((40, 44, 56), (170, 176, 190))}
MARK_NAME = [('◎', '本命'), ('○', '対抗'), ('▲', '単穴'), ('☆', '穴'), ('△', '連下'), ('注', '注意'), ('押', '押さえ'), ('消', '消し')]


def draw_mark(d, cx, cy, mk, r=24):
    bg, fg = MARK_STYLE.get(mk, MARK_STYLE['△'])
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=bg, outline=GOLD_D, width=2)
    ctext(d, cx, cy - r * 0.78, mk, F('sans_b', int(r * 1.15)), fg)


def render_short_image(race, arr):
    """全馬 印＆短評。馬名の横に短評を置いて、文字を大きくする（1頭1〜2行）"""
    W, PAD = 1440, 30
    fN, fC = F('sans_b', 34), F('sans_b', 28)
    c_mark, c_num, c_name = PAD + 18, PAD + 100, PAD + 170      # 列の左端
    c_cmt = c_name + 400
    cmt_w = W - PAD - 20 - c_cmt
    tmp = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    rows = []
    for i, h in enumerate(arr):
        lines = wrap(tmp, short_comment(race, h), fC, cmt_w)[:2]
        rows.append((i, h, short_mark(i, h), lines, 78 if len(lines) == 1 else 112))
    H = 444 + sum(r[4] for r in rows) + 30 + 360
    img = gradient_bg(W, H)
    d = ImageDraw.Draw(img)
    gold_frame(d, (14, 14, W - 14, H - 14), r=24, w=3)
    date = race.get('date')
    top = (f"{date:%m/%d} " if hasattr(date, 'strftime') else '') + f"{race['venue']}{race['R']}R"
    ctext(d, W / 2, 34, top, F('sans_b', 34), SILVER)
    ctext(d, W / 2, 80, fit_text(d, race['name'], F('serif_b', 64), W - 200), F('serif_b', 64), GOLD)
    ctext(d, W / 2, 168, '全馬印＆短評', F('serif_b', 52), (250, 236, 200))
    sub = f"{race['surf']}{race['dist']}m ／ 馬場 {race.get('going', '')} ／ {PACE_LABEL[race['pace']]}ペース想定 ／ 並び順：印順"
    sw = tw(d, sub, F('sans_r', 26)) + 60
    d.rounded_rectangle((W / 2 - sw / 2, 240, W / 2 + sw / 2, 286), radius=23, fill=(20, 28, 52), outline=GOLD_D)
    ctext(d, W / 2, 246, sub, F('sans_r', 26), SILVER)
    # 凡例
    y = 312
    d.rounded_rectangle((PAD, y, W - PAD, y + 64), radius=14, fill=(14, 22, 42), outline=GOLD_D, width=2)
    lx = PAD + 34
    step = (W - PAD * 2 - 40) / len(MARK_NAME)
    for k, (mk, nm) in enumerate(MARK_NAME):
        x = lx + k * step
        draw_mark(d, x + 18, y + 32, mk, r=19)
        d.text((x + 46, y + 14), nm, font=F('sans_b', 26), fill=SILVER)
    y += 84
    # 表の見出し
    d.rectangle((PAD, y, W - PAD, y + 48), fill=(34, 30, 20))
    for cx, t in ((c_mark + 24, '印'), (c_num + 26, '馬番'), (c_name + 180, '馬名'), (c_cmt + cmt_w / 2, '短評')):
        ctext(d, cx, y + 8, t, F('sans_b', 24), GOLD)
    y += 48
    for k, (i, h, mk, lines, rh) in enumerate(rows):
        d.rectangle((PAD, y, W - PAD, y + rh), fill=(16, 24, 46) if k % 2 == 0 else (11, 17, 34))
        d.line((PAD, y + rh, W - PAD, y + rh), fill=(60, 54, 36), width=1)
        for cx in (c_num - 8, c_name - 10, c_cmt - 14):
            d.line((cx, y, cx, y + rh), fill=(60, 54, 36), width=1)
        cy = y + rh / 2
        draw_mark(d, c_mark + 24, cy, mk)
        bg, fg = WAKU.get(h.get('w') or 0, ((245, 245, 245), (20, 20, 20)))
        d.rounded_rectangle((c_num + 2, cy - 24, c_num + 50, cy + 24), radius=8, fill=bg, outline=GOLD_D)
        ctext(d, c_num + 26, cy - 20, str(h['n']), F('sans_b', 30), fg)
        fz = next((F('sans_b', z) for z in (34, 30, 26, 23) if tw(d, h['name'], F('sans_b', z)) <= 380), F('sans_b', 23))
        d.text((c_name, cy - fz.size * 0.72), fit_text(d, h['name'], fz, 380), font=fz, fill=(250, 250, 255))
        ty = cy - len(lines) * 17 - 2
        for j, ln in enumerate(lines):
            d.text((c_cmt, ty + j * 36), ln, font=fC, fill=(225, 228, 238))
        y += rh
    # 注目ポイント
    y += 30
    d.rounded_rectangle((PAD, y, W - PAD, y + 266), radius=18, fill=(14, 22, 42))
    gold_frame(d, (PAD, y, W - PAD, y + 266), r=18, w=2)
    ctext(d, PAD + 150, y + 92, '注目ポイント', F('serif_b', 40), GOLD)
    ctext(d, PAD + 150, y + 146, 'KEY POINTS', F('sans_b', 20), GOLD_D)
    d.line((PAD + 300, y + 24, PAD + 300, y + 242), fill=GOLD_D, width=2)
    val = [h for h in arr[1:10] if h.get('o')]
    val = max(val, key=lambda h: h['p'] * h['o']) if val else None
    fav = race.get('fav')
    pts = [f"本命は {arr[0]['n']} {arr[0]['name']}" + (f"（1番人気は{fav['n']}番）" if fav and fav is not arr[0] else '')]
    bb = best_bet(arr)
    if bb:
        pts.append(f"おすすめの買い方は {bb['name']}（的中約{bb['hit'] * 100:.0f}%" + ('' if bb.get('noOdds') else f"・期待値{bb['ev']:.2f}") + '）')
    if val and val['p'] * val['o'] >= 1.1:
        pts.append(f"妙味なら {val['n']} {val['name']}")
    if len(arr) >= 3:
        pts.append(f"相手本線は {arr[1]['name']} ＆ {arr[2]['name']}")
    pts = pts[:4]
    for j, t in enumerate(pts):
        py = y + 30 + j * 56
        d.ellipse((PAD + 330, py, PAD + 370, py + 40), fill=GOLD)
        ctext(d, PAD + 350, py + 3, str(j + 1), F('sans_b', 26), (30, 24, 10))
        d.text((PAD + 388, py - 2), fit_text(d, t, F('sans_b', 34), W - PAD - 420), font=F('sans_b', 34), fill=(250, 236, 200))
    ctext(d, W / 2, y + 278, '馬名か馬番（例：10番）を送ると、その馬の詳しい根拠を返します', F('sans_b', 24), MUTED)
    return img


def find_horse(text, arr):
    """「10番」「10」「フィンガー」「フィン」などから馬を探す"""
    t = text.strip().replace(' ', '').replace('　', '')
    t = t.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    m = re.fullmatch(r'(\d{1,2})番?', t)
    if m:
        return next((h for h in arr if h.get('n') == int(m.group(1))), None)
    if len(t) < 2:
        return None
    exact = [h for h in arr if h['name'] == t]
    if exact:
        return exact[0]
    part = [h for h in arr if t in h['name']]
    return part[0] if len(part) == 1 else None


def horse_detail_image(race, arr, h):
    i = arr.index(h)
    return render_reasons_image(race, [(i if i < len(MARKS) else 99, h)], 0, 0)


# ═════════════════════════════════════════
# 展開図（3枚目）：スタート後・3コーナー・4コーナーの隊列（右が先頭、上が内ラチ）
# ═════════════════════════════════════════
def render_tenkai_image(race, arr, sim):
    stages = SV.stage_layout(race, arr, sim)
    by_n = {h['n']: h for h in arr if h.get('n')}
    W, PAD = 1080, 32
    R_ = 25                      # 丸の半径
    MAX_LANE = 6                 # 段（内→外）の最大数
    x_r, x_l = W - PAD - 64, PAD + 100
    # 横に近い馬は外（下の段）へ
    layouts = []
    for name, gaps, gain in stages:
        placed = []
        if name == '4コーナー':     # 勢い：ほかの馬より多く詰めた分だけ（全体の詰まりは差し引く）
            med = sorted(gain.values())[len(gain) // 2]
            gain = {n: v - med for n, v in gain.items()}
        min_dx = R_ * 2 + 30
        sc = (x_r - x_l) / (max(gaps.values()) or 1)   # 地点ごとに横幅いっぱいに広げる（先頭〜最後方）
        for n in sorted(gaps, key=lambda n: gaps[n]):
            x = x_r - gaps[n] * sc
            def room(l):   # その段で一番近い馬までの距離
                return min([abs(px - x) for _, px, pl in placed if pl == l] or [9999])
            free = [l for l in range(MAX_LANE) if room(l) >= min_dx]
            lane = free[0] if free else max(range(MAX_LANE), key=room)   # 5段までに収める
            placed.append((n, x, lane))
        layouts.append((name, placed, gain))
    row_h = 96
    panel_h = [76 + (max(p[2] for p in pl) + 1) * row_h + 24 for _, pl, _ in layouts]
    H = 200 + sum(ph + 22 for ph in panel_h) + 150
    img = gradient_bg(W, H)
    d = ImageDraw.Draw(img)
    gold_frame(d, (14, 14, W - 14, H - 14), r=22, w=3)
    ctext(d, W / 2, 34, 'シェイクユアハート ／ 展開予想', F('serif_b', 44), GOLD)
    title = f"{race['venue']}{race['R']}R {race['name']}"
    ctext(d, W / 2, 98, fit_text(d, title, F('sans_b', 32), W - PAD * 2), F('sans_b', 32), SILVER)
    d.line((PAD + 120, 156, W - PAD - 120, 156), fill=GOLD_D, width=2)
    y = 176
    for (name, placed, gain), ph in zip(layouts, panel_h):
        d.rounded_rectangle((PAD, y, W - PAD, y + ph), radius=16, fill=(14, 22, 42))
        gold_frame(d, (PAD, y, W - PAD, y + ph), r=16)
        d.rounded_rectangle((PAD + 18, y + 14, PAD + 190, y + 54), radius=20, fill=(48, 38, 16), outline=GOLD_D)
        ctext(d, PAD + 104, y + 17, name, F('sans_b', 24), GOLD)
        d.text((W - PAD - 190, y + 20), '進行方向 ▶', font=F('sans_b', 22), fill=MUTED)
        ty = y + 66
        d.rounded_rectangle((PAD + 14, ty, W - PAD - 14, y + ph - 14), radius=10, fill=(30, 66, 42))
        d.line((PAD + 14, ty + 2, W - PAD - 14, ty + 2), fill=(225, 228, 236), width=4)     # 内ラチ
        ctext(d, PAD + 44, ty + 14, '内ラチ', F('sans_b', 16), (225, 228, 236))
        ctext(d, PAD + 44, y + ph - 46, '外', F('sans_b', 16), (160, 175, 165))
        for n, x, lane in placed:
            cy = ty + 46 + lane * row_h
            h = by_n.get(n, {})
            bg, fg = WAKU.get(h.get('w') or 0, ((245, 245, 245), (20, 20, 20)))
            d.ellipse((x - R_, cy - R_, x + R_, cy + R_), fill=bg, outline=(20, 20, 20), width=2)
            ctext(d, x, cy - 15, str(n), F('sans_b', 24), fg)
            ctext(d, x, cy + R_ + 2, (h.get('name') or '')[:4], F('sans_b', 18), SILVER)
            if name == '4コーナー':                 # 3→4コーナーで詰めた馬に「》」
                gm = gain.get(n, 0)
                k = 3 if gm > 8 else 2 if gm > 4 else 1 if gm > 1.5 else 0
                if k:
                    cw_ = tw(d, '》' * k, F('sans_b', 16))
                    d.rounded_rectangle((x + 8, cy - R_ - 12, x + 16 + cw_, cy - R_ + 10), radius=8, fill=(120, 24, 30))
                    d.text((x + 12, cy - R_ - 13), '》' * k, font=F('sans_b', 16), fill=(255, 200, 200))
        y += ph + 22
    # 下の説明
    ld = race.get('leader')
    tr = SV.track_of(race)
    rep = sim['rep']
    l1 = f"予想：{PACE_LABEL[race['pace']]}ペース" + (f"・{ld['n']}番{ld['name']}が逃げ" if ld else '')
    l2 = f"{race['venue']} {race['surf']}{race['dist']}m {'右' if tr['right'] else '左'}回り ／ シミュ{sim['runs']}回："
    l2 += (f"この展開になったのは{rep['n_match']}回" if rep.get('match') == 'pred'
           else 'この逃げ馬になった回は無く、ペースだけ合わせた回を表示' if rep.get('match') == 'pred_pace'
           else '最も多かった展開を表示')
    ctext(d, W / 2, y + 8, fit_text(d, l1, F('sans_b', 28), W - PAD * 2), F('sans_b', 28), GOLD)
    ctext(d, W / 2, y + 50, fit_text(d, l2, F('sans_r', 22), W - PAD * 2), F('sans_r', 22), SILVER)
    ctext(d, W / 2, y + 88, '》は3→4コーナーで差を詰めた馬（多いほど勢いあり）。「動画」で動きも見られます', F('sans_r', 20), MUTED)
    return img


def lineup_rows(race):
    per_row = 5
    return {g: max(1, math.ceil(len(race['lineup'][g]) / per_row)) for g in GROUPS}


def lineup_height(race):
    if not race.get('lineup'):
        return 0
    return 70 + 56 * max(lineup_rows(race).values()) + 26


def draw_lineup(d, race, x0, y0, x1):
    """展開予想の隊列（逃げ｜先行｜中団｜後方）を枠色の丸で描く"""
    h_ = lineup_height(race)
    d.rounded_rectangle((x0, y0, x1, y0 + h_ - 10), radius=14, fill=(14, 22, 42))
    gold_frame(d, (x0, y0, x1, y0 + h_ - 10), r=14)
    d.text((x0 + 20, y0 + 12), '展開予想', font=F('sans_b', 26), fill=GOLD)
    sub = race.get('hana', '')
    d.text((x0 + 150, y0 + 16), fit_text(d, sub, F('sans_r', 22), x1 - x0 - 170), font=F('sans_r', 22), fill=SILVER)
    cw = (x1 - x0 - 40) / 4
    cols = [(200, 60, 60), (220, 150, 40), (60, 140, 220), (120, 110, 190)]
    for gi, g in enumerate(GROUPS):
        cx = x0 + 20 + gi * cw
        d.rounded_rectangle((cx + 4, y0 + 52, cx + 70, y0 + 80), radius=12, fill=cols[gi])
        ctext(d, cx + 37, y0 + 53, g, F('sans_b', 18), (255, 255, 255))
        if gi < 3:
            d.text((cx + cw - 22, y0 + 50), '◀', font=F('sans_r', 18), fill=MUTED)
        for k, h in enumerate(race['lineup'][g]):
            rx = cx + 22 + (k % 5) * 46
            ry = y0 + 108 + (k // 5) * 56
            bg, fg = WAKU.get(h.get('w') or 0, ((245, 245, 245), (20, 20, 20)))
            d.ellipse((rx - 20, ry - 20, rx + 20, ry + 20), fill=bg, outline=GOLD_D, width=2)
            ctext(d, rx, ry - 15, str(h['n']), F('sans_b', 22), fg)


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
    plan = bet_plan(arr)
    out = [f"{race['venue']}{race['R']}R {race['name']}", f"【おすすめ買い目】軸 ◎{arr[0]['n']} {arr[0]['name']}"]
    if race.get('aiPickNote'):
        out.append(f"（{race['aiPickNote']}）")
    out.append("")
    bb = best_bet(arr, plan)
    if bb:
        out += [f"🎯イチ推しの買い方：{best_bet_text(bb)}", f"　{bb['text']}", f"　→ {bb['why']}", ""]
    for b in plan:
        out += [f"{b['label']} {b['text']}", ""]   # 見やすいように1行ずつ空ける
    return "\n".join(out).rstrip()



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
    bb = best_bet(arr, plan)
    if bb:   # イチ推しは成績を別に見るための記録（合計の収支には入れない）
        plan = plan + [{'label': BEST_LABEL, 'text': best_bet_text(bb), 'parts': [(bb['kind'], bb['combos'])]}]
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
    pages = ['result_abroad.html', 'result.html'] if is_abroad_id(row['race_id']) else ['result.html']
    order, soup = {}, None
    for pg in pages:   # 海外レースは結果ページの名前が違うことがあるので、順に探す
        try:
            soup = fetch_soup(f"{site}/race/{pg}?race_id={row['race_id']}")
        except Exception:
            continue
        order = parse_result_order(soup)
        if order:
            break
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
            if b['label'] not in (VERIFY_LABEL, BEST_LABEL):
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
        if not k.startswith(VERIFY_LABEL):
            lines.append(f"{k}：的中 {pc(hit, races)} ／ 回収 {pc(g, c)}")
    ver = [(k, v) for k, v in by.items() if k.startswith(VERIFY_LABEL)]
    if ver:
        lines += ["", f"【検証中】期待値{VERIFY_EV}以上の単勝（記録用・上の合計には入っていません）"]
        for k, (races, hit, c, g) in ver:
            lines.append(f"{races}レース・{c // 100:.0f}点：的中 {pc(hit, races)} ／ 回収 {pc(g, c)}（払戻{g:,.0f}円）")
    lines += ["", "直近の結果"]
    for r in rows[-5:][::-1]:
        lines.append(f"{r['race']}　◎{r.get('honmei_pos', '?')}着　{float(r.get('ret') or 0) - float(r.get('cost') or 0):+,.0f}円")
    return "\n".join(lines)


# ═════════════════════════════════════════
# 対話型コマンド（直前に予想したレースについて「血統」「馬場」「3000円」「ハイレベル」などに答える）
# ═════════════════════════════════════════
USER_SESSIONS = {}          # {LINEのユーザーID: (保存時刻, race, arr)}
SESSION_TTL = 30 * 60


def save_session(uid, race, arr):
    now = time.time()
    for k in [k for k, v in USER_SESSIONS.items() if now - v[0] > SESSION_TTL]:
        USER_SESSIONS.pop(k, None)
    USER_SESSIONS[uid] = (now, race, arr)


def get_session(uid):
    v = USER_SESSIONS.get(uid)
    return (v[1], v[2]) if v and time.time() - v[0] <= SESSION_TTL else None


def pedigree_text(race, arr):
    wet = race['going'] != '良' or race.get('abroad')
    rows = []
    for h in arr:
        sc, why = 0.0, []
        if h['sire'] in EURO_SIRES or h['sire'] in JP_SIRE_EURO_PARENT:
            sc += 2; why.append(f"父{h['sire']}は欧州系")
        if h.get('damsire') in EURO_SIRES:
            sc += 1; why.append(f"母父{h['damsire']}は欧州系")
        if wet and h['sire'] in SIRE_MUD:
            sc += SIRE_MUD[h['sire']] / 5 * 1.5; why.append(f"父{h['sire']}は道悪巧者")
        if h['sire'] in SIRE_CLASS:
            sc += (SIRE_CLASS[h['sire']] - 3) * 0.5; why.append(f"父{h['sire']}は上級クラスの産駒が多い")
        sc += (h.get('fit', 3) - 3) * 0.5
        if h.get('fit', 3) >= 4:
            why.append('今回の距離・コースの実績あり')
        rows.append((sc, h, why))
    rows.sort(key=lambda x: -x[0])
    out = [f"🧬 血統適性ランキング（{race['venue']}{race['R']}R {race['surf']}{race['dist']}m・{race['going']}）", ""]
    for i, (sc, h, why) in enumerate(rows[:5], 1):
        out.append(f"{i}位 {h['n']}番 {h['name']}")
        out.append(f"　父{h['sire'] or '不明'}／母父{h.get('damsire') or '不明'}")
        if why:
            out.append('　' + '、'.join(why))
    return '\n'.join(out)


def going_text(race, arr):
    out = [f"🌧 馬場の見立て（{race['venue']}{race['R']}R）", "",
           f"馬場：{race['going']}" + ('（あなたが指定）' if race.get('going_manual') else '' if race['going_known'] else '（未発表のため良で計算）')]
    tb = race.get('trackBias')
    if tb:
        out.append(f"今日の{race['surf']}の傾向：{tb['text']}（終わった{tb['races']}レースから）")
    if race.get('course'):
        out.append(f"コースの特徴：{race['course']['note']}")
    if race.get('abroad'):
        out.append("欧州の馬場は雨で急に重くなります。URLの後ろに「重」などと付けて送ると、その馬場で計算し直します")
    mud = sorted([h for h in arr if h.get('mudM')], key=lambda h: -h['mudM'])[:5]
    if mud:
        out += ["", "道悪が得意そうな馬（血統・馬格・実績から）"]
        for h in mud:
            out.append(f"・{h['n']}番 {h['name']}：{'、'.join(h.get('mudParts') or ['馬格から判定'])}")
    bw = [h for h in arr if h.get('bwDiff') is not None and abs(h['bwDiff']) >= 10]
    if bw:
        out += ["", "当日の馬体重で注意"]
        for h in bw:
            out.append(f"・{h['n']}番 {h['name']}：{h['bwNow']}kg（{h['bwDiff']:+d}）")
    return '\n'.join(out)


def budget_text(race, arr, budget, unit=None, ev_mode=False):
    """予算の配分（文章版）。券種ごとに、その券種だけで予算を使う場合"""
    if budget < 100:
        return "予算は100円以上で送ってください（例：3000円）"
    plans, budget = allocate_by_kind(arr, budget, unit, ev_mode)
    if ev_mode and not plans:
        return (f"💰 期待値配分（予算{budget:,}円）\n{race['venue']}{race['R']}R {race['name']}\n\n"
                "期待値1を超える組み合わせがありません。このレースは見送りがおすすめです")
    out = [f"💰 {'期待値配分' if ev_mode else '資金配分プラン'}（予算{budget:,}円）", f"{race['venue']}{race['R']}R {race['name']}",
           f"軸：◎{arr[0]['n']} {arr[0]['name']}（全部の買い目に入ります）",
           ("期待値1を超える組み合わせだけに、期待値が高いほど多く配分しています" if ev_mode else
            "券種ごとに、その券種だけで予算を使う場合の配分です"), ""]
    for pl in plans:
        if pl.get('per'):
            out.append(f"【{pl['kind']}】1点{pl['per']:,}円×{len(pl['items'])}点＝{pl['total']:,}円（当たる見込み約{pl['hit'] * 100:.0f}%）")
        else:
            out.append(f"【{pl['kind']}】{len(pl['items'])}点＝{pl['total']:,}円（当たる見込み約{pl['hit'] * 100:.0f}%）")
        for x in pl['items']:
            out.append(f"　{combo_str(pl['kind'], x['c'])}　{x['stake']:,}円 → 約{x['ret']:,.0f}円" + (f"（期待値{x['ev']:.2f}）" if x.get('ev') else ''))
        if pl['dropped']:
            out.append(f"　※予算に入りきらない{pl['dropped']}点は外しました")
        out.append("")
    out.append("※予想配当は今の単勝オッズからの目安です。「3000円 500円ずつ」のように1点の金額も指定できます")
    return '\n'.join(out)


def highlevel_text(race, arr):
    hs = [h for h in arr if h.get('hlNotes')]
    if not hs:
        return "🔥 このレースには、ハイレベル戦を経験した馬が見つかりませんでした"
    hs.sort(key=lambda h: (h['rk'] <= 3, h['rk']))   # 人気（上位）以外を先に
    out = [f"🔥 特注馬（ハイレベル戦の経験馬）{race['venue']}{race['R']}R", ""]
    for h in hs[:6]:
        mark = MARKS[h['rk'] - 1] if h['rk'] <= len(MARKS) else '　'
        out.append(f"{mark}{h['n']}番 {h['name']}（予想{h['rk']}位・{h['o'] or '―'}倍）")
        for n_ in h['hlNotes'][:2]:
            out.append('　' + n_)
    return '\n'.join(out)


def _yen(t):
    """「3000」「3,000円」「1万円」「1万5千円」を数字に"""
    t = norm_digits(t).replace('，', ',').replace(',', '')
    m = re.fullmatch(r'(?:(\d+)万)?(?:(\d+)千)?(\d+)?円?', t)
    if not m or not any(m.groups()):
        return None
    man, sen, rest = (int(x) if x else 0 for x in m.groups())
    return man * 10000 + sen * 1000 + rest


def parse_budget_unit(text):
    """「3000円」「予算5,000円」「1万円」「3000円 500円ずつ」「3000円 各500円」→（予算, 1点の金額 or None）"""
    t = norm_digits(text.strip()).replace('　', ' ')
    if not (('円' in t) or t.startswith('予算')):
        return None, None
    t = re.sub(r'^予算\s*', '', t)
    t = re.sub(r'\s*(期待値|ケリー)(配分)?\s*', ' ', t).strip()
    t = re.sub(r'ずつ', '', t)
    t = re.sub(r'各|1点', ' ', t)
    parts = [x for x in re.split(r'\s+|(?<=円)(?=\S)', t) if x]
    if not parts or len(parts) > 2:
        return None, None
    budget = _yen(parts[0])
    unit = None
    if len(parts) == 2:
        unit = _yen(parts[1])
        if unit is None:
            return None, None
    return budget, unit


def parse_budget(text):
    return parse_budget_unit(text)[0]


# ═════════════════════════════════════════
# 展開動画（1000回シミュレーション）
#   ・中央の週末のレースは GitHub Actions が枠順確定後に作り置きして Cloudflare R2 に置く
#   ・Bot は R2 の一覧（index.json。URLは環境変数 VIDEO_INDEX_URL）を見て、あればそれを送る
#   ・作り置きが無いレースは「動画」と送ったときに、その場で作る
# ═════════════════════════════════════════
try:
    import sim_video as SV
except Exception as _e:   # numpy / imageio-ffmpeg が入っていないとき
    SV = None
    print(f"[video] 動画機能なし: {_e}", flush=True)
VIDEO_INDEX_URL = os.environ.get('VIDEO_INDEX_URL', '')


def horse_sig(arr):
    """出走馬の顔ぶれ（取消が出たら変わる）"""
    return '-'.join(str(n) for n in sorted(h['n'] for h in arr if h.get('n')))


def video_index():
    if not VIDEO_INDEX_URL:
        return {}
    def load():
        r = requests.get(VIDEO_INDEX_URL, timeout=5)
        r.raise_for_status()
        return r.json()
    try:
        return cached(('video_index',), 120, load, fail_ttl=60) or {}
    except Exception:
        return {}


def pred_shape(race):
    """予想画像の展開（ペースと逃げ馬）。作り置きの動画と食い違っていないかの確認用"""
    ld = race.get('leader')
    return f"{race.get('pace') or ''}|{ld['n'] if ld else ''}"


def prebuilt_video(race, arr):
    """作り置きの動画。顔ぶれ（取消など）や予想の展開が変わっていたら使わない（「動画」でその場で作り直す）"""
    e = video_index().get(race.get('id') or '')
    if not e or e.get('horses') != horse_sig(arr):
        return None
    if e.get('shape') and e['shape'] != pred_shape(race):
        return None
    return e


def video_message(race, arr, base, allow_make=False):
    """LINEに送る動画のメッセージ。作り置きがあればそれ、無ければ allow_make のときだけその場で作る"""
    e = prebuilt_video(race, arr)
    if e:
        return VideoSendMessage(original_content_url=e['mp4'], preview_image_url=e['jpg'])
    if not (allow_make and SV):
        return None
    t0 = time.time()
    sim = SV.simulate(race, arr)
    if not sim:
        return None
    os.makedirs(IMG_DIR, exist_ok=True)
    key = uuid.uuid4().hex
    with _render_lock:
        SV.make_video(race, arr, sim, os.path.join(IMG_DIR, key + '.mp4'), os.path.join(IMG_DIR, key + '_pv.jpg'))
    print(f"[video] その場で作成 {time.time() - t0:.1f}秒", flush=True)
    return VideoSendMessage(original_content_url=f"{base}/img/{key}.mp4", preview_image_url=f"{base}/img/{key}_pv.jpg")


def command_reply(text, uid):
    """対話型コマンドなら返事の文章を、そうでなければ None"""
    t = text.strip()
    bv = parse_budget(t)
    cmd = '血統' if t == '血統' else '馬場' if t == '馬場' else 'ハイレベル' if t in ('ハイレベル', '特注馬') \
        else '予算' if bv is not None else None
    if not cmd:
        return None
    sess = get_session(uid)
    if not sess:
        return "先にレースのURLを送ってください。予想のあと30分間は「血統」「馬場」「3000円」「ハイレベル」で詳しく答えます"
    race, arr = sess
    if cmd == '血統':
        return pedigree_text(race, arr)
    if cmd == '馬場':
        return going_text(race, arr)
    if cmd == 'ハイレベル':
        return highlevel_text(race, arr)
    return budget_text(race, arr, bv, parse_budget_unit(t)[1], bool(re.search(r'期待値|ケリー', t)))


# ═════════════════════════════════════════
# LINE
# ═════════════════════════════════════════
@app.route("/")
def health():
    return "ok"


@app.route("/health", methods=["GET", "HEAD"])
def health_check():
    """見張りサービス（UptimeRobot など）用。予想処理は動かさず、すぐ OK を返す"""
    return "OK", 200


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
    uid = getattr(event.source, 'user_id', None) or 'anon'
    threading.Thread(target=process_message, args=(event.reply_token, text, base, uid), daemon=True).start()


def build_messages(text, base, uid='anon'):
    """予想を作って、LINEに送るメッセージのリストと、返信後にやる処理を返す"""
    if text in ('成績', '成績確認'):
        return [TextSendMessage(text=stats_text())], None
    bv, unit = parse_budget_unit(text)
    ev_mode = bool(re.search(r'期待値|ケリー', text))
    sess = get_session(uid) if bv else None
    if bv and sess and bv >= 100:
        race, arr = sess
        budget = bv
        msgs = []
        try:
            if fonts_ok(20):
                with _render_lock:
                    key = save_image(render_budget_image(race, arr, budget, unit, ev_mode))
                msgs.append(ImageSendMessage(original_content_url=f"{base}/img/{key}.png",
                                             preview_image_url=f"{base}/img/{key}_pv.jpg"))
        except Exception:
            traceback.print_exc()
        msgs.append(TextSendMessage(text=budget_text(race, arr, budget, unit, ev_mode)[:4900]))
        return msgs, None
    if text.strip() in ('動画', '展開動画'):
        sess = get_session(uid)
        if not sess:
            return [TextSendMessage(text="先にレースのURLを送ってください。予想のあと30分間は「動画」で展開動画を送ります")], None
        try:
            vm = video_message(*sess, base, allow_make=True)
        except Exception as e:
            traceback.print_exc()
            vm = None
        if not vm:
            return [TextSendMessage(text="このレースの展開動画は作れませんでした")], None
        return [vm], None
    cr = command_reply(text, uid)
    if not cr and not ('netkeiba.com' in text or re.fullmatch(r'\d{12}', text.strip())):
        sess = get_session(uid)
        h = find_horse(text, sess[1]) if sess else None
        if h:   # 馬名・馬番 → その馬の詳しい根拠
            with _render_lock:
                key = save_image(horse_detail_image(sess[0], sess[1], h))
            return [ImageSendMessage(original_content_url=f"{base}/img/{key}.png",
                                     preview_image_url=f"{base}/img/{key}_pv.jpg")], None
    if cr:
        return [TextSendMessage(text=cr[:4900])], None
    msgs, race_arr = _build_prediction(text, base)
    after = None
    if race_arr:
        save_session(uid, *race_arr)
        def after():
            record_prediction(*race_arr)
            settle_pending(limit=10)
    return msgs, after


def _build_prediction(text, base):
    if not ("netkeiba.com" in text or re.fullmatch(r'\d{12}', text) or re.fullmatch(r'\d{4}[A-Z][0-9A-Za-z]{7}(\s+\S+)?', text)):
        return [TextSendMessage(text="netkeibaの出馬表のURL（またはレースID12桁）を送ってください！\n"
                                     "「成績」と送ると、これまでの予想の成績を確認できます。\n"
                                     "予想のあと30分間は「血統」「馬場」「3000円（予算）」「ハイレベル」「動画」、馬名・馬番（詳しい根拠）でも答えます。")], None
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
            imgs = [render_image(race, arr), render_short_image(race, arr)]
            try:   # 3枚目：展開図（動画と同じ「予想どおりの展開になった代表の1回」から）
                if SV:
                    sim = SV.simulate(race, arr)
                    if sim:
                        imgs.append(render_tenkai_image(race, arr, sim))
            except Exception:
                traceback.print_exc()
            keys = [save_image(im) for im in imgs]
        for key in keys:
            messages.append(ImageSendMessage(original_content_url=f"{base}/img/{key}.png",
                                             preview_image_url=f"{base}/img/{key}_pv.jpg"))
        try:   # 作り置きの展開動画があれば一緒に（LINEの返信は1回5件まで）
            vm = video_message(race, arr, base) if len(messages) <= 3 else None
            if vm:
                messages.append(vm)
        except Exception:
            traceback.print_exc()
        messages.append(TextSendMessage(text=bets_text(race, arr)))
        return messages, (race, arr)
    except Exception as e:
        # 画像づくりに失敗したら、今までどおり文章で送る（原因も添える）
        traceback.print_exc()
        return [TextSendMessage(text=f"⚠画像を作れませんでした（{type(e).__name__}: {e}）\n\n"
                                     + format_reply(race, arr))], (race, arr)


def process_message(reply_token, text, base, uid='anon'):
    t0 = time.time()
    after = None
    try:
        messages, after = build_messages(text, base, uid)
    except Exception as e:
        traceback.print_exc()
        messages = [TextSendMessage(text=f"予想中にエラーが発生しました。\n詳細: {e}")]
    print(f'[time] 予想づくり {time.time() - t0:.1f}秒', flush=True)
    try:
        line_bot_api.reply_message(reply_token, messages)
        print(f'[reply] 送信OK（{len(messages)}件・{time.time() - t0:.1f}秒）', flush=True)
    except Exception as e:
        print(f'[reply] 返信に失敗: {e}', flush=True)
        if uid and uid != 'anon':   # 時間切れなどで返信できなかったら、push で送り直す（pushは月の通数に数えられる）
            try:
                line_bot_api.push_message(uid, messages)
                print(f'[reply] pushで送り直しOK（{time.time() - t0:.1f}秒）', flush=True)
            except Exception as e2:
                print(f'[reply] pushも失敗: {e2}', flush=True)
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
        if request.args.get('n'):   # 例：&n=16 → 16番の根拠をすべて表示
            race, arr = result
            n = int(request.args['n'])
            h = next((x for x in arr if x['n'] == n), None)
            if not h:
                return f'{n}番の馬が見つかりません', 200, {'Content-Type': 'text/plain; charset=utf-8'}
            lines = [f"{n}番 {h['name']}：{h['rk']}位／{len(arr)}頭 ランク{h['rank']} スコア{h['a']:.3f}",
                     f"全成績の読み取り：{'OK' if h.get('careerOk') else '失敗（出馬表の5走で代用）'}",
                     f"騎手成績の読み取り：{h.get('jkStatus', '―')}（騎手ID {h.get('jid') or 'なし'}）",
                     f"所属：{h.get('area') or '不明'}", '']
            lines += [f"【{k}】{v}" for k, v in make_reasons(race, h)]
            return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        if request.args.get('t'):
            return format_reply(*result), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        if not fonts_ok(20):
            return '文字データを準備中です。1〜2分後に開き直してください', 200, {'Content-Type': 'text/plain; charset=utf-8'}
        with _render_lock:
            key = save_image(render_image(*result))
        return send_from_directory(IMG_DIR, key + '.png')
    except Exception:
        return traceback.format_exc(), 500, {'Content-Type': 'text/plain; charset=utf-8'}


if not os.environ.get('SYH_BACKTEST') and not os.environ.get('SYH_NOFONT'):   # パソコンで動かすときは、画像用の文字データを取りに行かない
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
