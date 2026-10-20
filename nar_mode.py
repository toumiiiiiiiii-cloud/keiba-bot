# -*- coding: utf-8 -*-
"""
地方競馬モード（帯広＝ばんえいは除く）
  ・地方競馬の結果（2022年〜）で学習した「オッズ＋地方向けの材料」のAI
  ・材料の計算はこのファイルの nar_features() の1か所だけ。学習（PC/作業用）もBotも同じものを使う
  ・表（騎手・調教師の勝率、基準タイム、競馬場ごとの前有利・内有利）と重みは data/nar_model.json
"""
import json
import math
import os
import re

import numpy as np

CODE = {'門別': '30', '盛岡': '35', '水沢': '36', '浦和': '42', '船橋': '43', '大井': '44', '川崎': '45',
        '金沢': '46', '笠松': '47', '名古屋': '48', '園田': '50', '姫路': '51', '高知': '54', '佐賀': '55'}
CIR = {'30': '北海道', '35': '岩手', '36': '岩手', '42': '南関', '43': '南関', '44': '南関', '45': '南関',
       '46': '金沢', '47': '東海', '48': '東海', '50': '兵庫', '51': '兵庫', '54': '高知', '55': '佐賀'}
FN = ['log_market', '騎手の勝率（この競馬場）', '騎手の勝率（メンバー内）', '乗り替わり', '騎手の強化', '調教師の勝率',
      '前走着順', '前走の着差（秒）', '前走人気', '実際の先行力（直近3走）', '先行力（メンバー内）', '先行力×この競馬場の前有利',
      '枠×この競馬場の内有利', 'タイム指数・最高', 'タイム指数・平均（メンバー内）', 'この競馬場の複勝率', '他地区からの転入',
      'データの無い新顔', '休み（log）', '馬体重（メンバー内）', '馬体重の増減', '斤量（メンバー内）', '年齢', '牝馬',
      '前走で先行して負けた']

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'nar_model.json')
M = None


def load():
    global M
    if M is None and os.path.exists(PATH):
        try:
            M = json.load(open(PATH, encoding='utf-8'))
        except Exception as e:
            print(f'[nar] 読み込めず: {e}', flush=True)
            M = {}
    return M or {}


def going_grp(g):
    g = (g or '良')[:1]
    return '良' if g == '良' else '稍' if g == '稍' else '重'


def first_corner(ps, N):
    """通過順（"3-3-2" やコーナー表の並び）から、最初のコーナーの位置（0＝先頭〜1＝最後方）"""
    m = re.match(r'\s*(\d+)', ps or '')
    if not m or not N or N < 2:
        return None
    return (int(m.group(1)) - 1) / (N - 1)


def rate(tab, key):
    w, n = (tab or {}).get(key, (0, 0))
    return (w + 0.8) / (n + 10)


