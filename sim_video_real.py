# -*- coding: utf-8 -*-
"""
中継風のレース動画（スマホ向けの縦長。横から馬群を追いかけるカメラ＋順位・実況）
  ・走る馬と騎手を勝負服の色で描く（ゼッケンに馬番）
  ・カメラは先頭集団を追いかける。手前の外ラチ・奥の内ラチ・スタンドが流れてスピード感を出す
  ・画面の下に今の並び順、右上に残り距離。ゴール板を過ぎたら着順→全頭の1・2・3着率
  使う数字は sim_video.simulate の代表レース（rep）と同じ。結果のパネルも sim_video のものを使う
"""
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import sim_video as SV

W, H, FPS = SV.W, SV.H, 20
LEN_M = 2.4                    # 1馬身（m）
PPM = 30.0                     # 1mを何ピクセルで描くか（いちばん手前のレーン）
# 米国のゼッケンの色（馬番ごと）。日本のレースは枠の色
US_CLOTH = {1: (200, 30, 40), 2: (245, 245, 245), 3: (30, 70, 190), 4: (245, 210, 40), 5: (30, 140, 70),
            6: (25, 25, 25), 7: (245, 130, 30), 8: (240, 120, 170), 9: (40, 190, 190), 10: (120, 50, 160),
            11: (150, 150, 150), 12: (140, 210, 60), 13: (120, 75, 40), 14: (120, 20, 40), 15: (190, 175, 120),
            16: (40, 60, 110)}
COATS = [(92, 52, 28), (120, 66, 32), (60, 36, 22), (150, 84, 40), (40, 30, 26), (170, 160, 150), (104, 58, 30)]


def _txt_col(c):
    return (20, 20, 20) if sum(c) > 420 else (255, 255, 255)


def _colors(race, h):
    n = h.get('n') or 1
    if race.get('abroad') or str(race.get('id', ''))[4:5].isalpha():
        cloth = US_CLOTH.get(n, (200, 200, 200))
    else:
        cloth = SV.WAKU.get(h.get('w') or 1, SV.WAKU[1])[0]
    silk = cloth
    cap = tuple(int(c * 0.6) for c in cloth) if sum(cloth) > 300 else tuple(min(255, c + 90) for c in cloth)
    coat = COATS[(n * 5 + len(h.get('name') or '')) % len(COATS)]
    return cloth, silk, cap, coat


# ─────────────────────────────────────────
# 馬の絵（4倍で描いて縮小＝なめらか）。phase 0〜1 で1完歩
# ─────────────────────────────────────────
def _leg(d, hip, a1, a2, l1, l2, col, w):
    x0, y0 = hip
    x1, y1 = x0 + l1 * math.sin(a1), y0 + l1 * math.cos(a1)
    x2, y2 = x1 + l2 * math.sin(a2), y1 + l2 * math.cos(a2)
    d.line((x0, y0, x1, y1), fill=col, width=w)
    d.line((x1, y1, x2, y2), fill=col, width=int(w * 0.7))
    d.ellipse((x2 - w * 0.5, y2 - w * 0.3, x2 + w * 0.5, y2 + w * 0.4), fill=(30, 26, 24))


