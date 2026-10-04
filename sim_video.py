# -*- coding: utf-8 -*-
"""
展開シミュレーション（1000回）と、その代表レースの動画づくり。
Bot（main.py）からも、GitHub Actions の作り置き（batch_videos.py）からも使う。

使い方:
    sim = simulate(race, arr)                 # 1000回走らせて集計
    make_video(race, arr, sim, 'out.mp4', 'out.jpg')
race / arr は main.analyze() が返すもの（各馬に n, w, name, p, earlyRatio, st が入っている）。
"""
import os
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PACE_LABEL = {'slow': 'スロー', 'base': '平均', 'fast': 'ハイ'}
STYLE_RATIO = {'逃': 0.03, '先': 0.22, '差': 0.6, '追': 0.85}
LEN_M = 2.4   # 1馬身のおおよその長さ（m）
VIS = 3.2     # 動画では差を見やすく広げて描く（馬身→画面上の距離の倍率）
BAND = 58     # コースの幅（px）
LANE_PX = 11  # 外へずらす1頭分（px）


# ═════════════════════════════════════════
# 1000回シミュレーション
# ═════════════════════════════════════════
def _early_ratio(h):
    r = h.get('earlyRatio')
    if r is None:
        r = STYLE_RATIO.get(h.get('st'), 0.5)
    return float(r)


def simulate(race, arr, runs=1000, seed=None):
    """各馬の位置取り（earlyRatio）と勝率（p）から、1000回レースを走らせて集計する。
    ・序盤：近走の位置取り＋ゆらぎ＋たまに出遅れ → 隊列とペースが決まる
    ・ゴール：勝率（Plackett-Luce：log p ＋ ガンベル乱数）に、その回のペースの有利不利を足す
    結果の形は data/simulations.json と同じ（apply_simulation でそのまま読める）＋動画用の代表レース"""
    horses = [h for h in arr if h.get('n')]
    N = len(horses)
    if N < 2:
        return None
    rng = np.random.default_rng(seed if seed is not None else int(str(race.get('id') or '0')[-6:] or 0))
    nums = np.array([h['n'] for h in horses])
    base_e = np.array([_early_ratio(h) for h in horses])
    p = np.array([max(1e-4, float(h.get('p') or 1.0 / N)) for h in horses])
    p = p / p.sum()

    # 序盤の位置取り
    E = base_e[None, :] + rng.normal(0, 0.07, (runs, N))
    late = rng.random((runs, N)) < 0.04                      # 出遅れ
    E = E + late * rng.uniform(0.2, 0.4, (runs, N))
    E = np.clip(E, 0, 1.2)
    early_rank = E.argsort(axis=1).argsort(axis=1)            # 0 = 先頭

    # ペース：前に行きたい馬（序盤の位置が0.10以内）の数で決める
    k = (E < 0.10).sum(axis=1)
    pace = np.where(k >= 3, 2, np.where(k <= 1, 0, 1))        # 0 slow / 1 base / 2 fast
    pace_sign = pace - 1                                       # ハイは差し有利、スローは前有利

    # ゴール
    gum = -np.log(-np.log(rng.random((runs, N))))
    S = np.log(p)[None, :] + gum + 0.6 * pace_sign[:, None] * (E - 0.5)
    fin_rank = (-S).argsort(axis=1).argsort(axis=1)           # 0 = 1着

    # 集計
    leader_idx = early_rank.argmin(axis=1)
    lead_cnt = np.bincount(leader_idx, minlength=N)
    pace_cnt = np.bincount(pace, minlength=3)
    pace_keys = ['slow', 'base', 'fast']
    modal_pace = int(pace_cnt.argmax())
    modal_leader = int(lead_cnt.argmax())
    win = (fin_rank == 0).mean(axis=0)
    top3 = (fin_rank <= 2).mean(axis=0)
    avg_fin = fin_rank.mean(axis=0) + 1
    avg_early = early_rank.mean(axis=0)

    order_e = np.argsort(avg_early)
    n_front, n_mid = max(2, round(N * 0.33)), max(3, round(N * 0.7))
    group = {}
    for pos, i in enumerate(order_e):
        group[i] = '先頭' if i == modal_leader else '先団' if pos < n_front else '中団' if pos < n_mid else '後方'

    # 代表の1回：最多のペース・最多の逃げ馬で、平均的な着順・隊列にいちばん近い回
    cand = np.where((pace == modal_pace) & (leader_idx == modal_leader))[0]
    if len(cand) == 0:
        cand = np.where(pace == modal_pace)[0]
    dist_ = (np.abs(fin_rank[cand] - (avg_fin - 1)[None, :]).sum(axis=1)
             + 0.5 * np.abs(early_rank[cand] - avg_early[None, :]).sum(axis=1))
    rep = int(cand[dist_.argmin()])

    # 代表レースの着差（強さの差を馬身に）
    fo = np.argsort(fin_rank[rep])
    s_sorted = S[rep][fo]
    margins = [0.0]
    for a, b in zip(s_sorted[:-1], s_sorted[1:]):
        margins.append(margins[-1] + float(np.clip((a - b) * 1.6, 0.1, 4.0)))

    out = {
        'runs': runs,
        'pace': pace_keys[modal_pace],
        'pace_share': {pace_keys[i]: float(pace_cnt[i] / runs) for i in range(3)},
        'leader': int(nums[modal_leader]),
        'leader_share': float(lead_cnt[modal_leader] / runs),
        'horses': {str(int(nums[i])): {'early': float(E[:, i].mean()), 'group': group[i],
                                       'win': float(win[i]), 'top3': float(top3[i]), 'avg': float(avg_fin[i]),
                                       'lead': float(lead_cnt[i] / runs)} for i in range(N)},
        'rep': {
            'early_order': [int(nums[i]) for i in np.argsort(early_rank[rep])],
            'finish_order': [int(nums[i]) for i in fo],
            'margins': margins,                          # 1着からの差（馬身）
            'pace': pace_keys[int(pace[rep])],
        },
    }
    return out