def nar_features(race, horses, tables, as_of=None):
    """race: {'v':競馬場コード, 'surf':'ダ'/'芝', 'dist':int, 'N':頭数}
    horses: [{'o':単勝オッズ, 'n':馬番, 'jid','trid','bw','bwd','wt','age','female',
              'past':[{'d','v','pos','N','pop','gap','ps','fig','surf','jid'} 新しい順ではなく古い順]}]
    戻り値：材料の行列（len(horses) × len(FN)）"""
    v = race['v']
    N = race.get('N') or len(horses)
    jk = tables.get('jk', {})
    tr = tables.get('tr', {})
    VF = tables.get('VF', {})
    VD = tables.get('VD', {})
    inv = np.array([1.0 / h['o'] for h in horses])
    lm = np.log(inv / inv.sum())
    rows = []
    for k, h in enumerate(horses):
        f = [0.0] * len(FN)
        f[0] = lm[k]
        f[1] = rate(jk, f"{h.get('jid')}|{v}")
        f[5] = rate(tr, h.get('trid') or '')
        past = h.get('past') or []
        if past:
            l = past[-1]
            if l.get('jid') and h.get('jid') and l['jid'] != h['jid']:
                f[3] = 1.0
                f[4] = f[1] - rate(jk, f"{l['jid']}|{v}")
            f[6] = (l['pos'] / l['N']) if l.get('pos') else 1.0
            f[7] = min(max(l['gap'], 0.0), 5.0) if l.get('gap') is not None else 3.0
            f[8] = (l.get('pop') or l['N']) / l['N']
            eps = [first_corner(x.get('ps'), x['N']) for x in past[-3:]]
            eps = [e for e in eps if e is not None]
            f[9] = 1 - float(np.mean(eps)) if eps else 0.5
            figs = [x['fig'] for x in past[-6:] if x.get('fig') is not None and x.get('surf') == race['surf']]
            f[13] = max(figs) if figs else np.nan
            f[14] = float(np.mean(sorted(figs)[-3:])) if figs else np.nan
            same = [x for x in past[-10:] if x.get('v') == v]
            f[15] = (sum(1 for x in same if x.get('pos') and x['pos'] <= 3) + 0.3) / (len(same) + 1)
            f[16] = 1.0 if CIR.get(l.get('v')) != CIR.get(v) else 0.0
            f[18] = math.log(1 + max(0, h.get('days') or 0))
            e0 = first_corner(l.get('ps'), l['N'])
            f[24] = 1.0 if (e0 is not None and e0 <= 0.25 and l.get('pos') and l['pos'] > 3) else 0.0
        else:
            f[17] = 1.0
            f[9] = 0.5
            f[13] = f[14] = np.nan
            f[6] = f[8] = 0.5
            f[15] = 0.3
        n_ = h.get('n') or 1
        f[12] = (1 - (n_ - 1) / max(N - 1, 1)) * (VD.get(v, 0.3) - 0.3) * 10
        f[19] = h.get('bw') or 0
        f[20] = h.get('bwd') or 0
        f[21] = h.get('wt') or 0
        f[22] = h.get('age') or 4
        f[23] = 1.0 if h.get('female') else 0.0
        rows.append(f)
    X = np.array(rows, dtype=float)
    for j in (19, 21):
        X[:, j] -= np.nanmean(X[:, j])
    X[:, 2] = X[:, 1] - np.median(X[:, 1])
    X[:, 10] = X[:, 9] - np.median(X[:, 9])
    X[:, 11] = X[:, 10] * (VF.get(v, 0.3) - 0.3) * 10
    for j in (13, 14):
        col = X[:, j]
        ok = ~np.isnan(col)
        m = np.median(col[ok]) if ok.any() else 0.0
        X[:, j] = np.where(ok, col - m, -5.0)
    X = np.nan_to_num(X)
    lo, hi = tables.get('clip_lo'), tables.get('clip_hi')
    if lo and hi:
        X = np.clip(X, np.array(lo), np.array(hi))
    return X


def predict(X, model):
    mu, sd, b = np.array(model['mean']), np.array(model['std']), np.array(model['beta'])
    z = (X - mu) / sd
    z[:, 0] = X[:, 0]
    s = z @ b
    e = np.exp(s - s.max())
    return e / e.sum()


def fig_of(tables, vcode, surf, dist, going, t):
    b = tables.get('base', {}).get(f"{vcode}|{surf}|{int(dist)}|{going_grp(going)}")
    if not b or not t:
        return None
    return (b - t) / (dist / 1000) * 10


# ── 買い方（2024・2025・2026年の答え合わせ） ──
PUSH_EV = 1.0          # 期待値（AIの勝率×単勝オッズ）
PUSH_MAX_ODDS = 15.0   # 単勝15倍以下
PUSH_MIN_RANK = 4      # AIの4位以下（人気の盲点になっている中位の馬）
FUN_MIN_FAV = 0.40     # 楽しむ用の馬単：1番人気の勝率（オッズから）40%以上
FUN_EDGE = 1.2         # AIの見込み÷市場の見込みが1.2以上の目だけ
BUDGET_PUSH = 1000     # 推奨の単勝：1レースあたりの金額
BUDGET_FUN = 1000      # 楽しむ用の馬単：1レースあたりの金額


def unit100(x):
    return max(100, int(round(x / 100.0)) * 100)


def exacta_fun(p_ai, p_mk, nums):
    """1番人気を1着に固定した馬単（相手はAI上位5頭）。お得度1.2以上の目だけ、お得度に比例して配分
    戻り値：[(1着, 2着, 金額, お得度)] または []"""
    h = int(np.argmax(p_mk))
    if p_mk[h] < FUN_MIN_FAV or p_ai[h] < p_mk[h]:
        return []
    order = [j for j in np.argsort(-p_ai) if j != h][:5]
    cand = []
    for j in order:
        a = p_ai[h] * p_ai[j] / max(1e-9, 1 - p_ai[h])
        b = p_mk[h] * p_mk[j] / max(1e-9, 1 - p_mk[h])
        if b > 0 and a / b >= FUN_EDGE:
            cand.append((int(nums[h]), int(nums[j]), a - b, a / b))
    if not cand:
        return []
    tot = sum(c[2] for c in cand)
    return [(x, y, unit100(BUDGET_FUN * w / tot), e) for x, y, w, e in cand]