def horse_sprite(phase, coat, silk, cap, cloth, num, fnt, S=4):
    """幅 140 × 高さ 120（S倍で描画）。地面は下端。右向き"""
    w_, h_ = 140 * S, 120 * S
    im = Image.new('RGBA', (w_, h_), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    P = lambda x, y: (x * S + 60 * S, y * S + 112 * S)       # 原点＝胴の下の地面
    ph = phase * 2 * math.pi
    bob = 2.5 * math.sin(2 * ph)
    dark = tuple(int(c * 0.72) for c in coat)
    lw = int(7 * S)
    # 奥の脚（暗い色）
    _leg(d, P(-30, -52 + bob), 0.55 * math.sin(ph + 0.6) - 0.1, 0.55 * math.sin(ph + 0.6) + 0.35 + 0.35 * max(0, math.sin(ph + 2.2)),
         22 * S, 26 * S, dark, lw)
    _leg(d, P(30, -52 + bob), 0.7 * math.sin(ph + 2.4) + 0.1, 0.7 * math.sin(ph + 2.4) - 0.5 * max(0, math.sin(ph + 0.9)),
         22 * S, 26 * S, dark, lw)
    # 尾
    sw = 4 * math.sin(ph)
    d.polygon([P(-44, -70 + bob), P(-62, -58 + bob + sw), P(-66, -40 + bob + sw), P(-52, -56 + bob), P(-42, -62 + bob)], fill=dark)
    # 胴・尻・胸
    d.ellipse([*P(-44, -78 + bob), *P(40, -48 + bob)], fill=coat)
    d.ellipse([*P(-50, -80 + bob), *P(-16, -48 + bob)], fill=coat)
    d.ellipse([*P(16, -80 + bob), *P(46, -50 + bob)], fill=coat)
    # 首と頭（完歩に合わせて少し上下）
    nk = 3 * math.sin(ph + 1.0)
    d.polygon([P(22, -74 + bob), P(52, -98 + bob + nk), P(62, -92 + bob + nk), P(44, -58 + bob)], fill=coat)
    d.polygon([P(50, -102 + bob + nk), P(60, -104 + bob + nk), P(80, -86 + bob + nk), P(76, -80 + bob + nk), P(58, -88 + bob + nk)], fill=coat)
    d.polygon([P(52, -102 + bob + nk), P(54, -110 + bob + nk), P(58, -103 + bob + nk)], fill=dark)       # 耳
    d.line([P(36, -86 + bob + nk), P(54, -100 + bob + nk)], fill=dark, width=int(3 * S))                # たてがみ
    # ゼッケン（馬番）
    d.polygon([P(-14, -76 + bob), P(16, -76 + bob), P(14, -56 + bob), P(-12, -56 + bob)], fill=cloth, outline=(30, 30, 30))
    t = str(num)
    cx, cy = P(1, -66 + bob)
    tw = d.textlength(t, font=fnt)
    d.text((cx - tw / 2, cy - fnt.size * 0.62), t, font=fnt, fill=_txt_col(cloth))
    # 手前の脚
    _leg(d, P(-28, -54 + bob), 0.55 * math.sin(ph) - 0.1, 0.55 * math.sin(ph) + 0.35 + 0.35 * max(0, math.sin(ph + 1.6)),
         22 * S, 27 * S, coat, lw)
    _leg(d, P(32, -54 + bob), 0.7 * math.sin(ph + 1.8) + 0.1, 0.7 * math.sin(ph + 1.8) - 0.5 * max(0, math.sin(ph + 0.3)),
         22 * S, 27 * S, coat, lw)
    # 騎手（前かがみ）
    jb = bob * 0.5
    d.polygon([P(-4, -80 + jb), P(26, -96 + jb), P(32, -88 + jb), P(4, -74 + jb)], fill=silk, outline=tuple(int(c * 0.6) for c in silk))
    d.line([P(-2, -78 + jb), P(-8, -66 + jb), P(2, -60 + jb)], fill=(240, 240, 235), width=int(5 * S))   # 脚（白いズボン）
    d.line([P(24, -92 + jb), P(40, -86 + jb), P(50, -88 + bob + nk)], fill=silk, width=int(4 * S))       # 腕
    d.ellipse([*P(27, -106 + jb), *P(39, -94 + jb)], fill=cap)                                          # 帽子
    d.line([P(50, -90 + bob + nk), P(62, -94 + bob + nk)], fill=(60, 40, 30), width=int(1.5 * S))     # 手綱
    return im.resize((140, 120), Image.LANCZOS)


# ─────────────────────────────────────────
# 縦長（スマホ向け 720×1280）のレイアウト
#   見出し → 残り距離のバー → 位置取りの帯（＋上位3頭のオッズ）→ レース映像（＋コース図）
#   → 今の順位（2列）→ 実況 → 下の帯
# ─────────────────────────────────────────
VW, VH = 720, 1280
SCN_Y, SCN_H = 318, 500                # レース映像の位置と高さ
Y_INNER, Y_OUTER = 205, 470            # 映像の中での内ラチ・外ラチ（手前）の高さ
BG = (14, 14, 16)
PANEL = (24, 24, 28)
GOLD_BAR = (236, 190, 100)


def _sky(w, h):
    top, bot = np.array((96, 160, 226)), np.array((206, 228, 244))
    a = np.linspace(0, 1, h)[:, None]
    return Image.fromarray(np.repeat((top * (1 - a) + bot * a)[:, None, :], w, axis=1).astype(np.uint8))


def _stand_strip(seed, w=2600):
    """スタンド＋木（横に長い1枚。カメラに合わせてゆっくり流す）"""
    rng = np.random.default_rng(seed)
    im = Image.new('RGBA', (w, 170), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 0
    while x < w:                                    # 木
        r = int(rng.integers(16, 30))
        cx = x + r
        d.rectangle((cx - 3, 130, cx + 3, 165), fill=(110, 80, 50))
        for dx, dy in ((-r * 0.5, 0), (r * 0.5, 0), (0, -r * 0.6)):
            d.ellipse((cx + dx - r * 0.75, 112 + dy - r * 0.75, cx + dx + r * 0.75, 112 + dy + r * 0.75), fill=(66, 150, 70))
        x += int(rng.integers(40, 90))
    for sx in range(200, w - 700, 1100):            # スタンド
        sw = 620
        d.polygon([(sx, 70), (sx + sw, 70), (sx + sw + 30, 52), (sx - 10, 58)], fill=(40, 40, 46))
        d.rectangle((sx, 70, sx + sw, 160), fill=(214, 206, 188))
        for y in range(78, 156, 9):
            d.line((sx + 6, y, sx + sw - 6, y), fill=(150, 140, 126), width=2)
        d.rectangle((sx - 90, 40, sx - 6, 160), fill=(120, 150, 196))
        d.rectangle((sx - 80, 20, sx - 20, 46), fill=(100, 132, 180))
        px = im.load()
        for _ in range(5000):                       # 観客
            xx, yy = int(rng.integers(sx + 6, sx + sw - 6)), int(rng.integers(80, 154))
            px[xx, yy] = tuple(int(v) for v in rng.integers(60, 240, 3)) + (255,)
    return im


def _scene_ground(w, surf):
    im = Image.new('RGB', (w, SCN_H - 150))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, Y_INNER - 150), fill=(118, 188, 92))
    top, bot = ((196, 160, 116), (170, 132, 92)) if surf == 'ダ' else ((118, 196, 96), (96, 178, 80))
    for y in range(Y_INNER - 150, SCN_H - 150):
        t = (y - (Y_INNER - 150)) / max(1, SCN_H - Y_INNER)
        d.line((0, y, w, y), fill=tuple(int(a * (1 - t) + b * t) for a, b in zip(top, bot)))
    if surf == 'ダ':                               # 内側の芝コース（色違いの帯）
        d.rectangle((0, Y_INNER - 150 - 22, w, Y_INNER - 150 - 6), fill=(132, 200, 104))
    return im


