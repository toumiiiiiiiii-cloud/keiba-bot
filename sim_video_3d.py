# -*- coding: utf-8 -*-
"""
立体（遠近法）のレース映像。sim_video_real の縦長レイアウトの「映像」の部分をこれで描く
  ・コースはスタジアム形（直線2本＋半円2つ）を地面に置き、カメラで写す
  ・カメラ：ゲートは正面寄り → 道中は斜め後ろから追いかける → コーナーは高めの後ろ → 直線は横 → ゴール前
  ・馬は丸っこいキャラ（横・後ろ・前の3方向の絵）を、カメラから見た向きで使い分ける
"""
import math

import numpy as np
from PIL import Image, ImageDraw

# ── コース情報（BC2026・キーンランド。調べた数字） ──
# 本馬場（ダート）1周 1 1/16マイル≒1710m、最後の直線 1,236フィート≒377m
# 芝コース 1周 7.5ハロン≒1509m（ダートの内側）
COURSE = {
    ('キーンランド', 'ダ'): {'L': 1710.0, 'S': 400.0, 'stretch': 377.0, 'elev': '高低差ほぼなし　平坦'},
    ('キーンランド', '芝'): {'L': 1509.0, 'S': 380.0, 'stretch': 340.0, 'elev': '高低差ほぼなし　平坦'},
    # JRA（JRAの公表値：1周・直線・高低差）
    ('東京', '芝'): {'L': 2083.1, 'S': 530.0, 'stretch': 525.9, 'elev': '高低差2.7m　直線に坂'},
    ('東京', 'ダ'): {'L': 1899.0, 'S': 505.0, 'stretch': 501.6, 'elev': '高低差2.5m　直線に坂'},
    ('京都', '芝外'): {'L': 1894.3, 'S': 410.0, 'stretch': 403.7, 'elev': '高低差4.3m　3コーナーに坂'},
    ('京都', '芝'): {'L': 1782.8, 'S': 335.0, 'stretch': 328.4, 'elev': '高低差3.1m　3コーナーに坂'},
    ('京都', 'ダ'): {'L': 1607.6, 'S': 335.0, 'stretch': 329.1, 'elev': '高低差3.0m'},
}
# レースごとのスタート地点の説明（race id → 文）。finish_back：本来のゴール板から手前に何mでゴールするか
RACE_NOTE = {
    '2026F5010106': {'note': 'スタンド前からスタート。最初のコーナーまで約140mと短く、2回カーブ。ゴールは手前の16分の1ポール（直線が短い）',
                     'finish_back': 100.0},
    '2026F5010108': {'note': '向こう正面のポケットからスタート。カーブは3〜4コーナーの1回だけ、最後の直線は約377m'},
    '2026F5010109': {'note': 'ゴール板の少し手前からスタート。最初のコーナーまでが短く外枠は不利。2回カーブ'},
    '2026F5010111': {'note': '向こう正面からスタートしてコースを約1周半。カーブは3回。ヨーロッパ勢が強い長丁場'},
    '2026F5010112': {'note': '直線の途中からスタートして1周。最初のコーナーまで約300m。2回カーブ'},
}


def course_of(race):
    outer = '外' in (race.get('courseNote') or '') and race.get('surf') == '芝'
    c = COURSE.get((race.get('venue'), '芝外' if outer else race.get('surf'))) or COURSE.get((race.get('venue'), race.get('surf')))
    if not c:
        L = 1700.0 if race.get('surf') == 'ダ' else 1600.0
        c = {'L': L, 'S': 380.0, 'stretch': 360.0, 'elev': ''}
    return dict(c)