def summary_line(sim):
    """「シミュ1000回：5番が逃げ（46%）・ハイペース58%」"""
    if not sim:
        return ''
    return (f"シミュ{sim['runs']}回：{sim['leader']}番が逃げ（{sim['leader_share'] * 100:.0f}%）・"
            f"{PACE_LABEL[sim['pace']]}ペース{sim['pace_share'][sim['pace']] * 100:.0f}%")


# ═════════════════════════════════════════
# コース形状（JRA。周回距離・直線の長さの目安 m）
# ═════════════════════════════════════════
RIGHT_TURN = {'札幌', '函館', '福島', '中山', '京都', '阪神', '小倉'}   # 右回り（東京・新潟・中京は左回り）
TRACK = {   # (周回, 直線)
    ('札幌', '芝'): (1641, 266), ('函館', '芝'): (1627, 262), ('福島', '芝'): (1600, 292),
    ('新潟', '芝'): (1623, 359), ('新潟', '芝外'): (2223, 659), ('東京', '芝'): (2083, 525),
    ('中山', '芝'): (1667, 310), ('中山', '芝外'): (1840, 310), ('中京', '芝'): (1705, 412),
    ('京都', '芝'): (1783, 328), ('京都', '芝外'): (1894, 404), ('阪神', '芝'): (1689, 356),
    ('阪神', '芝外'): (2089, 473), ('小倉', '芝'): (1615, 293),
    ('札幌', 'ダ'): (1487, 264), ('函館', 'ダ'): (1476, 260), ('福島', 'ダ'): (1444, 295),
    ('新潟', 'ダ'): (1472, 354), ('東京', 'ダ'): (1899, 501), ('中山', 'ダ'): (1493, 308),
    ('中京', 'ダ'): (1530, 410), ('京都', 'ダ'): (1608, 329), ('阪神', 'ダ'): (1517, 352),
    ('小倉', 'ダ'): (1445, 291),
}


def track_of(race):
    venue, surf = race.get('venue', ''), race.get('surf', '芝')
    outer = '外' in (race.get('courseNote') or '') and surf == '芝'
    key = (venue, '芝外' if outer else surf)
    P, S = TRACK.get(key) or TRACK.get((venue, surf)) or (1700, 350)
    straight_course = venue == '新潟' and surf == '芝' and int(race.get('dist') or 0) == 1000
    return {'P': P, 'S': S, 'right': venue in RIGHT_TURN, 'straight': straight_course}