def _rail(d, cam_x, y, ppm, h, w, step=12.0):
    d.rectangle((0, y - h, w, y - h + max(3, h // 5)), fill=(252, 252, 252))
    x = -((cam_x * ppm) % (step * ppm))
    while x < w:
        d.line((x, y - h, x, y), fill=(232, 232, 232), width=max(2, h // 7))
        x += step * ppm


def _mini_course(dist, tr, w=220, h=104):
    """コース図（スタジアム形）。戻り値：(画像, 進んだ距離→座標の関数)"""
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, fill=(16, 40, 24, 200))
    P, S = tr['P'], tr['S']
    r = max(60.0, (P - 2 * S) / (2 * math.pi))
    sc = min((w - 30) / (S + 2 * r), (h - 30) / (2 * r))
    cx, cy = w / 2, h / 2 + 4
    L = 2 * S + 2 * math.pi * r

    def pt(q):                                      # ゴールから進行方向（左回り）へ q m
        q %= L
        if q < math.pi * r:
            a = q / r
            x, y = cx + S * sc / 2 + r * sc * math.sin(a), cy + r * sc * math.cos(a)
        elif q < math.pi * r + S:
            x, y = cx + S * sc / 2 - (q - math.pi * r) * sc, cy - r * sc
        elif q < 2 * math.pi * r + S:
            a = (q - math.pi * r - S) / r
            x, y = cx - S * sc / 2 - r * sc * math.sin(a), cy - r * sc * math.cos(a)
        else:
            x, y = cx - S * sc / 2 + (q - 2 * math.pi * r - S) * sc, cy + r * sc
        if tr.get('right'):
            x = 2 * cx - x
        return x, y
    pts = [pt(L * i / 200) for i in range(201)]
    d.line(pts, fill=(200, 230, 200), width=7)
    gx, gy = pt(0)
    d.line((gx, gy - 7, gx, gy + 7), fill=(255, 80, 80), width=3)

    def where(run):                                 # スタートから run m 進んだ地点
        return pt(-(dist - run))
    return im, where


def _wrap(d, text, fnt, maxw):
    out, cur = [], ''
    for ch in text:
        if d.textlength(cur + ch, font=fnt) > maxw and ch not in '、。！？）':
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return out


def _badge(d, x, y, n, cloth, fnt, size=30):
    d.rounded_rectangle((x, y, x + size, y + size), radius=5, fill=cloth, outline=(60, 60, 60))
    t = str(n)
    d.text((x + size / 2 - d.textlength(t, font=fnt) / 2, y + size * 0.08), t, font=fnt, fill=_txt_col(cloth))


def _comment_plan(nums, name, pos_at, fav, winner_seq):
    """実況（u：進み具合 0〜1 → 文）"""
    nm = lambda n: name[n]
    o = lambda u: sorted(nums, key=lambda n: -pos_at(u)[n])
    plan = [(-1.0, '各馬ゲートイン完了！'), (0.0, 'スタートしました！')]
    a = o(0.14)
    plan.append((0.1, f"先頭は{nm(a[0])}\n{nm(a[1])}が続く"))
    a = o(0.24)
    if len(a) > 2:
        plan.append((0.22, f"{nm(a[2])}が3番手\n手応えよく運ぶ"))
    a = o(0.34)
    if len(a) > 4:
        plan.append((0.32, f"その後ろに\n{nm(a[3])}と{nm(a[4])}"))
    a = o(0.45)
    if fav in a:
        k = a.index(fav) + 1
        where = '好位' if k <= 3 else '中団' if k <= len(a) * 0.6 else '後方'
        plan.append((0.43, f"人気の{nm(fav)}は{where}\n{k}番手あたり"))
    a = o(0.55)
    plan.append((0.53, f"最後方は{nm(a[-1])}\nじっくり構える"))
    a = o(0.66)
    plan.append((0.63, f"3コーナーから4コーナーへ\n先頭は{nm(a[0])}"))
    a = o(0.76)
    plan.append((0.74, f"直線に入った！\n先頭は{nm(a[0])}！"))
    # 一番伸びている馬
    p1, p2 = pos_at(0.76), pos_at(0.92)
    gain = max(nums, key=lambda n: (p2[n] - p1[n]) - (p2[a[0]] - p1[a[0]]))
    if gain != a[0]:
        plan.append((0.84, f"{nm(gain)}、凄い脚で\n追い込む！！"))
    w = winner_seq[0]
    plan.append((0.93, f"{nm(w)}、\n先頭でゴールへ！！" if o(0.93)[0] == w else f"{nm(w)}が\nとらえるか！！"))
    plan.append((1.0, f"{nm(w)}、ゴール！！"))
    return plan


def make_video(race, arr, sim, out_mp4, out_jpg=None, seconds=34):
    import imageio_ffmpeg
    dist = int(race.get('dist') or 1600)
    rep = sim['rep']
    by_n = {h['n']: h for h in arr if h.get('n')}
    nums = [n for n in rep['early_order'] if n in by_n]
    N = len(nums)
    name = {n: (by_n[n].get('name') or str(n)) for n in nums}
    odds = {n: by_n[n].get('o') for n in nums}
    pop_order = sorted([n for n in nums if odds[n]], key=lambda n: odds[n])
    pop = {n: i + 1 for i, n in enumerate(pop_order)}
    fav = pop_order[0] if pop_order else nums[0]
    g_early, g_fin, _ = SV._plan(race, arr, sim)
    k = 1.0 / getattr(SV, 'VIS', 1.0)
    g_early = {n: v * k for n, v in g_early.items()}
    g_fin = {n: v * k for n, v in g_fin.items()}
    rng = np.random.default_rng(N * 31 + dist)
    wob = {n: (rng.uniform(0, 6.28), rng.uniform(0.6, 1.4)) for n in nums}
    ph0 = {n: rng.uniform(0, 1) for n in nums}

    def pos_at(u):
        front = min(u, 1.08) * dist
        t_form = SV._smooth(u / 0.12)
        t_fin = SV._smooth((u - 0.6) / 0.4)
        out = {}
        for n in nums:
            a, sp = wob[n]
            jit = 0.25 * math.sin(u * 40 * sp + a) * (1 - t_fin)
            out[n] = front - (g_early[n] * t_form * (1 - t_fin) + g_fin[n] * t_fin + jit) * LEN_M
        return out

    plan = _comment_plan(nums, name, pos_at, fav, rep['finish_order'])
    fnt_sp = SV.font('b', 19 * 4)
    sprites = {}
    for n in nums:
        cloth, silk, cap, coat = _colors(race, by_n[n])
        sprites[n] = [horse_sprite(i / 10, coat, silk, cap, cloth, n, fnt_sp) for i in range(10)]
    cloth_of = {n: _colors(race, by_n[n])[0] for n in nums}
    sky = _sky(VW, 150)
    stand = _stand_strip(N + dist)
    ground = _scene_ground(VW, race.get('surf'))
    tr = SV.track_of(race)
    mini, where = _mini_course(dist, tr)
    F = lambda s: SV.font('b', s)
    FR = lambda s: SV.font('r', s)
    import re as _re
    title = race.get('name', '')
    _g = _re.search(r'\s*\(?(G[1-3]|Jpn[1-3]|L|OP)\)?\s*$', title)
    if _g and not race.get('grade'):
        title = title[:_g.start()].strip()
        race = dict(race, grade=_g.group(1), name=title)
    sub = f"{race.get('venue', '')} {race.get('surf', '')}{dist}m {N}頭 ｜ 基準：{sim['runs']:,}回で最多の着順"

    # 動かない部分（見出し・枠・下の帯）
    base = Image.new('RGB', (VW, VH), BG)
    d = ImageDraw.Draw(base)
    tw = d.textlength(title, font=F(50))
    d.text((VW / 2 - tw / 2 - (34 if race.get('grade') or 'BC' in title else 0), 22), title, font=F(50), fill=(250, 250, 250))
    gtxt = race.get('grade') or ('G1' if 'BC' in title else '')
    if gtxt:
        gx = VW / 2 + tw / 2 - 20
        d.rounded_rectangle((gx, 30, gx + 70, 76), radius=23, fill=(214, 172, 80))
        d.text((gx + 35 - d.textlength(gtxt, font=F(26)) / 2, 36), gtxt, font=F(26), fill=(40, 30, 10))
    d.text((VW / 2 - d.textlength(sub, font=F(22)) / 2, 90), sub, font=F(22), fill=(200, 200, 205))
    d.rectangle((0, 196, VW, 312), fill=PANEL)
    d.rectangle((0, SCN_Y + SCN_H, VW, VH), fill=BG)
    d.line((0, 1112, VW, 1112), fill=(170, 130, 70), width=3)
    d.line((0, 1236, VW, 1236), fill=(170, 130, 70), width=3)
    brand = 'シェイクユアハート 隊列シミュレーション'
    d.text((VW / 2 - d.textlength(brand, font=F(22)) / 2, 1244), brand, font=F(22), fill=(150, 150, 156))

    import sim_video_3d as V3
    S3 = V3.Scene(race, nums, {n: _colors(race, by_n[n]) for n in nums}, VW, SCN_H, F, seed=N + dist)
    sky3 = _sky(VW, SCN_H)
    elev = V3.course_of(race).get('elev', '')
    note_ = V3.RACE_NOTE.get(race.get('id') or '', {}).get('note')
    if note_:
        plan.insert(1, (-0.5, note_))
    seconds = max(seconds, dist / 26.0)
    PRE = int(FPS * 3.2)                     # ゲートイン（コースの説明も）
    n_run = int(FPS * seconds)
    n_after = int(FPS * 2.0)
    n_res = FPS * 5
    n_rates = FPS * 8
    lane = {n: (i / max(1, N - 1)) * 5.0 for i, n in enumerate(sorted(nums))}
    writer = imageio_ffmpeg.write_frames(
        out_mp4, (VW, VH), fps=FPS, codec='libx264', pix_fmt_in='rgb24', pix_fmt_out='yuv420p',
        output_params=['-movflags', '+faststart', '-crf', '25', '-preset', 'veryfast'], macro_block_size=16)
    writer.send(None)
    cam = off_s = None
    finish_order = []
    pv = last = None
    total = PRE + n_run + n_after
    try:
        for f in range(total + n_res + n_rates):
            if f >= total:
                img = _end_panels(race, sim, by_n, cloth_of, name, pop, (f - total) / FPS, n_res / FPS)
                writer.send(img.tobytes())
                last = img
                continue
            fr_run = f - PRE
            t_ = min(fr_run / n_run, 1.0) if fr_run >= 0 else 0.0
            u = t_ * 0.9 / 0.82 if t_ < 0.82 else 0.9 + (t_ - 0.82) / 0.18 * 0.1     # 最後の100mほどはゆっくり
            u += max(0, fr_run - n_run) / n_run * 0.25
            pos = pos_at(u) if fr_run >= 0 else {n: 0.0 for n in nums}
            front = max(pos.values())
            order = sorted(nums, key=lambda n: -pos[n])
            occ = []
            t_fin = SV._smooth((u - 0.6) / 0.4)
            for i, n in enumerate(order):
                want = (0.0 if u > 0.08 else lane[n]) + (i % 3) * 0.9
                while any(abs(pos[m] - pos[n]) < LEN_M * 1.1 and abs(l_ - want) < 0.9 for m, l_ in occ):
                    want += 1.0
                if t_fin > 0 and i > 2:
                    want += 1.2 * t_fin * min(1, i / 4)
                if fr_run >= 0:
                    lane[n] += (min(want, 6.5) - lane[n]) * (0.5 if u < 0.02 else 0.06)
                occ.append((n, lane[n]))
            # カメラ：スタートは引き、道中は先頭集団、直線は寄る
            zoom = 0.78 if u < 0.1 else 0.95 if u < 0.7 else 1.08
            ppm = 25.0 * zoom
            spread_ = pos[order[0]] - pos[order[-1]]
            off = -min(spread_ * 0.5, (VW * 0.33) / ppm) + (VW * 0.14) / ppm
            off_s = off if off_s is None else off_s + (off - off_s) * 0.08
            cam = min(pos[order[0]] + off_s, dist - (VW * 0.12) / ppm)
            cam_left = cam - (VW * 0.5) / ppm

            img = base.copy()
            if S3 is not None:
                remain_ = dist - pos[order[0]]
                phase = ('gate' if fr_run < FPS * 0.8 else 'goal' if remain_ < 45 or fr_run > n_run
                         else 'stretch' if remain_ < 260 else 'run')
                hs_ = [(n, pos[n], 1.0 + lane[n] * 1.6, fr_run >= 0) for n in nums]
                scn = S3.render(hs_, pos[order[0]], pos[order[min(3, N - 1)]], phase, sky3, f,
                                label_n=order[0] if fr_run >= 0 else None, name=name[order[0]], F=F)
                sd = ImageDraw.Draw(scn)
            else:
                raise RuntimeError('3Dの映像が必要です')
            remain = max(0, dist - front)
            if fr_run < int(FPS * 1.2) and fr_run >= 0 or remain <= 0:
                lab = 'スタート' if remain > 0 else 'ゴール'
                lw = sd.textlength(lab, font=F(34))
                sd.rounded_rectangle((VW / 2 - lw / 2 - 40, 14, VW / 2 + lw / 2 + 40, 66), radius=12, fill=(24, 24, 28), outline=(200, 160, 80), width=2)
                sd.text((VW / 2 - lw / 2, 18), lab, font=F(34), fill=(255, 255, 255))
            if elev:                                     # 高低差の箱
                ov_ = Image.new('RGBA', (300, 92), (0, 0, 0, 0))
                od_ = ImageDraw.Draw(ov_)
                od_.rectangle((0, 0, 299, 91), fill=(20, 40, 24, 190))
                od_.text((10, 6), elev, font=F(20), fill=(240, 240, 240))
                od_.line((12, 66, 288, 66), fill=(150, 230, 150), width=3)
                px_ = 12 + 276 * min(1.0, max(0.0, front / dist))
                od_.ellipse((px_ - 7, 59, px_ + 7, 73), fill=(250, 210, 60))
                scn.paste(ov_, (0, SCN_H - 102), ov_)
            # コース図
            mm = mini.copy()
            md = ImageDraw.Draw(mm)
            for n in reversed(order):
                mx, my = where(max(0.0, min(dist, pos[n])))
                md.ellipse((mx - 7, my - 7, mx + 7, my + 7), fill=cloth_of[n], outline=(20, 20, 20))
            scn.paste(mm, (VW - mini.width - 10, SCN_H - mini.height - 10), mm)
            img.paste(scn, (0, SCN_Y))

            d = ImageDraw.Draw(img)
            # 残り距離のバー
            prog = min(1.0, max(0.0, front / dist))
            d.rounded_rectangle((0, 132, VW, 178), radius=0, fill=(30, 30, 34))
            if prog > 0:
                d.rounded_rectangle((0, 136, max(40, VW * prog), 174), radius=19, fill=GOLD_BAR)
            lab = f"残り {remain:,.0f}m"
            d.text((VW - 16 - d.textlength(lab, font=F(28)), 138), lab, font=F(28), fill=(255, 255, 255), stroke_width=3, stroke_fill=(20, 20, 20))
            # 位置取りの帯（先頭が右）
            span = max(30.0, pos[order[0]] - pos[order[-1]])
            for i, n in enumerate(reversed(order)):
                x = 40 + (pos[n] - pos[order[-1]]) / span * 420 if fr_run >= 0 else 40 + (n - 1) / max(1, N - 1) * 420
                y = 232 + (i % 3) * 24
                d.ellipse((x - 14, y - 14, x + 14, y + 14), fill=cloth_of[n], outline=(240, 240, 240) if n == fav else (30, 30, 30), width=2)
                t = str(n)
                d.text((x - d.textlength(t, font=F(16)) / 2, y - 11), t, font=F(16), fill=_txt_col(cloth_of[n]))
            for i, n in enumerate(order[:3]):          # 上位3頭と単勝オッズ（日本）
                y = 206 + i * 34
                d.text((520, y), str(i + 1), font=F(22), fill=(200, 200, 200))
                _badge(d, 548, y, n, cloth_of[n], F(18), 28)
                o_ = f"{odds[n]:.1f}" if odds[n] else '-'
                d.text((590, y - 2), o_, font=F(28), fill=(255, 255, 255))
            # 今の順位（2列）
            show = finish_order + [n for n in order if n not in finish_order] if remain <= 0 else order
            if remain <= 0:
                for n in order:
                    if pos[n] >= dist and n not in finish_order:
                        finish_order.append(n)
            rows = (N + 1) // 2
            rh = min(48, 276 // max(1, rows))
            for i, n in enumerate(show):
                col, row = divmod(i, rows)
                x0, y0 = 10 + col * 360, SCN_Y + SCN_H + 14 + row * rh
                d.text((x0, y0 + 2), str(i + 1), font=F(20), fill=(220, 220, 220))
                _badge(d, x0 + 30, y0, n, cloth_of[n], F(18), 28)
                d.text((x0 + 68, y0), name[n][:10], font=F(22), fill=(245, 245, 245))
                if pop.get(n):
                    pt_ = f"{pop[n]}人気"
                    d.text((x0 + 340 - d.textlength(pt_, font=F(18)), y0 + 4), pt_, font=F(18), fill=(200, 200, 200))
            # 実況
            cur = [t for uu, t in plan if (uu <= u if fr_run >= 0 else uu <= -1.0 + 1.0 * (f / max(1, PRE)) * 0.99)]
            txt = cur[-1] if cur else ''
            for fs_ in (42, 34, 28, 24):
                lines = []
                for part in txt.split('\n'):
                    lines += _wrap(d, part, F(fs_), VW - 30)
                if len(lines) * fs_ * 1.28 <= 116:
                    break
            lh_ = int(fs_ * 1.28)
            yy = 1174 - len(lines) * lh_ / 2
            for ln in lines:
                d.text((VW / 2 - d.textlength(ln, font=F(fs_)) / 2, yy), ln, font=F(fs_), fill=(250, 250, 250))
                yy += lh_
            if pv is None and u >= 0.8:
                pv = img.copy()
            last = img
            writer.send(img.tobytes())
    finally:
        writer.close()
    if out_jpg:
        p_ = (pv or last).copy()
        p_.thumbnail((720, 720))
        p_.save(out_jpg, quality=85)
    return out_mp4


def _end_panels(race, sim, by_n, cloth_of, name, pop, t, t_res):
    """ゴールのあと：①着順（一番多かった3連単）②全頭の1着率・2着率・3着率"""
    img = Image.new('RGB', (VW, VH), BG)
    d = ImageDraw.Draw(img)
    F = lambda s: SV.font('b', s)
    title = race.get('name', '')
    d.text((VW / 2 - d.textlength(title, font=F(46)) / 2, 30), title, font=F(46), fill=(250, 250, 250))
    rep = sim['rep']
    if t < t_res:
        lab = '一番多かった着順'
        d.text((VW / 2 - d.textlength(lab, font=F(36)) / 2, 130), lab, font=F(36), fill=SV.GOLD)
        if sim.get('trifecta'):
            c = sim['trifecta'][0]
            s_ = f"{sim['runs']:,}回中 {c['count']}回（3連単 {'→'.join(map(str, c['combo']))}）"
            d.text((VW / 2 - d.textlength(s_, font=F(24)) / 2, 186), s_, font=F(24), fill=(200, 200, 205))
        y = 260
        for i, n in enumerate(rep['finish_order'][:8]):
            if n not in name:
                continue
            st = sim['horses'].get(str(n), {})
            d.rounded_rectangle((30, y, VW - 30, y + 92), radius=14, fill=(34, 30, 22) if i < 3 else PANEL)
            d.text((50, y + 22), f"{i + 1}着", font=F(36), fill=SV.GOLD if i < 3 else (220, 220, 220))
            _badge(d, 150, y + 24, n, cloth_of[n], F(26), 44)
            d.text((210, y + 12), name[n][:11], font=F(32), fill=(250, 250, 250))
            mg = '' if i == 0 else f"+{rep['margins'][i]:.1f}馬身"
            d.text((210, y + 54), f"{mg}  {pop.get(n, '-')}人気", font=F(20), fill=(190, 190, 195))
            r_ = f"勝率{st.get('win', 0) * 100:.0f}%"
            d.text((VW - 50 - d.textlength(r_, font=F(26)), y + 30), r_, font=F(26), fill=SV.GOLD)
            y += 104
        return img
    lab = f"{sim['runs']:,}回走らせた結果"
    d.text((VW / 2 - d.textlength(lab, font=F(34)) / 2, 120), lab, font=F(34), fill=SV.GOLD)
    rows = sorted(sim['horses'].items(), key=lambda kv: -(kv[1].get('p1', 0) + kv[1].get('p2', 0) * 0.5 + kv[1].get('p3', 0) * 0.25))
    rows = [(int(k), v) for k, v in rows if int(k) in name]
    xs = [20, 70, 330, 430, 530, 630]
    y = 190
    for x, l_ in zip(xs, ['', '馬名', '1着率', '2着率', '3着率', '3着内']):
        d.text((x, y), l_, font=F(22), fill=(170, 170, 176))
    y += 40
    rh = min(66, (VH - y - 120) // max(1, len(rows)))
    for i, (n, st) in enumerate(rows):
        if i % 2 == 0:
            d.rectangle((10, y - 4, VW - 10, y + rh - 8), fill=PANEL)
        _badge(d, xs[0], y + 2, n, cloth_of[n], F(20), 34)
        d.text((xs[1], y + 2), name[n][:9], font=F(26), fill=(245, 245, 245))
        for x, v in zip(xs[2:], [st.get('p1', 0), st.get('p2', 0), st.get('p3', 0), st.get('top3', 0)]):
            d.rectangle((x, y + 30, x + int(86 * min(1, v / 0.6)), y + 36), fill=(200, 160, 70))
            d.text((x, y), f"{v * 100:.1f}", font=F(26), fill=SV.GOLD if x == xs[2] else (230, 230, 230))
        y += rh
    if sim.get('trifecta'):
        d.text((20, VH - 100), '多かった3連単', font=F(22), fill=(170, 170, 176))
        tops = '　'.join(f"{'→'.join(map(str, c['combo']))}（{c['count']}）" for c in sim['trifecta'][:3])
        d.text((20, VH - 66), tops, font=F(24), fill=(240, 240, 240))
    return img