class Track:
    """コースの形。p＝ゴール板から進行方向へ何m（左回り）。lane＝内ラチからの距離（m）"""

    def __init__(self, c, right=False, finish_back=0.0):
        self.L, self.S = c['L'], c['S']
        self.r = (self.L - 2 * self.S) / (2 * math.pi)
        self.W = 26.0                                   # コースの幅（m）
        self.xf = -self.S / 2 + min(c['stretch'], self.S - 10) - finish_back   # ゴール板（下の直線）
        self.right = right

    def center(self, p, lane=0.0):
        """戻り値：(x, y, 進む向きの角度, カーブ中か)"""
        S, r, L = self.S, self.r, self.L
        q = (p + (self.xf + S / 2)) % L                 # 下の直線の左端（4コーナー出口）から
        R = r + lane
        if q < S:                                       # ホームの直線（左→右）
            x, y, h, turn = -S / 2 + q, -R, 0.0, False
        elif q < S + math.pi * r:                       # 1〜2コーナー
            a = (q - S) / r
            x, y, h, turn = S / 2 + R * math.sin(a), -R * math.cos(a), a, True
        elif q < 2 * S + math.pi * r:                   # 向こう正面（右→左）
            x, y, h, turn = S / 2 - (q - S - math.pi * r), R, math.pi, False
        else:                                           # 3〜4コーナー
            a = (q - 2 * S - math.pi * r) / r
            x, y, h, turn = -S / 2 - R * math.sin(a), R * math.cos(a), math.pi + a, True
        if self.right:
            x, h = -x, math.pi - h
        return x, y, h, turn


class Cam:
    def __init__(self, w, h, fov=58):
        self.w, self.h = w, h
        self.fpx = (w / 2) / math.tan(math.radians(fov) / 2)

    def set(self, pos, target):
        self.p = np.array(pos, dtype=float)
        f = np.array(target, dtype=float) - self.p
        f /= np.linalg.norm(f)
        up0 = np.array([0, 0, 1.0])
        rt = np.cross(f, up0)
        rt /= np.linalg.norm(rt)
        up = np.cross(rt, f)
        self.f, self.rt, self.up = f, rt, up

    def to_cam(self, P):
        v = np.asarray(P, dtype=float) - self.p
        return v @ self.rt, v @ self.up, v @ self.f

    def proj(self, P):
        x, y, z = self.to_cam(P)
        z = np.maximum(z, 1e-3)
        return self.w / 2 + self.fpx * x / z, self.h / 2 - self.fpx * y / z, z

    def horizon(self):
        # 地面の遠く（水平方向）の高さ
        fh = np.array([self.f[0], self.f[1], 0.0])
        fh /= np.linalg.norm(fh)
        x, y, z = self.to_cam(self.p + fh * 1e5)
        return self.h / 2 - self.fpx * y / z


def _clip_poly(cam, pts3, near=0.8):
    """カメラの手前で切る（Sutherland-Hodgman）→ 画面の座標"""
    c = [cam.to_cam(p) for p in pts3]
    out = []
    n = len(c)
    for i in range(n):
        a, b = c[i], c[(i + 1) % n]
        ina, inb = a[2] > near, b[2] > near
        if ina:
            out.append(a)
        if ina != inb:
            t = (near - a[2]) / (b[2] - a[2])
            out.append(tuple(a[k] + (b[k] - a[k]) * t for k in range(3)))
    return [(cam.w / 2 + cam.fpx * x / z, cam.h / 2 - cam.fpx * y / z) for x, y, z in out]


# ─────────────────────────────────────────
# 丸っこい馬（横・後ろ・前）。4倍で描いて縮小
# ─────────────────────────────────────────
def _leg(d, x0, y0, a, l, col, w):
    x1, y1 = x0 + l * math.sin(a), y0 + l * math.cos(a)
    d.line((x0, y0, x1, y1), fill=col, width=w)
    d.ellipse((x1 - w * 0.6, y1 - w * 0.4, x1 + w * 0.6, y1 + w * 0.5), fill=(40, 34, 30))