class Course:
    """スタジアム形（直線2本＋半円2つ）。ゴールはホーム直線（画面下）の端"""

    def __init__(self, tr, box):
        self.tr = tr
        x0, y0, x1, y1 = box
        if tr['straight']:
            self.L = 1000.0
            self.x0, self.x1, self.y = x0 + 20, x1 - 20, (y0 + y1) / 2
            self.pxm = (self.x1 - self.x0) / self.L
            return
        P, S = tr['P'], tr['S']
        r = max(60.0, (P - 2 * S) / (2 * math.pi))
        self.S, self.r, self.P = S, r, 2 * S + 2 * math.pi * r
        sc = min((x1 - x0 - 2 * BAND) / (S + 2 * r), (y1 - y0 - 2 * BAND) / (2 * r))
        self.sc = self.pxm = sc
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.cxL, self.cxR, self.cy, self.rp = cx - S * sc / 2, cx + S * sc / 2, cy, r * sc

    def point(self, s, lane=0.0):
        """ゴールまで残り s m の地点の画面座標。lane は外側へのずれ（px）"""
        if self.tr['straight']:
            t = 1 - s / self.L
            return self.x0 + (self.x1 - self.x0) * t, self.y - 50 + lane
        q = (-s) % self.P            # ゴールから進行方向へ q m（左回りで計算して、右回りは左右反転）
        S, r, sc = self.S, self.r, self.sc
        R = self.rp + lane
        if q < math.pi * r:          # 右の半円（下→上）
            a = q / r
            x, y = self.cxR + R * math.sin(a), self.cy + R * math.cos(a)
        elif q < math.pi * r + S:    # 向正面（右→左）
            d = (q - math.pi * r) * sc
            x, y = self.cxR - d, self.cy - R
        elif q < 2 * math.pi * r + S:   # 左の半円（上→下）
            a = (q - math.pi * r - S) / r
            x, y = self.cxL - R * math.sin(a), self.cy - R * math.cos(a)
        else:                        # ホーム直線（左→右）
            d = (q - 2 * math.pi * r - S) * sc
            x, y = self.cxL + d, self.cy + R
        if self.tr['right']:
            cx = (self.cxL + self.cxR) / 2
            x = 2 * cx - x
        return x, y

    def draw(self, d):
        if self.tr['straight']:
            d.rounded_rectangle((self.x0 - 10, self.y - 60, self.x1 + 10, self.y + 30), radius=12,
                                fill=(28, 62, 40), outline=(90, 120, 90), width=2)
            return
        ss = np.linspace(0, self.P, 360)
        inner = [self.point(s, -8) for s in ss]
        outer = [self.point(s, BAND - 4) for s in ss]
        d.polygon(outer + inner[::-1], fill=(34, 72, 46))
        for pts in (inner, outer):
            d.line(pts + [pts[0]], fill=(150, 160, 176), width=2)
        fx, fy = self.point(0, -8)
        fx2, fy2 = self.point(0, BAND - 4)
        d.line((fx, fy, fx2, fy2), fill=(255, 255, 255), width=3)


# ═════════════════════════════════════════
# 動画
# ═════════════════════════════════════════
W, H, FPS = 1024, 576, 15
BG_TOP, BG_BOT = (20, 30, 58), (6, 9, 18)
GOLD, GOLD_D, SILVER, MUTED = (221, 183, 98), (150, 116, 52), (232, 234, 242), (150, 160, 184)
WAKU = {1: ((245, 245, 245), (20, 20, 20)), 2: ((30, 30, 30), (255, 255, 255)), 3: ((214, 48, 49), (255, 255, 255)),
        4: ((41, 98, 214), (255, 255, 255)), 5: ((247, 206, 38), (20, 20, 20)), 6: ((38, 160, 80), (255, 255, 255)),
        7: ((245, 142, 38), (20, 20, 20)), 8: ((240, 128, 170), (20, 20, 20))}
FONT_CANDS = {
    'b': ['fonts/NotoSansCJKjp-Bold.otf', '/tmp/NotoSansCJKjp-Bold.otf',
          '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'],
    'r': ['fonts/NotoSansCJKjp-Regular.otf', '/tmp/NotoSansCJKjp-Regular.otf',
          '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'],
}
_fonts = {}


def font(kind, size):
    key = (kind, size)
    if key not in _fonts:
        here = os.path.dirname(os.path.abspath(__file__))
        for c in FONT_CANDS[kind]:
            path = c if os.path.isabs(c) else os.path.join(here, c)
            if os.path.exists(path):
                _fonts[key] = ImageFont.truetype(path, size, index=0)
                break
        else:
            raise RuntimeError('日本語フォントが見つかりません')
    return _fonts[key]


def _bg():
    col = Image.new('RGB', (1, 256))
    col.putdata([tuple(int(BG_TOP[i] * (1 - t / 255) + BG_BOT[i] * t / 255) for i in range(3)) for t in range(256)])
    return col.resize((W, H), Image.BILINEAR)


