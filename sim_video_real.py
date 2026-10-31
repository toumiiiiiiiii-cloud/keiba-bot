# -*- coding: utf-8 -*-
"""
中継風のレース動画（横から馬群を追いかけるカメラ）
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
# 背景（遠景・スタンド・内ラチ・コース・外ラチ）
# ─────────────────────────────────────────
Y_INNER, Y_OUTER = 300, 520        # 奥の内ラチ・手前の外ラチの高さ（画面のy）


def _sky():
    im = Image.new('RGB', (W, H))
    top, bot = np.array((96, 150, 214)), np.array((198, 222, 240))
    a = np.linspace(0, 1, 220)[:, None]
    im.paste(Image.fromarray(np.repeat((top * (1 - a) + bot * a)[:, None, :], W, axis=1).astype(np.uint8)), (0, 0))
    return im


def _stand_strip(seed):
    """スタンド（横に長い1枚。カメラに合わせてゆっくり流す）"""
    rng = np.random.default_rng(seed)
    sw = 3000
    im = Image.new('RGBA', (sw, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 40, sw, 140), fill=(120, 124, 134))
    for x in range(0, sw, 60):
        d.rectangle((x, 20, x + 4, 60), fill=(90, 92, 100))
    d.polygon([(0, 40), (sw, 40), (sw, 22), (0, 30)], fill=(70, 74, 86))
    px = im.load()
    for _ in range(26000):                     # 観客
        x, y = int(rng.integers(0, sw)), int(rng.integers(62, 132))
        c = tuple(int(v) for v in rng.integers(40, 235, 3)) + (255,)
        px[x, y] = c
        if x + 1 < sw:
            px[x + 1, y] = c
    return im.filter(ImageFilter.GaussianBlur(0.7))


def _ground(surf):
    im = Image.new('RGB', (W, H))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 210, W, Y_INNER - 6), fill=(70, 128, 62))                  # 内馬場の芝
    if surf == 'ダ':
        top, bot = (166, 124, 86), (138, 98, 64)
    else:
        top, bot = (74, 140, 64), (52, 116, 48)
    for y in range(Y_INNER - 6, H):
        t = (y - Y_INNER) / (H - Y_INNER)
        d.line((0, y, W, y), fill=tuple(int(a * (1 - t) + b * t) for a, b in zip(top, bot)))
    return im


def _rail(d, cam_x, y, ppm, h, col=(250, 250, 250)):
    d.rectangle((0, y - h, W, y - h + max(2, h // 6)), fill=col)
    step = 12.0                                  # 12mごとの支柱（速く流れてもちらつかないように）
    x0 = -((cam_x * ppm) % (step * ppm))
    x = x0
    while x < W:
        d.line((x, y - h, x, y), fill=(225, 225, 225), width=max(1, h // 8))
        x += step * ppm


# ─────────────────────────────────────────
# 本体
# ─────────────────────────────────────────
def make_video(race, arr, sim, out_mp4, out_jpg=None, seconds=18):
    import imageio_ffmpeg
    dist = int(race.get('dist') or 1600)
    rep = sim['rep']
    by_n = {h['n']: h for h in arr if h.get('n')}
    nums = [n for n in rep['early_order'] if n in by_n]
    N = len(nums)
    g_early, g_fin, _ = SV._plan(race, arr, sim)
    # 馬身単位に（_plan は見た目用の倍率つき）
    k = 1.0 / getattr(SV, 'VIS', 1.0)
    g_early = {n: v * k for n, v in g_early.items()}
    g_fin = {n: v * k for n, v in g_fin.items()}
    rng = np.random.default_rng(N * 31 + dist)
    wob = {n: (rng.uniform(0, 6.28), rng.uniform(0.6, 1.4)) for n in nums}
    ph0 = {n: rng.uniform(0, 1) for n in nums}

    fnt = SV.font('b', 19 * 4)
    sprites = {}
    for n in nums:
        cloth, silk, cap, coat = _colors(race, by_n[n])
        sprites[n] = [horse_sprite(i / 10, coat, silk, cap, cloth, n, fnt) for i in range(10)]
    sky, ground = _sky(), _ground(race.get('surf'))
    stand = _stand_strip(N + dist)
    f_b = {s: SV.font('b', s) for s in (13, 15, 18, 22, 26, 30)}
    title = f"{race.get('venue', '')}{race.get('R', '')}R  {race.get('name', '')}"
    sub = f"{race.get('surf', '')}{dist}m  {race.get('going', '')}  {N}頭"

    n_run = int(FPS * seconds)
    n_after = int(FPS * 1.6)
    n_hold = FPS * 4
    n_rates = FPS * 7 if 'p1' in next(iter(sim['horses'].values())) else 0
    lane = {n: (i / max(1, N - 1)) * 5.0 for i, n in enumerate(sorted(nums))}   # 0＝内ラチ沿い 5＝外
    writer = imageio_ffmpeg.write_frames(
        out_mp4, (W, H), fps=FPS, codec='libx264', pix_fmt_in='rgb24', pix_fmt_out='yuv420p',
        output_params=['-movflags', '+faststart', '-crf', '24', '-preset', 'veryfast'], macro_block_size=16)
    writer.send(None)
    last = None
    cam = None
    finish_order = []
    try:
        for f in range(n_run + n_after + n_hold + n_rates):
            if f >= n_run + n_after + n_hold:
                img = SV._rates_panel(sim, {n: dict(by_n[n], w=by_n[n].get('w')) for n in by_n}, race,
                                      (f - n_run - n_after - n_hold) / FPS)
                writer.send(img.tobytes())
                last = img
                continue
            if f >= n_run + n_after:
                img = last_race.copy()
                SV._result_panel(img, sim, by_n, (f - n_run - n_after) / FPS)
                writer.send(img.tobytes())
                last = img
                continue
            u = min(f / n_run, 1.0) + max(0, f - n_run) / n_run * 0.6
            front = min(u, 1.06) * dist
            t_form = SV._smooth(u / 0.12)
            t_fin = SV._smooth((u - 0.6) / 0.4)
            pos = {}
            for n in nums:
                a, sp = wob[n]
                jit = 0.25 * math.sin(u * 40 * sp + a) * (1 - t_fin)
                gap = (g_early[n] * t_form * (1 - t_fin) + g_fin[n] * t_fin + jit) * LEN_M
                pos[n] = front - gap
            order = sorted(nums, key=lambda n: -pos[n])
            # レーン：序盤はゲートの並び → 道中は内へ（前が詰まれば外へ）→ 直線は差し馬が外へ
            occ = []
            for i, n in enumerate(order):
                want = (0.0 if u > 0.08 else lane[n]) + (i % 3) * 0.9
                while any(abs(pos[m] - pos[n]) < LEN_M * 1.1 and abs(l_ - want) < 0.9 for m, l_ in occ):
                    want += 1.0
                if t_fin > 0 and i > 2:
                    want += 1.2 * t_fin * min(1, i / 4)
                lane[n] += (min(want, 6.5) - lane[n]) * (0.5 if u < 0.02 else 0.06)
                occ.append((n, lane[n]))
            # カメラ：先頭から3〜4番手あたり。画面の右寄りに先頭が来るように
            # 先頭がいつも画面の右寄り（7割の位置）に来るように。動きはなめらかに
            spread_ = pos[order[0]] - pos[order[-1]]
            off = -min(spread_ * 0.5, (W * 0.35) / PPM) + (W * 0.12) / PPM
            off_s = off if cam is None else off_s + (off - off_s) * 0.1
            cam = min(pos[order[0]] + off_s, dist - (W * 0.15) / PPM)   # ゴール板は画面に残す
            cam_left = cam - (W * 0.5) / PPM

            img = sky.copy()
            sx = int((cam_left * 3.2) % (stand.width - W))
            img.paste(stand.crop((sx, 0, sx + W, 140)), (0, 108), stand.crop((sx, 0, sx + W, 140)))
            img.paste(ground.crop((0, 210, W, H)), (0, 210))
            d = ImageDraw.Draw(img)
            _rail(d, cam_left * 0.75, Y_INNER, PPM * 0.75, 16)
            # ハロン棒・ゴール板（内ラチ側）
            for m in range(0, dist + 1, 200):
                x = (m - cam_left) * PPM * 0.75 + W * 0.5 * (1 - 0.75)
                if -40 < x < W + 40:
                    if m == dist:
                        d.rectangle((x - 3, Y_INNER - 120, x + 3, Y_INNER), fill=(250, 250, 250))
                        d.ellipse((x - 26, Y_INNER - 150, x + 26, Y_INNER - 98), fill=(250, 250, 250), outline=(200, 30, 30), width=5)
                        xn = (m - cam_left) * PPM * 1.25 + W * 0.5 * (1 - 1.25)     # 手前のレーンでのゴールの位置
                        d.line((x, Y_INNER, xn, Y_OUTER + 10), fill=(255, 255, 255), width=3)
                    else:
                        d.rectangle((x - 2, Y_INNER - 46, x + 2, Y_INNER), fill=(220, 40, 40) if (dist - m) % 400 == 0 else (250, 250, 250))
            # 馬（奥＝内から描く）。奥ほど小さく・上に
            for n in sorted(nums, key=lambda n: lane[n]):
                z = min(1.0, lane[n] / 6.5)
                sc = 0.75 + 0.5 * z
                yg = Y_INNER + 40 + (Y_OUTER - Y_INNER - 30) * z
                x = (pos[n] - cam_left) * PPM * sc + W * 0.5 * (1 - sc)
                if x < -120 or x > W + 120:
                    continue
                sp_rate = 2.3 if u < 1.0 else 1.6
                fr = int(((f / FPS) * sp_rate + ph0[n]) * 10) % 10
                spr = sprites[n][fr]
                sw, sh = int(140 * sc * 1.05), int(120 * sc * 1.05)
                s2 = spr.resize((sw, sh), Image.BILINEAR)
                sh_ = Image.new('RGBA', (sw, 10), (0, 0, 0, 0))
                ImageDraw.Draw(sh_).ellipse((sw * 0.15, 0, sw * 0.85, 9), fill=(0, 0, 0, 70))
                img.paste(sh_, (int(x - sw * 0.43), int(yg - 6)), sh_)
                img.paste(s2, (int(x - sw * 0.43), int(yg - sh + 4)), s2)
            # 手前の外ラチ
            _rail(d, cam_left, Y_OUTER + 40, PPM * 1.1, 34)
            # 文字（中継風）
            ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.rounded_rectangle((14, 12, 14 + max(330, od.textlength(title, font=f_b[22]) + 30), 74), radius=8, fill=(10, 16, 40, 200))
            od.rounded_rectangle((W - 214, 12, W - 14, 60), radius=8, fill=(10, 16, 40, 200))
            od.rounded_rectangle((0, H - 50, W, H), radius=0, fill=(8, 12, 30, 215))
            img.paste(Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB'))
            d = ImageDraw.Draw(img)
            d.text((28, 16), title, font=f_b[22], fill=SV.GOLD)
            d.text((28, 46), sub, font=f_b[15], fill=(225, 230, 240))
            remain = max(0, dist - front)
            lab = 'ゴール' if remain <= 0 else f"残り {remain:,.0f}m"
            d.text((W - 114 - d.textlength(lab, font=f_b[26]) / 2, 16), lab, font=f_b[26], fill=(255, 255, 255))
            if remain <= 0:
                for n in order:
                    if pos[n] >= dist and n not in finish_order:
                        finish_order.append(n)
            show = finish_order + [n for n in order if n not in finish_order]
            d.text((14, H - 40), '着順' if remain <= 0 else '隊列', font=f_b[18], fill=SV.MUTED)
            for i, n in enumerate(show):
                cx_ = 74 + i * min(60, (W - 110) / N)
                cloth = _colors(race, by_n[n])[0]
                d.rounded_rectangle((cx_ - 17, H - 42, cx_ + 17, H - 10), radius=6, fill=cloth, outline=(20, 20, 20))
                t = str(n)
                d.text((cx_ - d.textlength(t, font=f_b[18]) / 2, H - 40), t, font=f_b[18], fill=_txt_col(cloth))
            if f == n_run + n_after - 1 or f == n_run:
                last_race = img.copy()
            last = img
            writer.send(img.tobytes())
    finally:
        writer.close()
    if out_jpg:
        pv = (last_race if 'last_race' in dir() else last).copy()
        pv.thumbnail((640, 640))
        pv.save(out_jpg, quality=85)
    return out_mp4