def chibi_side(phase, coat, silk, cap, cloth, num, fnt, S=4):
    w_, h_ = 150 * S, 140 * S
    im = Image.new('RGBA', (w_, h_), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    P = lambda x, y: (x * S + 70 * S, y * S + 132 * S)
    ph = phase * 2 * math.pi
    bob = 3 * math.sin(2 * ph)
    dark = tuple(int(c * 0.7) for c in coat)
    ol = (50, 34, 24)
    lw = int(10 * S)
    _leg(d, *P(-26, -40 + bob), 0.6 * math.sin(ph + 0.6), 30 * S, dark, lw)
    _leg(d, *P(24, -40 + bob), 0.7 * math.sin(ph + 2.4), 30 * S, dark, lw)
    sw = 5 * math.sin(ph)
    d.polygon([P(-46, -62 + bob), P(-66, -54 + bob + sw), P(-70, -34 + bob + sw), P(-50, -48 + bob)], fill=dark, outline=ol)
    d.ellipse([*P(-50, -76 + bob), *P(40, -34 + bob)], fill=coat, outline=ol, width=int(2 * S))     # 胴（丸い）
    # 頭（大きめ）
    hx, hy = 44, -86 + bob + 2 * math.sin(ph + 1)
    d.polygon([P(18, -66 + bob), P(hx - 4, hy + 4), P(hx + 10, hy + 10), P(36, -46 + bob)], fill=coat)
    d.ellipse([*P(hx - 14, hy - 18), *P(hx + 22, hy + 16)], fill=coat, outline=ol, width=int(2 * S))
    d.ellipse([*P(hx + 12, hy - 2), *P(hx + 34, hy + 18)], fill=coat, outline=ol, width=int(2 * S))  # 鼻先
    d.polygon([P(hx - 8, hy - 14), P(hx - 4, hy - 30), P(hx + 4, hy - 16)], fill=dark, outline=ol)   # 耳
    d.ellipse([*P(hx + 2, hy - 6), *P(hx + 10, hy + 2)], fill=(20, 20, 20))                          # 目
    d.line([P(hx - 2, hy + 4), P(hx + 26, hy + 8)], fill=(200, 60, 60), width=int(2 * S))             # 手綱
    # ゼッケン
    d.rounded_rectangle([*P(-18, -66 + bob), *P(14, -44 + bob)], radius=3 * S, fill=cloth, outline=ol)
    t = str(num)
    cx, cy = P(-2, -55 + bob)
    d.text((cx - d.textlength(t, font=fnt) / 2, cy - fnt.size * 0.62), t, font=fnt, fill=(20, 20, 20) if sum(cloth) > 420 else (255, 255, 255))
    _leg(d, *P(-22, -40 + bob), 0.6 * math.sin(ph), 31 * S, coat, lw)
    _leg(d, *P(28, -40 + bob), 0.7 * math.sin(ph + 1.8), 31 * S, coat, lw)
    # 騎手（丸い頭・帽子）
    jb = bob * 0.4
    d.ellipse([*P(-6, -92 + jb), *P(22, -66 + jb)], fill=silk, outline=ol, width=int(2 * S))
    d.line([P(16, -80 + jb), P(32, -74 + jb), P(hx - 2, hy + 6)], fill=silk, width=int(5 * S))
    d.ellipse([*P(8, -112 + jb), *P(32, -88 + jb)], fill=(250, 226, 200), outline=ol, width=int(2 * S))
    d.chord([*P(8, -114 + jb), *P(32, -92 + jb)], 180, 360, fill=cap, outline=ol)
    d.line([P(0, -70 + jb), P(-4, -58 + jb), P(6, -54 + jb)], fill=(245, 245, 240), width=int(6 * S))
    return im.resize((150, 140), Image.LANCZOS)


def chibi_back(phase, coat, silk, cap, cloth, num, fnt, front=False, S=4):
    w_, h_ = 100 * S, 140 * S
    im = Image.new('RGBA', (w_, h_), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    P = lambda x, y: (x * S + 50 * S, y * S + 132 * S)
    ph = phase * 2 * math.pi
    bob = 3 * math.sin(2 * ph)
    dark = tuple(int(c * 0.7) for c in coat)
    ol = (50, 34, 24)
    lw = int(10 * S)
    for k, (x, a) in enumerate(((-14, ph), (14, ph + math.pi))):
        lift = 10 * max(0, math.sin(a))
        d.line([P(x, -40 + bob), P(x, -10 - lift)], fill=coat if k else dark, width=lw)
        d.ellipse([*P(x - 6, -12 - lift), *P(x + 6, -4 - lift)], fill=(40, 34, 30))
    d.ellipse([*P(-32, -78 + bob), *P(32, -30 + bob)], fill=coat, outline=ol, width=int(2 * S))
    if front:
        d.ellipse([*P(-18, -96 + bob), *P(18, -54 + bob)], fill=coat, outline=ol, width=int(2 * S))   # 顔
        d.polygon([P(-4, -92 + bob), P(4, -92 + bob), P(2, -62 + bob), P(-2, -62 + bob)], fill=(245, 240, 230))
        d.ellipse([*P(-12, -84 + bob), *P(-6, -78 + bob)], fill=(20, 20, 20))
        d.ellipse([*P(6, -84 + bob), *P(12, -78 + bob)], fill=(20, 20, 20))
        d.rounded_rectangle([*P(-12, -50 + bob), *P(12, -36 + bob)], radius=3 * S, fill=cloth, outline=ol)
    else:
        sw = 4 * math.sin(ph)
        d.polygon([P(-5, -64 + bob), P(5, -64 + bob), P(8 + sw, -30 + bob), P(-8 + sw, -30 + bob)], fill=dark, outline=ol)  # 尾
        for sx in (-1, 1):
            d.rounded_rectangle([*P(sx * 34 - 7, -66 + bob), *P(sx * 34 + 7, -46 + bob)], radius=2 * S, fill=cloth, outline=ol)
    jb = bob * 0.4
    d.ellipse([*P(-16, -100 + jb), *P(16, -70 + jb)], fill=silk, outline=ol, width=int(2 * S))
    d.ellipse([*P(-12, -120 + jb), *P(12, -96 + jb)], fill=cap if not front else (250, 226, 200), outline=ol, width=int(2 * S))
    if front:
        d.chord([*P(-12, -122 + jb), *P(12, -100 + jb)], 180, 360, fill=cap, outline=ol)
    t = str(num)
    cx, cy = P(0, -86 + jb)
    if not front:
        d.text((cx - d.textlength(t, font=fnt) / 2, cy - fnt.size * 0.62), t, font=fnt, fill=(20, 20, 20) if sum(silk) > 420 else (255, 255, 255))
    return im.resize((100, 140), Image.LANCZOS)


# ─────────────────────────────────────────
# 場面を描く
# ─────────────────────────────────────────
class Scene:
    def __init__(self, race, nums, colors, w, h, font_fn, seed=1):
        self.race = race
        self.w, self.h = w, h
        c = course_of(race)
        note = RACE_NOTE.get(race.get('id') or '', {})
        try:
            import sim_video as _SV
            right = bool(_SV.track_of(race).get('right'))
        except Exception:
            right = False
        self.trk = Track(c, right=right, finish_back=note.get('finish_back', 0.0))
        self.sgn = -1.0 if right else 1.0
        self.dist = float(race.get('dist') or 1600)
        self.cam = Cam(w, h)
        self.surf = race.get('surf')
        fnt = font_fn(22 * 4)
        self.spr = {}
        for n in nums:
            cloth, silk, cap, coat = colors[n]
            self.spr[n] = {'side': [chibi_side(i / 8, coat, silk, cap, cloth, n, fnt) for i in range(8)],
                           'back': [chibi_back(i / 8, coat, silk, cap, cloth, n, font_fn(18 * 4)) for i in range(8)],
                           'front': [chibi_back(i / 8, coat, silk, cap, cloth, n, font_fn(18 * 4), front=True) for i in range(8)]}
        rng = np.random.default_rng(seed)
        T = self.trk
        # 木（コースの外と内）
        self.trees = []
        for _ in range(140):
            p = rng.uniform(0, T.L)
            lane = rng.choice([rng.uniform(-T.r * 0.85, -30), rng.uniform(T.W + 30, T.W + 160)])
            x, y, _, _ = T.center(p, lane)
            self.trees.append((x, y, rng.uniform(5, 9), rng.random() < 0.3))
        self.cam_state = None

    def pos3(self, run, lane):
        """スタートから run m 進んだ地点・内ラチから lane m（地面 z=0）"""
        x, y, h, turn = self.trk.center(run - self.dist, lane)
        return np.array([x, y, 0.0]), h, turn

    def _cam_target(self, lead_run, pack_run, phase):
        """phase：'gate' / 'run' / 'stretch' / 'goal'"""
        T = self.trk
        mid = (lead_run * 0.6 + pack_run * 0.4)
        pt, h, turn = self.pos3(mid, T.W * 0.3)
        fwd = np.array([math.cos(h), math.sin(h), 0.0])
        out = self.sgn * np.array([math.sin(h), -math.cos(h), 0.0])   # 外向き（左回りは進行方向の右、右回りは左）
        if phase == 'gate':                                     # ゲート：内側の斜め前から
            pos = pt + fwd * 16 - out * 14 + [0, 0, 4.5]
            tgt = pt - fwd * 2 + out * 4 + [0, 0, 1.0]
        elif phase == 'goal':                                   # ゴール板の内側から（スタンドが奥に見える）
            g, gh, _ = self.pos3(self.dist, T.W * 0.4)
            gf = np.array([math.cos(gh), math.sin(gh), 0.0])
            go = self.sgn * np.array([math.sin(gh), -math.cos(gh), 0.0])
            pos = g - go * 20 + gf * 2 + [0, 0, 3.5]
            tgt = g - gf * 3 + [0, 0, 1.2]
        elif phase == 'stretch':                                # 直線：横（内側）から並走
            pos = pt - out * 17 + fwd * 4 + [0, 0, 3.8]
            tgt = pt + fwd * 3 + [0, 0, 1.2]
        else:
            if turn:                                            # カーブ：少し高い後ろから
                pos = pt - fwd * 16 + out * 3 + [0, 0, 8.5]
            else:                                               # 道中：斜め後ろから
                pos = pt - fwd * 13 + out * 9 + [0, 0, 5.0]
            tgt = pt + fwd * 12 + [0, 0, 1.0]
        return pos, tgt

    def render(self, horses, lead_run, pack_run, phase, sky, frame, label_n=None, name=None, F=None):
        """horses：[(n, run, lane, 走っているか)]"""
        pos, tgt = self._cam_target(lead_run, pack_run, phase)
        if self.cam_state is None:
            self.cam_state = [pos, tgt]
        else:
            k = 0.06
            self.cam_state = [self.cam_state[0] + (pos - self.cam_state[0]) * k, self.cam_state[1] + (tgt - self.cam_state[1]) * k * 1.5]
        cam = self.cam
        cam.set(*self.cam_state)
        im = sky.resize((self.w, self.h)) if sky.size != (self.w, self.h) else sky.copy()
        d = ImageDraw.Draw(im)
        hy = cam.horizon()
        d.rectangle((0, max(0, hy), self.w, self.h), fill=(122, 196, 96))          # 地面（芝）
        T = self.trk
        step = 6.0
        ps = np.arange(0, T.L + step, step)
        col_track = (204, 168, 122) if self.surf == 'ダ' else (104, 182, 84)
        col_track2 = (196, 160, 114) if self.surf == 'ダ' else (98, 174, 80)
        # コース（帯）
        inner = [np.array(T.center(p, 0)[:2] + (0.0,)) for p in ps]
        outer = [np.array(T.center(p, T.W)[:2] + (0.0,)) for p in ps]
        for i in range(len(ps) - 1):
            poly = _clip_poly(cam, [inner[i], inner[i + 1], outer[i + 1], outer[i]])
            if len(poly) >= 3:
                d.polygon(poly, fill=col_track if (i // 3) % 2 else col_track2)
        # 内側の芝（ダートのとき）
        if self.surf == 'ダ':
            in2 = [np.array(T.center(p, -14)[:2] + (0.0,)) for p in ps]
            for i in range(len(ps) - 1):
                poly = _clip_poly(cam, [in2[i], in2[i + 1], inner[i + 1], inner[i]])
                if len(poly) >= 3:
                    d.polygon(poly, fill=(134, 206, 104))
        # ラチ（白い柵）
        for lane in (-0.5, T.W + 0.5):
            pts = []
            for p in ps:
                x, y, _, _ = T.center(p, lane)
                X, Y, Z = cam.to_cam((x, y, 1.0))
                if Z > 1.0:
                    pts.append((cam.w / 2 + cam.fpx * X / Z, cam.h / 2 - cam.fpx * Y / Z, Z))
                else:
                    pts.append(None)
            for a, b in zip(pts[:-1], pts[1:]):
                if a and b:
                    wdt = max(1, int(cam.fpx * 0.12 / min(a[2], b[2])))
                    d.line((a[0], a[1], b[0], b[1]), fill=(250, 250, 250), width=min(wdt, 8))
            for i in range(0, len(ps), 1):                # 支柱（近いところだけ）
                x, y, _, _ = T.center(ps[i], lane)
                X, Y, Z = cam.to_cam((x, y, 0.0))
                if 1.0 < Z < 120:
                    sx, sy = cam.w / 2 + cam.fpx * X / Z, cam.h / 2 - cam.fpx * Y / Z
                    tx, ty, _ = cam.proj((x, y, 1.0))
                    d.line((sx, sy, tx, ty), fill=(236, 236, 236), width=max(1, int(cam.fpx * 0.08 / Z)))
        # ゴール板・ハロン棒（外ラチ側）
        for m in list(range(200, int(self.dist), 200)) + [int(self.dist)]:
            x, y, _, _ = T.center(m - self.dist, T.W + 1.5)
            X, Y, Z = cam.to_cam((x, y, 0.0))
            if 1.0 < Z < 600:
                sx, sy = cam.w / 2 + cam.fpx * X / Z, cam.h / 2 - cam.fpx * Y / Z
                hh = cam.fpx * (4.5 if m == int(self.dist) else 2.2) / Z
                if m == int(self.dist):
                    d.rectangle((sx - max(1, hh * 0.04), sy - hh, sx + max(1, hh * 0.04), sy), fill=(250, 250, 250))
                    rr = hh * 0.22
                    d.ellipse((sx - rr, sy - hh - rr, sx + rr, sy - hh + rr), fill=(250, 250, 250), outline=(210, 40, 40), width=max(1, int(rr * 0.25)))
                    # ゴールライン
                    a = _clip_poly(cam, [np.array(T.center(0, -0.5)[:2] + (0.02,)), np.array(T.center(0.6, -0.5)[:2] + (0.02,)),
                                         np.array(T.center(0.6, T.W)[:2] + (0.02,)), np.array(T.center(0, T.W)[:2] + (0.02,))])
                    if len(a) >= 3:
                        d.polygon(a, fill=(250, 250, 250))
                else:
                    rem = int(self.dist) - m
                    d.rectangle((sx - max(1, hh * 0.05), sy - hh, sx + max(1, hh * 0.05), sy), fill=(220, 40, 40) if rem % 400 == 0 else (250, 250, 250))
                    if F and Z < 260 and rem % 400 == 0:
                        t = str(rem)
                        fz = F(max(12, min(24, int(cam.fpx * 1.4 / Z))))
                        d.text((sx - d.textlength(t, font=fz) / 2, sy - hh - fz.size - 2), t, font=fz, fill=(120, 80, 10), stroke_width=2, stroke_fill=(255, 245, 210))
        # スタンド（ホームの直線の外）
        fx = T.center(0, 0)[0]
        st0, st1 = (fx - 260, fx + 60) if not T.right else (fx - 60, fx + 260)
        yo = -(T.r + T.W + 40)
        for (x0, x1, h0, col) in ((st0, st1, 22, (222, 214, 196)),):
            box = [np.array([x0, yo, 0]), np.array([x1, yo, 0]), np.array([x1, yo - 20, h0]), np.array([x0, yo - 20, h0])]
            poly = _clip_poly(cam, box)
            if len(poly) >= 3:
                d.polygon(poly, fill=col, outline=(120, 110, 100))
                if col[0] > 200:                         # 客席の段
                    for k in range(1, 8):
                        z = h0 * k / 8
                        a = _clip_poly(cam, [np.array([x0, yo - 20 * k / 8, z]), np.array([x1, yo - 20 * k / 8, z]),
                                             np.array([x1, yo - 20 * k / 8, z + 0.6]), np.array([x0, yo - 20 * k / 8, z + 0.6])])
                        if len(a) >= 3:
                            d.polygon(a, fill=(160, 150, 136))
                    roof = _clip_poly(cam, [np.array([x0 - 5, yo - 24, h0 + 4]), np.array([x1 + 5, yo - 24, h0 + 4]),
                                            np.array([x1 + 5, yo + 4, h0 + 1]), np.array([x0 - 5, yo + 4, h0 + 1])])
                    if len(roof) >= 3:
                        d.polygon(roof, fill=(50, 52, 60))
        # 木と馬を奥から
        items = []
        for (x, y, s, pine) in self.trees:
            X, Y, Z = cam.to_cam((x, y, 0.0))
            if Z > 2:
                items.append((Z, 'tree', (x, y, s, pine)))
        for n, run, lane, moving in horses:
            p3, h, _ = self.pos3(run, lane)
            X, Y, Z = cam.to_cam(p3)
            if Z > 1.5:
                items.append((Z, 'horse', (n, p3, h, moving)))
        items.sort(key=lambda t: -t[0])
        label_xy = None
        for Z, kind, data in items:
            if kind == 'tree':
                x, y, s, pine = data
                bx, by, _ = cam.proj((x, y, 0.0))
                hh = cam.fpx * s / Z
                if by < -50 or by > self.h + hh or bx < -hh or bx > self.w + hh:
                    continue
                d.rectangle((bx - hh * 0.06, by - hh * 0.5, bx + hh * 0.06, by), fill=(110, 80, 50))
                if pine:
                    d.polygon([(bx, by - hh * 1.2), (bx - hh * 0.35, by - hh * 0.35), (bx + hh * 0.35, by - hh * 0.35)], fill=(54, 130, 70))
                else:
                    for dx, dy in ((-0.22, -0.62), (0.22, -0.62), (0, -0.85)):
                        r_ = hh * 0.3
                        d.ellipse((bx + dx * hh - r_, by + dy * hh - r_, bx + dx * hh + r_, by + dy * hh + r_), fill=(72, 158, 76))
                continue
            n, p3, h, moving = data
            bx, by, _ = cam.proj(p3)
            hd = np.array([math.cos(h), math.sin(h), 0.0])
            hx, hz = hd @ cam.rt, hd @ cam.f
            hh = cam.fpx * 2.3 / Z                           # 馬の高さ 2.3m
            if hh < 4 or by < 0 or by > self.h + hh:
                continue
            fr = int(frame * 0.45 + n * 3) % 8 if moving else 2
            ang = math.degrees(math.atan2(hx, hz))           # 0＝カメラから遠ざかる（後ろ姿）
            if abs(ang) < 32:
                spr, flip = self.spr[n]['back'][fr], False
            elif abs(ang) > 148:
                spr, flip = self.spr[n]['front'][fr], False
            else:
                spr, flip = self.spr[n]['side'][fr], ang < 0
            sc = hh / 140
            sw, sh = max(2, int(spr.width * sc)), max(2, int(spr.height * sc))
            if spr is self.spr[n]['side'][fr]:
                squeeze = max(0.55, abs(math.sin(math.radians(ang))))
                sw = max(2, int(sw * squeeze))
            s2 = spr.resize((sw, sh), Image.BILINEAR)
            if flip:
                s2 = s2.transpose(Image.FLIP_LEFT_RIGHT)
            sh_ = Image.new('RGBA', (sw, max(2, sh // 8)), (0, 0, 0, 0))
            ImageDraw.Draw(sh_).ellipse((0, 0, sw, max(2, sh // 8) - 1), fill=(0, 0, 0, 60))
            im.paste(sh_, (int(bx - sw / 2), int(by - sh // 16)), sh_)
            im.paste(s2, (int(bx - sw / 2), int(by - sh + sh * 0.06)), s2)
            if n == label_n:
                label_xy = (bx, by - sh)
        if label_xy and name and F:
            t = name
            fz = F(20)
            tw_ = d.textlength(t, font=fz)
            bx = max(4, min(self.w - tw_ - 24, label_xy[0] - tw_ / 2 - 10))
            by = max(4, label_xy[1] - 36)
            d.rounded_rectangle((bx, by, bx + tw_ + 20, by + 30), radius=8, fill=(22, 22, 26), outline=(240, 240, 240))
            d.polygon([(label_xy[0] - 6, by + 30), (label_xy[0] + 6, by + 30), (label_xy[0], by + 38)], fill=(22, 22, 26))
            d.text((bx + 10, by + 2), t, font=fz, fill=(255, 255, 255))
        return im