def _smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _plan(race, arr, sim):
    """代表レースの各馬の「先頭からの差（m）」を、序盤・道中・ゴールで決める"""
    rep = sim['rep']
    N = len(rep['early_order'])
    spread = {'fast': 1.45, 'base': 1.15, 'slow': 0.85}[rep['pace']]
    rng = np.random.default_rng(len(rep['finish_order']) * 7 + N)
    g_early, acc = {}, 0.0
    for i, n in enumerate(rep['early_order']):
        acc += 0 if i == 0 else VIS * LEN_M * spread * float(rng.uniform(0.6, 1.5))
        g_early[n] = acc
    g_fin = {n: rep['margins'][i] * LEN_M * VIS for i, n in enumerate(rep['finish_order'])}
    gate = {h['n']: (h.get('w') or 1, h['n']) for h in arr if h.get('n')}
    return g_early, g_fin, gate


def make_video(race, arr, sim, out_mp4, out_jpg=None, seconds=14):
    import imageio_ffmpeg
    dist = int(race.get('dist') or 1600)
    tr = track_of(race)
    course = Course(tr, (40, 104, W - 40, H - 108 + BAND))
    g_early, g_fin, gate = _plan(race, arr, sim)
    by_n = {h['n']: h for h in arr if h.get('n')}
    nums = list(g_early.keys())

    # 動かない部分（背景・コース・見出し）は1枚だけ作って使い回す
    base = _bg()
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((8, 8, W - 8, H - 8), radius=18, outline=GOLD_D, width=2)
    course.draw(d)
    date = race.get('date')
    head = f"{date:%m/%d} " if date and hasattr(date, 'strftime') else ''
    head += f"{race.get('venue', '')}{race.get('R', '')}R  {race.get('name', '')}"
    d.text((28, 20), head[:40], font=font('b', 30), fill=GOLD)
    sub = (f"{race.get('surf', '')}{dist}m  {race.get('going', '')}  {len(nums)}頭   "
           f"{'右回り' if tr['right'] else '左回り'}{'（直線）' if tr['straight'] else ''}")
    d.text((30, 60), sub, font=font('r', 20), fill=SILVER)
    ps = sim['pace_share'][sim['pace']] * 100
    tag = f"シミュ{sim['runs']}回  最多展開：{PACE_LABEL[sim['pace']]}ペース {ps:.0f}%  ／  逃げ {sim['leader']}番 {sim['leader_share'] * 100:.0f}%"
    tw_ = d.textlength(tag, font=font('b', 19))
    d.rounded_rectangle((W - 36 - tw_ - 24, 58, W - 30, 90), radius=14, fill=(48, 38, 16), outline=GOLD_D)
    d.text((W - 36 - tw_ - 12, 62), tag, font=font('b', 19), fill=GOLD)
    d.text((28, H - 32), '※1000回で最も多かった展開に近い1回を再現。コース形状は目安です', font=font('r', 15), fill=MUTED)

    f_num = font('b', 15)
    f_small = font('b', 13)
    n_run = int(FPS * seconds)
    n_hold = FPS * 4
    lanes = {n: 6 + (gate[n][1] - 1) * (BAND - 18) / max(1, len(nums) - 1) for n in nums}

    writer = imageio_ffmpeg.write_frames(
        out_mp4, (W, H), fps=FPS, codec='libx264', pix_fmt_in='rgb24', pix_fmt_out='yuv420p',
        output_params=['-movflags', '+faststart', '-crf', '27', '-preset', 'veryfast'], macro_block_size=16)
    writer.send(None)
    last = None
    try:
        for f in range(n_run + n_hold):
            u = min(f / n_run, 1.0) * 1.03            # 先頭の進み具合（少しだけゴールを過ぎるまで）
            front = u * dist
            t_form = _smooth(u / 0.12)                # 序盤で隊列ができる
            t_fin = _smooth((u - 0.62) / 0.38)        # 直線に向けて着順の形へ
            pos = {}
            for n in nums:
                gap = g_early[n] * t_form * (1 - t_fin) + g_fin[n] * t_fin
                pos[n] = min(dist + 3 * VIS, front - gap)   # スタートからの距離
            # 並んだ馬は外へずらす（前の馬から順に空いているレーンへ）
            order = sorted(nums, key=lambda n: -pos[n])
            placed = []
            for n in order:
                ln = 0
                while any(abs(pos[m] - pos[n]) * course.pxm < 23 and abs(lanes_t - ln) < 0.5 for m, lanes_t in placed):
                    ln += 1
                if u < 0.04:                          # スタート直後はゲートの並び
                    target = 6 + (gate[n][1] - 1) * (BAND - 18) / max(1, len(nums) - 1)
                else:
                    target = 6 + min(ln, 3) * LANE_PX
                lanes[n] += (target - lanes[n]) * 0.25
                placed.append((n, ln))
            img = base.copy()
            dd = ImageDraw.Draw(img)
            for n in reversed(order):                 # 後ろの馬から描いて、前の馬を上に
                x, y = course.point(dist - pos[n], lanes[n])
                w = by_n.get(n, {}).get('w') or 1
                fc, tc = WAKU.get(w, WAKU[1])
                dd.ellipse((x - 11, y - 11, x + 11, y + 11), fill=fc, outline=GOLD if n == order[0] else (20, 20, 20), width=2)
                t = str(n)
                dd.text((x - dd.textlength(t, font=f_num) / 2, y - 10), t, font=f_num, fill=tc)
            # 残り距離と隊列（下の帯）
            remain = max(0, dist - front)
            lab = 'ゴール' if remain <= 0 else f"残り {remain:,.0f}m"
            dd.text((W - 30 - dd.textlength(lab, font=font('b', 24)), H - 66), lab, font=font('b', 24), fill=SILVER)
            x0 = 30
            dd.text((x0, H - 62), '隊列', font=font('b', 16), fill=MUTED)
            for i, n in enumerate(order):
                cx_ = x0 + 52 + i * 31
                w = by_n.get(n, {}).get('w') or 1
                fc, tc = WAKU.get(w, WAKU[1])
                dd.ellipse((cx_ - 12, H - 66, cx_ + 12, H - 42), fill=fc, outline=(20, 20, 20))
                dd.text((cx_ - dd.textlength(str(n), font=f_small) / 2, H - 63), str(n), font=f_small, fill=tc)
            if f >= n_run:                            # 最後の4秒：結果
                _result_panel(img, sim, by_n, (f - n_run) / FPS)
            last = img
            writer.send(img.tobytes())
    finally:
        writer.close()
    if out_jpg and last is not None:
        pv = last.copy()
        pv.thumbnail((640, 640))
        pv.save(out_jpg, quality=82)
    return out_mp4


def _result_panel(img, sim, by_n, t):
    a = int(min(1.0, t / 0.4) * 225)
    ov = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    bx = (W * 0.18, 120, W * 0.82, H - 84)
    d.rounded_rectangle(bx, radius=18, fill=(10, 14, 28, a), outline=GOLD_D + (a,), width=2)
    img.paste(Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB'))
    if t < 0.3:
        return
    d = ImageDraw.Draw(img)
    x0, y = bx[0] + 30, bx[1] + 20
    d.text((x0, y), '代表レースの着順', font=font('b', 24), fill=GOLD)
    d.text((bx[2] - 30 - d.textlength('1000回の勝率／3着内率', font=font('r', 16)), y + 8),
           '1000回の勝率／3着内率', font=font('r', 16), fill=MUTED)
    y += 46
    rep = sim['rep']
    for i, n in enumerate(rep['finish_order'][:5]):
        h = by_n.get(n, {})
        st = sim['horses'].get(str(n), {})
        fc, tc = WAKU.get(h.get('w') or 1, WAKU[1])
        d.text((x0, y), f"{i + 1}着", font=font('b', 22), fill=SILVER if i else GOLD)
        d.ellipse((x0 + 62, y + 2, x0 + 90, y + 30), fill=fc, outline=(20, 20, 20))
        d.text((x0 + 76 - d.textlength(str(n), font=font('b', 16)) / 2, y + 5), str(n), font=font('b', 16), fill=tc)
        d.text((x0 + 104, y), (h.get('name') or '')[:12], font=font('b', 22), fill=SILVER)
        mg = '' if i == 0 else f"+{rep['margins'][i]:.1f}馬身"
        d.text((x0 + 330, y + 3), mg, font=font('r', 18), fill=MUTED)
        r_ = f"{st.get('win', 0) * 100:.0f}% ／ {st.get('top3', 0) * 100:.0f}%"
        d.text((bx[2] - 30 - d.textlength(r_, font=font('b', 20)), y + 2), r_, font=font('b', 20), fill=GOLD)
        y += 46
